"""Bounded, hash-bound macOS candidate sessions for manual debugging.

The detached Python worker owns the launched candidate after the CLI exits.
This module records process identity only; it does not claim gameplay success.
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
import uuid

from .errors import BeaverError, fail
from .io import encoded, load, new_output, read, safe, sha, write_json
from .output_limits import digest_file
from .payload import verify_manifest

MAX_DURATION = 3600
_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)=)[^\s\"']+")


def _ps(pid, field):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', field + '='], capture_output=True,
                                text=True, timeout=5, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _exe(pid):
    # proc_pidpath gives the kernel-reported executable path on macOS.
    if sys.platform != 'darwin':
        return None
    import ctypes
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    buf = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(int(pid), buf, len(buf)) <= 0:
        return None
    return os.fsdecode(buf.value)


def _identity(record):
    pid = record.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return 'identity_unknown', 'Invalid recorded process id'
    command = _ps(pid, 'command')
    started = _ps(pid, 'lstart')
    try:
        exe = _exe(pid)
    except (OSError, PermissionError):
        exe = None
    if command is None or started is None:
        return 'identity_unknown', 'Process inspection was unavailable or denied'
    if command == '' and started == '':
        return 'exited', 'ps completed successfully and reported no process for this PID'
    if not command or not started or exe is None:
        return 'identity_unknown', 'Process identity fields were incomplete or access was denied'
    if record.get('sessionMarker') not in command:
        return 'identity_unknown', 'Process command line no longer carries this session marker'
    if str(record.get('executable')) != exe or record.get('processStarted') != started:
        return 'identity_unknown', 'PID identity differs from the recorded executable or start time'
    try:
        actual_hash = digest_file(exe)
    except (OSError, BeaverError):
        return 'identity_unknown', 'Could not hash the recorded process executable'
    if actual_hash != record.get('executableSha256'):
        return 'identity_unknown', 'Executable hash differs from the recorded candidate'
    return 'running', None


def _atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _status_path(session_path):
    return Path(session_path).with_name('status.json')


def _write_status(session_path, status, **fields):
    _atomic_json(_status_path(session_path), dict(status=status, updatedUtc=datetime.now(timezone.utc).isoformat(), **fields))


def _viewport(width, height):
    if width is None and height is None:
        return None
    if type(width) is not int or type(height) is not int or not 64 <= width <= 8192 or not 64 <= height <= 8192:
        fail('DEBUG_VIEWPORT', 'viewport', 'Width and height must be provided together as integers from 64..8192')
    return dict(width=width, height=height)


def _app_arguments(executable, log_path, marker, width=None, height=None):
    viewport = _viewport(width, height)
    args = [str(executable), '-screen-fullscreen', '0']
    if viewport is not None:
        args.extend(['-screen-width', str(viewport['width']), '-screen-height', str(viewport['height'])])
    args.extend(['-logFile', str(log_path), '-beaver-session='+marker])
    return args


def start(manifest, output, expected, duration=MAX_DURATION, width=None, height=None):
    viewport = _viewport(width, height)
    path = safe(manifest)
    if expected != digest_file(path):
        fail('DEBUG_MANIFEST_DRIFT', str(path), 'Explicit --expected must match the artifact manifest SHA256')
    if type(duration) is not int or not 1 <= duration <= MAX_DURATION:
        fail('DEBUG_DURATION', str(duration), 'Explicit process budget must be 1..3600 seconds')
    doc = load(read(path, max_bytes=128 * 1024**2), max_nodes=8_000_000)
    if doc.get('schemaVersion') != 1 or not doc.get('sourceSha256') or not isinstance(doc.get('files'), list):
        fail('DEBUG_MANIFEST', str(path), 'Expected a beaver build artifact manifest')
    root = path.parent
    try:
        before = verify_manifest(root, path, scope='player', allow_links=True,artifact=True)
    except BeaverError as error:
        fail('DEBUG_ARTIFACT_' + error.code, error.location, error.message)
    apps = list((root / 'player').glob('*.app'))
    if len(apps) != 1:
        fail('DEBUG_APP', str(root), 'Expected exactly one fixed macOS candidate app')
    app = apps[0]
    try:
        info = plistlib.loads(read(app / 'Contents/Info.plist'))
    except (OSError, ValueError) as error:
        fail('DEBUG_APP', str(app), 'Could not read candidate Info.plist: ' + str(error))
    name = info.get('CFBundleExecutable')
    if not isinstance(name, str) or Path(name).name != name:
        fail('DEBUG_EXECUTABLE', str(app), 'Invalid CFBundleExecutable')
    executable = safe(app / 'Contents/MacOS' / name)
    rel = executable.relative_to(root).as_posix()
    rows = {row.get('path'): row for row in doc['files'] if isinstance(row, dict)}
    if not os.access(executable, os.X_OK) or rel not in rows:
        fail('DEBUG_EXECUTABLE', str(executable), 'Executable is not an executable listed in the artifact manifest')
    session_id = str(uuid.uuid4())
    marker = 'beaver-debug-session-' + session_id
    stage = new_output(output, [root])
    record = dict(schemaVersion=1, sessionId=session_id, sessionMarker=marker, status='starting',
                  pid=None, executable=str(executable), executableSha256=rows[rel]['sha256'],
                  artifactRoot=str(root), artifactManifest=str(path), artifactManifestSha256=expected,
                  sourceSha256=doc['sourceSha256'], startedUtc=datetime.now(timezone.utc).isoformat(),
                  durationBudgetSeconds=duration, deadlineEpoch=time.time()+duration,
                  requestedViewport=viewport,
                  runDirectory=str(stage), logPath=str(stage/'player.log'), screenshotEvidence=[],
                  targetInspection=dict(status='candidate_app', kind='App', note='This API launches a fixed app candidate; it does not assert gameplay success'),
                  artifactVerificationBefore=before,
                  scope='fixed candidate only; no real project, backend or player saves')
    write_json(stage/'session.json', record)
    _write_status(stage/'session.json', 'starting')
    # The worker writes status.json separately; the immutable session record
    # can never race with its launch/status update.
    code = "from beaver.debug import _worker; _worker(sys.argv[1])"
    env = {k: v for k, v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG','LC_ALL')}
    package_root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root
    try:
        with (stage / 'worker.log').open('ab') as log:
            worker = subprocess.Popen([sys.executable, '-c', 'import sys; ' + code, str(stage/'session.json')],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      close_fds=True, start_new_session=True, env=env)
    except OSError as error:
        _write_status(stage/'session.json', 'failed', diagnostic='worker launch failed: '+type(error).__name__)
        fail('DEBUG_WORKER_FAILED', str(stage), 'Could not start detached debug worker: '+str(error))
    # This process deliberately delegates lifetime ownership to its detached
    # worker; suppress Popen's child-finalizer warning in short-lived CLI hosts.
    worker._child_created = False
    return dict(status='starting', session=str(stage/'session.json'), sessionId=session_id,
                workerPid=worker.pid, durationBudgetSeconds=duration,
                artifactVerification=before, note='Worker launch is asynchronous; inspect_session reports process state, not game success')


def _worker(session_path):
    path = Path(session_path)
    record = load(read(path, max_bytes=2*1024*1024))
    marker = record['sessionMarker']
    viewport = record.get('requestedViewport') or {}
    args = _app_arguments(record['executable'], record['logPath'], marker,
                          viewport.get('width'), viewport.get('height'))
    # Keep only non-secret launch essentials; stale control-job variables are removed.
    env = {k: v for k, v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG','LC_ALL')}
    try:
        with open(record['logPath'], 'ab') as log:
            process = subprocess.Popen(args, cwd=Path(record['executable']).parent, env=env,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       close_fds=True, start_new_session=True)
        launched = dict(pid=process.pid, processStarted=_ps(process.pid, 'lstart'),
                        executable=record['executable'], executableSha256=record['executableSha256'],
                        sessionMarker=marker, launchArguments=args,
                        launchedUtc=datetime.now(timezone.utc).isoformat())
        _write_status(path, 'running', **launched)
        while process.poll() is None:
            request = path.parent/'stop-request.json'
            if time.time() >= record['deadlineEpoch']:
                _terminate_owned(process)
                final_status = 'failed'
                final_fields = dict(**launched, stopReason='duration_budget_expired', exitCode=process.poll())
                break
            if request.exists():
                _terminate_owned(process)
                code = process.poll()
                final_status = 'stopped' if code == -signal.SIGTERM else ('exited' if code == 0 else 'failed')
                final_fields = dict(**launched, stopResult='stop_request_processed', exitCode=code,
                                    exitedUtc=datetime.now(timezone.utc).isoformat())
                break
            time.sleep(0.25)
        else:
            code = process.returncode
            final_status = 'stopped' if code == -signal.SIGTERM else ('exited' if code == 0 else 'failed')
            final_fields = dict(**launched, exitCode=code, exitedUtc=datetime.now(timezone.utc).isoformat(),
                                result='process lifecycle only; gameplay success is not established')
        try:
            after = verify_manifest(record['artifactRoot'], record['artifactManifest'], scope='player', allow_links=True,artifact=True)
        except BeaverError as error:
            final_status = 'failed'
            final_fields['artifactVerificationAfter'] = 'failed'
            final_fields['artifactDiagnostic'] = error.as_dict()
        else:
            final_fields['artifactVerificationAfter'] = after
        _write_status(path, final_status, **final_fields)
    except Exception as error:
        _write_status(path, 'failed', diagnostic=type(error).__name__ + ': ' + str(error))


def _terminate_owned(process):
    """Signal only the child represented by this worker's Popen handle."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                return
            process.wait(timeout=8)


