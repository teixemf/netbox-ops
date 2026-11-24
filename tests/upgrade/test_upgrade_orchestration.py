"""Exercise orchestration without touching systemd, Git or backups."""
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from netbox_ops import upgrade_orchestration as orchestration
from netbox_ops.upgrade_execution import UpgradeExecutionError


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = Mock()
        names = (
            'validate_execution_approval', 'pin_release_stages', 'SystemdServices',
            'write_audit', 'stop_upgrade_writers', '_capture_quiesced_recovery',
            'require_quiesced_writers', 'run_stage', 'start_upgrade_writers',
        )
        for name in names:
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.enterContext(patch.object(orchestration.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(orchestration, 'ExclusiveLock', nullcontext))
        self.calls.pin_release_stages.return_value = ('first', 'second')
        self.calls._capture_quiesced_recovery.return_value.directory = Path(
            '/backup/pre-upgrade-test'
        )

    def execute(self):
        return orchestration.execute_netbox_upgrade(
            Mock(), Mock(), Mock(), rebuild_plan=self.calls.rebuild,
            health_check=self.calls.health,
        )

    def test_success_orders_backup_stages_and_health(self):
        calls = self.calls
        result = self.execute()
        self.assertIs(result, calls._capture_quiesced_recovery.return_value)
        names = [call[0] for call in calls.mock_calls]
        ordered = ('rebuild', 'validate_execution_approval', 'pin_release_stages',
                   'stop_upgrade_writers', '_capture_quiesced_recovery',
                   'run_stage', 'start_upgrade_writers', 'health')
        indices = [names.index(name) for name in ordered]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(calls.run_stage.call_count, 2)
        self.assertIs(calls.run_stage.call_args.args[0], calls.run_stage.return_value)

    def test_failure_never_starts_writers(self):
        for failure in (
            'validate_execution_approval', 'pin_release_stages', 'stop_upgrade_writers',
            '_capture_quiesced_recovery', 'run_stage',
        ):
            with self.subTest(failure=failure):
                self.calls.reset_mock()
                operation = getattr(self.calls, failure)
                operation.side_effect = UpgradeExecutionError('failure')
                try:
                    with self.assertRaises(UpgradeExecutionError):
                        self.execute()
                    self.calls.start_upgrade_writers.assert_not_called()
                    self.calls.health.assert_not_called()
                    self.assertEqual(self.calls.run_stage.call_count,
                                     1 if failure == 'run_stage' else 0)
                finally:
                    operation.side_effect = None

    def test_health_failure_reports_recovery_without_secrets(self):
        self.calls.health.side_effect = RuntimeError('secret password')
        with self.assertRaisesRegex(UpgradeExecutionError, 'functional health check') as error:
            self.execute()
        self.assertIn('pre-upgrade-test', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.calls.start_upgrade_writers.assert_called_once()
