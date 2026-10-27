from pathlib import Path
import hashlib
import plistlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from beaver.debug import _app_arguments, _identity, inspect_native, inspect_session, screenshot_record, start, stop, tail
from beaver.errors import BeaverError
from beaver.io import load, read, write_json
from beaver.output_limits import digest_file


class DebugSessionTests(unittest.TestCase):
    def make_session(self, root, **updates):
        session = root / 'session.json'
        log = root / 'player.log'
        log.write_text('hello\nBearer top-secret\n', encoding='utf-8')
        data = dict(schemaVersion=1, sessionId='00000000-0000-4000-8000-000000000001',
                    sessionMarker='beaver-debug-session-00000000-0000-4000-8000-000000000001',
                    status='running', pid=998877, executable='/self-owned/Test.app/Contents/MacOS/Test',
                    executableSha256='a'*64, processStarted='today', logPath=str(log),
                    runDirectory=str(root), screenshotEvidence=[])
        data.update(updates)
        write_json(session, data)
        return session

    def test_inspect_pid_identity_mismatch_never_signals_process(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            session = self.make_session(Path(temp))
            with patch('beaver.debug._ps', side_effect=lambda pid, field: 'foreign-process' if field == 'command' else 'today'), \
                 patch('beaver.debug._exe', return_value='/different/executable'), \
                 patch('beaver.debug.os.kill') as kill:
                observed = inspect_session(session)
                self.assertEqual(observed['status'], 'identity_unknown')
                self.assertEqual(observed['workerStatus'], 'running')
                result = stop(session)
                self.assertEqual(result['stopResult'], 'termination_requested')
                self.assertTrue(session.parent.joinpath('stop-request.json').is_file())
                kill.assert_not_called()

    def test_tail_redacts_secrets_and_screenshot_requires_exact_hash(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            session = self.make_session(root)
            lines = tail(session)['lines']
            self.assertIn('[REDACTED]', lines[-1])
            shot = root / 'caller-shot.png'
            shot.write_bytes(b'self-authored evidence bytes')
            digest = hashlib.sha256(shot.read_bytes()).hexdigest()
            with self.assertRaises(BeaverError):
                screenshot_record(session, shot, '0'*64)
            result = screenshot_record(session, shot, digest)
            self.assertEqual(result['evidence']['source'], 'caller_supplied_file')
            self.assertIn('not_verified', result['evidence']['originClaim'])

    def test_tail_reads_only_bounded_suffix_of_sparse_huge_log(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            session = self.make_session(root)
            log = root/'player.log'
            with log.open('wb') as stream:
                stream.truncate(512*1024*1024+256)
                stream.seek(0, 2)
                stream.write(b'old-tail-line\nBearer hidden-value\nknown final marker\n')
            original_open = Path.open
            requested = []

            class BoundedRead:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): return self
                def __exit__(self, *args): return self.stream.__exit__(*args)
                def seek(self, *args): return self.stream.seek(*args)
                def tell(self): return self.stream.tell()
                def read(self, size=-1):
                    requested.append(size)
                    if size < 0 or size > 256*1024:
                        raise AssertionError('tail attempted an unbounded/oversized read')
                    return self.stream.read(size)

            def tracked_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                return BoundedRead(stream) if path == log else stream

            with patch.object(Path, 'open', new=tracked_open):
                result = tail(session, lines=3)
            self.assertEqual(requested, [256*1024])
            self.assertIn('known final marker', result['lines'][-1])
            self.assertIn('[REDACTED]', result['lines'][-2])

    def test_unavailable_process_probe_is_unknown_and_confirmed_absence_is_exited(self):
        record = dict(pid=123, executable='/candidate', executableSha256='a'*64,
                      sessionMarker='beaver-debug-session-id', processStarted='then')
        with patch('beaver.debug._ps', return_value=None), patch('beaver.debug._exe', return_value=None):
            self.assertEqual(_identity(record)[0], 'identity_unknown')
        with patch('beaver.debug._ps', return_value=''), patch('beaver.debug._exe', return_value=None):
            self.assertEqual(_identity(record)[0], 'exited')

    def test_viewport_arguments_default_to_player_settings_and_explicit_size_is_paired(self):
        default = _app_arguments('/candidate/Game', '/tmp/game.log', 'session-marker')
        self.assertIn('-screen-fullscreen', default)
        self.assertIn('0', default)
        self.assertNotIn('-screen-width', default)
        self.assertNotIn('-screen-height', default)
        explicit = _app_arguments('/candidate/Game', '/tmp/game.log', 'session-marker', 900, 1400)
        self.assertEqual(explicit[explicit.index('-screen-width')+1], '900')
        self.assertEqual(explicit[explicit.index('-screen-height')+1], '1400')
        for width, height in ((900, None), (None, 1400), (63, 1400), (900, 8193), (True, 700)):
            with self.assertRaisesRegex(BeaverError, 'DEBUG_VIEWPORT'):
                _app_arguments('/candidate/Game', '/tmp/game.log', 'session-marker', width, height)

    def build_mock_candidate(self, root, body):
        app = root/'candidate/player/Mock.app'
        exe = app/'Contents/MacOS/Mock'
        exe.parent.mkdir(parents=True)
        exe.write_text('#!'+sys.executable+'\n'+body, encoding='utf-8')
        exe.chmod(0o755)
        info = app/'Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleExecutable':'Mock'}))
        manifest = root/'candidate/artifact-manifest.json'
        rows = []
        for file in (exe, info):
            rows.append(dict(path=file.relative_to(manifest.parent).as_posix(),
                             sha256=digest_file(file), bytes=file.stat().st_size))
        write_json(manifest, dict(schemaVersion=1, sourceSha256='b'*64, files=rows))
        return manifest

    def wait_for(self, session, accepted, timeout=10):
        until = time.time()+timeout
        while time.time() < until:
            data = inspect_session(session)
            if data.get('status') in accepted:
                return data
            time.sleep(0.05)
        self.fail('debug worker did not reach expected state: '+repr(inspect_session(session)))

    def wait_worker_status(self, session, accepted, timeout=10):
        status_path = Path(session).with_name('status.json')
        until = time.time()+timeout
        while time.time() < until:
            if status_path.is_file():
                data = load(read(status_path, max_bytes=2*1024*1024))
                if data.get('status') in accepted:
                    return data
            time.sleep(0.05)
        self.fail('debug worker status did not reach expected state: '+str(status_path))

    def test_detached_worker_records_mock_exit_and_stop_without_session_write_race(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            manifest = self.build_mock_candidate(root, 'raise SystemExit(0)\n')
            launched = start(manifest, root/'normal-run', digest_file(manifest), duration=10)
            ended = self.wait_for(launched['session'], {'exited','failed'})
            self.assertEqual(ended['status'], 'exited')
            self.assertEqual(ended['exitCode'], 0)

            manifest = self.build_mock_candidate(root/'second', 'import time\ntime.sleep(30)\n')
            evidence = root/'own-evidence.png'
            evidence.write_bytes(b'caller supplied')
            launched = start(manifest, root/'stop-run', digest_file(manifest), duration=30, width=900, height=1400)
            session_record = load(read(Path(launched['session']), max_bytes=2*1024*1024))
            self.assertEqual(session_record['requestedViewport'], dict(width=900, height=1400))
            self.wait_worker_status(launched['session'], {'running'})
            running = inspect_session(launched['session'])
            args = running['launchArguments']
            self.assertEqual(args[args.index('-screen-width')+1], '900')
            self.assertEqual(args[args.index('-screen-height')+1], '1400')
            screenshot_record(launched['session'], evidence, digest_file(evidence))
            response = stop(launched['session'])
            self.assertEqual(response['status'], 'stopping')
            self.wait_worker_status(launched['session'], {'stopped','failed'})
            ended = inspect_session(launched['session'])
            self.assertEqual(ended['status'], 'stopped')
            self.assertEqual(len(ended['screenshotEvidence']), 1)

    def transaction(self, root, unity=False, operation='validate.play', play_frames=None,
                    report_status='verified', report_plan_override=None, exited=True):
        project = str(root/'project')
        if unity:
            plan = dict(contract='beaver-unity/1', schemaVersion=1, projectRoot=project,
                        spec=dict(operation=operation))
            plan_name, journal_contract, process = 'unity-plan.json', None, dict(
                pid=3456, mode='apply', owned=True, exited=exited, exitCode=0 if exited else None)
            report = dict(status=report_status, operation=operation, mode='apply',
                          planSha256='pending')
            if play_frames is not None:
                report['playFrames'] = play_frames
            report_name = 'native-apply.json'
            journal = dict(status='complete' if exited else 'prepared', projectRoot=project,
                           operation=operation)
        else:
            plan = dict(contract='beaver-batch/2', schemaVersion=2, projectRoot=project, spec={})
            plan_name, journal_contract, process = 'batch-plan.json', 'beaver-batch-transaction/2', dict(
                pid=3456, owned=True, executable='/Applications/Unity.app/Contents/MacOS/Unity',
                startedUtc='2026-10-26T00:00:00Z', exited=exited, exitCode=0 if exited else None)
            report = dict(status=report_status, mode='apply', planSha256='pending')
            report_name = 'native-result.json'
            journal = dict(contract=journal_contract, status='complete' if exited else 'prepared', projectRoot=project)
        from beaver.io import encoded, sha
        digest = sha(encoded(plan))
        report['planSha256'] = report_plan_override or digest
        journal['planSha256'] = digest
        write_json(root/plan_name, plan)
        write_json(root/'journal.json', journal)
        write_json(root/'process.json', process)
        write_json(root/report_name, report)
        return root

    def test_native_inspector_blocks_plan_identity_mismatch(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            self.transaction(root, report_plan_override='0'*64)
            result = inspect_native(root)
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('native report plan identity mismatch', result['diagnostics'])

    def test_play_mode_requires_native_frames_and_running_editor_is_unverified(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            self.transaction(root, unity=True, play_frames=None)
            result = inspect_native(root)
            self.assertEqual(result['kind'], 'Editor')
            self.assertEqual(result['playModeStatus'], 'plannedPlayMode_unverified')
            self.assertEqual(result['status'], 'partial')  # v1 process record has no executable path

        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            self.transaction(root, unity=True, play_frames=7, exited=False, report_status='running')
            result = inspect_native(root)
            self.assertEqual(result['kind'], 'PlayMode')
            self.assertEqual(result['playModeStatus'], 'observedPlayMode')
            self.assertEqual(result['processState'], 'process state unverified')
            self.assertEqual(result['status'], 'partial')

    def test_batch_recorded_exit_and_verified_native_are_reported_separately(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root = Path(temp)
            self.transaction(root)
            result = inspect_native(root)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['kind'], 'Editor')
            self.assertEqual(result['processState'], 'recorded_exit')
            self.assertTrue(result['nativeVerified'])


if __name__ == '__main__':
    unittest.main()