def _session_path(session):
    if isinstance(session, dict):
        session = session.get('session') or session.get('report')
    path = safe(session)
    if path.name != 'session.json' or not path.is_file():
        fail('DEBUG_SESSION', str(path), 'Expected a session.json returned by start')
    record = load(read(path, max_bytes=2*1024*1024))
    try:
        identity = str(uuid.UUID(record.get('sessionId', '')))
    except (ValueError, TypeError, AttributeError):
        fail('DEBUG_SESSION', str(path), 'Session UUID is missing or invalid')
    if (record.get('schemaVersion') != 1 or identity != record.get('sessionId') or
            record.get('sessionMarker') != 'beaver-debug-session-'+identity or
            record.get('runDirectory') != str(path.parent)):
        fail('DEBUG_SESSION', str(path), 'Session record identity does not match its directory')
    return path


def inspect_session(session):
    path = _session_path(session)
    record = load(read(path, max_bytes=2*1024*1024))
    status_path = _status_path(path)
    status = load(read(status_path, max_bytes=2*1024*1024)) if status_path.is_file() else {'status': record.get('status', 'starting')}
    record.update(status)
    if record.get('status') == 'running':
        state, reason = _identity(record)
        if state != 'running':
            record.update(workerStatus='running', status=state, identityDiagnostic=reason)
    if record.get('status') == 'starting' and time.time() > record.get('deadlineEpoch', 0):
        record.update(status='identity_unknown', identityDiagnostic='Worker did not publish the process identity before the deadline')
    evidence_dir = path.parent/'evidence'
    record['screenshotEvidence'] = [load(read(item, max_bytes=64*1024)) for item in sorted(evidence_dir.glob('*.json'))] if evidence_dir.exists() else []
    record['targetInspection'] = dict(status='candidate_app', kind='App',
                                     note='This API launches and tracks only the fixed app candidate; Editor and PlayMode inspection is not part of this API')
    return record


