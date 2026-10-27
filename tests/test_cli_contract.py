import contextlib
import io
import json
import unittest
from unittest.mock import patch

from beaver.cli import main


class MigrationResultContractTests(unittest.TestCase):
    def invoke(self, result):
        output = io.StringIO()
        argv = ['server', 'migration-apply', '--project', '/candidate', '--plan', '/plan',
                '--compose', '/compose', '--project-name', 'beaver-go-contract',
                '--owner', 'server', '--expected-state', '0' * 64]
        with patch('beaver.cli.dispatch', return_value=result), contextlib.redirect_stdout(output):
            code = main(argv)
        return code, json.loads(output.getvalue())

    def test_stale_database_state_is_nonzero_and_has_no_write_claim(self):
        code, result = self.invoke(dict(status='stale_plan', execution='not_run'))
        self.assertEqual(code, 3)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['actualChanges'], [])

    def test_unknown_ddl_result_retains_possible_mutation(self):
        code, result = self.invoke(dict(status='execution_unknown_or_partial',
                                        execution='attempted; inspect before retry',
                                        database='beaver_go_example_server'))
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['actualChanges'][0]['owner'], 'server')
        self.assertEqual(result['actualChanges'][0]['status'], 'execution_unknown_or_partial')

    def test_verified_migration_exposes_database_effect(self):
        code, result = self.invoke(dict(status='applied', execution='verified_migration_ledger_and_schema_state',
                                        database='beaver_go_example_server',
                                        stateBeforeSha256='1' * 64, stateAfterSha256='2' * 64))
        self.assertEqual(code, 0)
        self.assertEqual(result['actualChanges'][0]['stateAfterSha256'], '2' * 64)


class NativeChangesContractTests(unittest.TestCase):
    def invoke(self, command, action, data):
        output = io.StringIO()
        if action == 'apply':
            args = ['--plan', '/synthetic-plan', '--expected-plan', '0' * 64]
        else:
            args = ['--transaction', '/synthetic-transaction']
        argv = [command, action, *args, '--out', '/synthetic-output',
                '--editor-path', '/synthetic-editor']
        with patch('beaver.cli.dispatch', return_value=data) as dispatch, contextlib.redirect_stdout(output):
            code = main(argv)
        dispatch.assert_called_once()
        return code, json.loads(output.getvalue())

    def test_production_paths_survive_cli_serialization_and_native_verdict(self):
        changes = ['Assets/Game/Presentation/Window.cs',
                   'Assets/Game/Presentation/Panel.cs',
                   'Assets/Game/Gameplay/Feedback.cs',
                   'Assets/Game/Tests/PlayMode/FeedbackTests.cs']
        native = 'saved_and_reloaded_same_editor'
        code, result = self.invoke('production', 'apply',
                                   dict(status='verified', changes=changes, nativeReadback=native))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['contract'], 'beaver-result/1')
        self.assertEqual(result['actualChanges'], changes)
        self.assertEqual(result['data']['changes'], changes)
        self.assertEqual(result['verification']['native'], native)

    def test_batch_paths_and_object_readback_are_not_normalized(self):
        changes = ['Assets/Game/Window.prefab', 'Assets/Game/Window.prefab.meta']
        native = dict(status='verified', planSha256='1' * 64)
        code, result = self.invoke('batch', 'apply',
                                   dict(status='verified', changes=changes, nativeReadback=native))
        self.assertEqual(code, 0)
        self.assertEqual(result['actualChanges'], changes)
        self.assertEqual(result['verification']['native'], native)

    def test_recovery_retains_object_summary_instead_of_fabricating_paths(self):
        code, result = self.invoke('production', 'recover',
                                   dict(status='verified', restored=4,
                                        nativeReadback='restored_exact_bytes_and_imported'))
        self.assertEqual(code, 0)
        self.assertEqual(result['actualChanges'], [dict(operation='production.recover', restored=4)])
        self.assertNotIn('changes', result['data'])


if __name__ == '__main__':
    unittest.main()