def stop(session):
    path = _session_path(session)
    status_path = _status_path(path)
    status = load(read(status_path, max_bytes=2*1024*1024)) if status_path.is_file() else {'status': 'starting'}
    if status.get('status') in ('exited', 'stopped', 'failed'):
        return dict(status, stopResult='already_finished')
    _atomic_json(path.parent/'stop-request.json', dict(requestedUtc=datetime.now(timezone.utc).isoformat(),
                                                       sessionId=load(read(path, max_bytes=2*1024*1024))['sessionId']))
    return dict(status='stopping', stopResult='termination_requested', control='worker-owned Popen handle')
    return record


def tail(session, lines=80):
    path = _session_path(session)
    if type(lines) is not int or not 1 <= lines <= 500:
        fail('DEBUG_TAIL_LINES', str(lines), 'lines must be 1..500')
    record = load(read(path, max_bytes=2*1024*1024))
    log = safe(record['logPath'])
    if not log.is_file():
        return dict(status='not_available', lines=[])
    with log.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()
        stream.seek(max(0, length-256*1024), os.SEEK_SET)
        raw = stream.read(min(length, 256*1024))
    raw = raw.decode('utf-8', errors='replace').splitlines()[-lines:]
    return dict(status='ok', lines=[_SECRET.sub(r'\1[REDACTED]', line)[:4000] for line in raw])


def screenshot_record(session, path, expected_sha):
    session_path = _session_path(session)
    record = load(read(session_path, max_bytes=2*1024*1024))
    image_path = safe(path)
    if not image_path.is_file():
        fail('DEBUG_SCREENSHOT', str(image_path), 'Expected an existing screenshot evidence file')
    actual = digest_file(image_path)
    if not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha) or actual != expected_sha.lower():
        fail('DEBUG_SCREENSHOT_HASH', str(image_path), 'Explicit screenshot SHA256 does not match evidence file')
    evidence = dict(path=str(image_path), sha256=actual, recordedUtc=datetime.now(timezone.utc).isoformat(),
                    source='caller_supplied_file', originClaim='not_verified; recording does not create or authenticate a GUI screenshot')
    evidence_dir = session_path.parent/'evidence'
    evidence_dir.mkdir(mode=0o700, exist_ok=True)
    evidence_path = evidence_dir/(uuid.uuid4().hex + '.json')
    _atomic_json(evidence_path, evidence)
    return dict(status='recorded', evidence=evidence)


def inspect_native(transaction):
    """Read a Unity/batch transaction's recorded native target evidence only."""
    root = safe(transaction)
    if not root.is_dir():
        fail('DEBUG_NATIVE_TRANSACTION', str(root), 'Expected an existing transaction directory')

    def json_file(name, limit=128*1024**2):
        path = root/name
        if not path.is_file():
            return None
        try:
            return load(read(path, max_bytes=limit), max_nodes=8_000_000)
        except (BeaverError, OSError, ValueError, TypeError) as error:
            fail('DEBUG_NATIVE_RECORD', str(path), 'Could not read transaction JSON: '+type(error).__name__)

    plan_names = [name for name in ('unity-plan.json', 'batch-plan.json') if (root/name).is_file()]
    if len(plan_names) != 1:
        fail('DEBUG_NATIVE_PLAN', str(root), 'Expected exactly one unity-plan.json or batch-plan.json')
    plan_name = plan_names[0]
    plan = json_file(plan_name)
    journal = json_file('journal.json', 32*1024**2)
    process = json_file('process.json', 2*1024**2)
    if not isinstance(plan, dict) or not isinstance(journal, dict):
        fail('DEBUG_NATIVE_RECORD', str(root), 'Plan and journal must be JSON objects')

    unity = plan_name == 'unity-plan.json'
    production=not unity and plan.get('contract')=='beaver-production/3'
    expected_contract = 'beaver-unity/1' if unity else 'beaver-production/3' if production else 'beaver-batch/2'
    journal_contract = None if unity else 'beaver-production-transaction/3' if production else 'beaver-batch-transaction/2'
    expected_plan_sha = sha(encoded(plan))
    diagnostics = []
    blocked = False
    if plan.get('contract') != expected_contract:
        diagnostics.append('plan contract mismatch')
        blocked = True
    if not isinstance(journal.get('planSha256'), str) or journal.get('planSha256') != expected_plan_sha:
        diagnostics.append('journal plan identity does not match the transaction plan')
        blocked = True
    if journal_contract and journal.get('contract') != journal_contract:
        diagnostics.append('journal contract mismatch')
        blocked = True
    project_root = plan.get('projectRoot')
    if not isinstance(project_root, str) or not project_root:
        diagnostics.append('plan projectRoot is missing')
        blocked = True
    elif journal.get('projectRoot') != project_root:
        diagnostics.append('journal projectRoot does not match plan')
        blocked = True
    if isinstance(project_root, str):
        if not Path(project_root).is_absolute():
            diagnostics.append('plan projectRoot is not absolute')
            blocked = True
        else:
            try:
                if str(safe(project_root)) != project_root:
                    diagnostics.append('plan projectRoot is not canonical')
                    blocked = True
            except BeaverError:
                diagnostics.append('plan projectRoot contains a symbolic link')
                blocked = True
    spec = plan.get('spec')
    operation = spec.get('operation') if unity and isinstance(spec, dict) else 'production.apply' if production else 'batch.apply'
    if unity and not isinstance(spec, dict):
        diagnostics.append('Unity plan spec is missing or malformed')
        blocked = True
    if not isinstance(journal.get('status'), str) or not journal.get('status'):
        diagnostics.append('journal status is missing or invalid')
        blocked = True
    allowed_journal_status = ({'prepared','native-complete-pending-source-audit','recovery-required','complete'}
                              if unity else {'prepared','recovery-required','complete','recovered'})
    if journal.get('status') not in allowed_journal_status:
        diagnostics.append('journal status is outside the transaction contract')
        blocked = True
    if unity and (not isinstance(operation, str) or journal.get('operation') != operation):
        diagnostics.append('journal operation does not match plan')
        blocked = True
    if plan.get('schemaVersion') != (1 if unity else 3 if production else 2):
        diagnostics.append('plan schema version mismatch')
        blocked = True

    process_complete = isinstance(process, dict)
    process_native_gate = False
    pid = None
    executable = None
    owned = None
    exited = None
    process_state = 'unavailable'
    process_mode = None
    exit_code = None
    if not process_complete:
        diagnostics.append('process record is missing')
    else:
        pid, owned, exited = process.get('pid'), process.get('owned'), process.get('exited')
        executable = process.get('executable')
        process_mode = process.get('mode')
        if type(pid) is not int or pid <= 0 or type(owned) is not bool or type(exited) is not bool:
            process_complete = False
            diagnostics.append('process record lacks valid pid/owned/exited fields')
        if unity and process_mode not in ('apply', 'verify', 'recover'):
            process_complete = False
            diagnostics.append('Unity process mode is missing or invalid')
        if not unity and (not isinstance(executable, str) or not Path(executable).is_absolute() or not isinstance(process.get('startedUtc'), str)):
            process_complete = False
            diagnostics.append('batch process record lacks executable or start time')
        if exited is True:
            if type(process.get('exitCode')) is not int:
                process_complete = False
                diagnostics.append('exited process record lacks integer exitCode')
            else:
                exit_code = process['exitCode']
                process_state = 'recorded_exit'
                process_native_gate = bool(type(pid) is int and pid > 0 and owned is True and exit_code == 0)
        elif exited is False:
            process_state = 'process state unverified'
        if owned is not True:
            diagnostics.append('process ownership is not recorded as owned')
            process_complete = False
        if unity:
            # v1 unity.run did not persist its editor executable path.
            diagnostics.append('Unity v1 process record does not include the Editor executable path')
            process_complete = False

    if unity:
        report_name = 'native-'+str(process_mode or 'apply')+'.json'
        report_path = root/report_name
        report = json_file(report_name, 16*1024**2)
        if report is None and isinstance(journal.get('nativeReadback'), dict):
            report = journal['nativeReadback']
            report_path = root/'journal.json'
    else:
        report_name = 'native-result.json'
        report_path = root/report_name
        report = json_file(report_name, 32*1024**2)
        if report is None and isinstance(journal.get('native'), dict):
            report = journal['native']
            report_path = root/'journal.json'
    native_verified = False
    if not isinstance(report, dict):
        diagnostics.append('native report is unavailable')
    else:
        if report.get('planSha256') != expected_plan_sha:
            diagnostics.append('native report plan identity mismatch')
            blocked = True
        if unity:
            if report.get('operation') != operation or report.get('mode') != process_mode:
                diagnostics.append('native report operation/mode mismatch')
                blocked = True
        elif report.get('mode') != 'apply':
            diagnostics.append('batch native report mode mismatch')
            blocked = True
        if not unity and isinstance(journal.get('native'), dict):
            saved = journal['native']
            if any(saved.get(key) != report.get(key) for key in ('status', 'planSha256', 'mode')):
                diagnostics.append('journal native report differs from native-result.json')
                blocked = True
        if unity and isinstance(journal.get('nativeReadback'), dict):
            saved = journal['nativeReadback']
            if any(saved.get(key) != report.get(key) for key in ('status', 'planSha256', 'operation', 'mode')):
                diagnostics.append('journal native readback differs from native report')
                blocked = True
        native_verified = report.get('status') == 'verified' and process_native_gate and not blocked
        if report.get('status') != 'verified':
            diagnostics.append('native report does not record verified status')
        elif not process_native_gate:
            diagnostics.append('native report is not paired with an owned zero-exit process record')

    observed_play = bool(unity and operation == 'validate.play' and isinstance(report, dict)
                         and type(report.get('playFrames')) is int and report['playFrames'] > 0)
    play_mode_status = 'observedPlayMode' if observed_play else ('plannedPlayMode_unverified' if unity and operation == 'validate.play' else 'not_requested')
    kind = 'PlayMode' if observed_play else 'Editor'
    if process_state == 'process state unverified':
        diagnostics.append('running is recorded, but Editor process state is unverified; no marker-based attachment was attempted')
    if not process_complete or not native_verified:
        if not blocked:
            diagnostics.append('transaction evidence is incomplete')
    status = 'blocked' if blocked else ('ok' if process_complete and native_verified else 'partial')
    log_names = (['apply.log','apply-console.log','verify.log','verify-console.log','recover.log','recover-console.log']
                 if unity else ['editor.log','console.log'])
    logs = [str(root/name) for name in log_names if (root/name).is_file()]
    evidence = [str(root/name) for name in (plan_name, 'journal.json', 'process.json', report_name)
                if (root/name).is_file()]
    return dict(status=status, kind=kind, playModeStatus=play_mode_status,
                projectRoot=project_root, planSha256=expected_plan_sha, operation=operation,
                editorExecutable=executable, editorExecutableEvidence='recorded_unverified' if executable else 'not_recorded',
                pid=pid, owned=owned, exited=exited, processState=process_state, exitCode=exit_code,
                nativeStatus=report.get('status') if isinstance(report, dict) else 'unavailable',
                nativeVerified=native_verified, nativeReport=str(report_path) if report is not None else None,
                logPaths=logs, evidencePaths=evidence, diagnostics=diagnostics,
                connection='recorded transaction evidence only; no external Editor attachment or live GUI observation')
