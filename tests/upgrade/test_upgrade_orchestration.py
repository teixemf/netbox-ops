"""Exercise orchestration without touching systemd, Git or backups."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from netbox_ops import upgrade_orchestration as orchestration
from netbox_ops.upgrade_execution import UpgradeExecutionError


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = Mock()
        names = (
            'validate_execution_approval', 'pin_release_stages', 'SystemdServices',
            'write_audit', 'begin_maintenance', 'finish_maintenance', 'stop_upgrade_writers', '_capture_quiesced_recovery',
            'require_quiesced_writers', 'run_stage', 'start_upgrade_writers',
            'resolve_base_python', 'read_netbox_health', 'wait_for_netbox_health',
            'fetch_pinned_stage', 'inventory_plugins',
        )
        for name in names:
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.enterContext(patch.object(orchestration.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(orchestration, 'ExclusiveLock', nullcontext))
        self.calls.pin_release_stages.return_value = ('first', 'second')
        self.installation = SimpleNamespace(root=Path('/opt/netbox'), python_version='3.12.1')
        self.inventory = SimpleNamespace(requirements_hash='hash', enabled=(), packages=())
        self.plan = SimpleNamespace(
            source=SimpleNamespace(installation=self.installation, inventory=self.inventory,
                                   python_by_stage=(('4.4.1', '3.12.1'),)),
            current_netbox='4.3.9', target_netbox='4.4.1',
            stages=(SimpleNamespace(plugin_versions=()), SimpleNamespace(plugin_versions=())),
        )
        self.calls.read_netbox_health.return_value.version = self.plan.current_netbox
        self.calls.inventory_plugins.return_value = self.inventory
        self.calls._capture_quiesced_recovery.return_value.directory = Path(
            '/backup/pre-upgrade-test'
        )

    def execute(self):
        return orchestration.execute_netbox_upgrade(
            Mock(), self.plan, Mock(), rebuild_plan=self.calls.rebuild,
            health_url='https://netbox.example/api/status/',
        )

    def test_success_orders_backup_stages_and_health(self):
        calls = self.calls
        result = self.execute()
        self.assertIs(result, calls._capture_quiesced_recovery.return_value)
        names = [call[0] for call in calls.mock_calls]
        ordered = ('rebuild', 'validate_execution_approval', 'resolve_base_python', 'read_netbox_health',
                   'pin_release_stages',
                   'begin_maintenance', 'stop_upgrade_writers', '_capture_quiesced_recovery',
                   'fetch_pinned_stage', 'run_stage', 'inventory_plugins', 'start_upgrade_writers',
                   'wait_for_netbox_health', 'finish_maintenance')
        indices = [names.index(name) for name in ordered]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(calls.run_stage.call_count, 2)
        self.assertIs(calls.run_stage.call_args.args[0], calls.run_stage.return_value)

    def test_failure_never_starts_writers(self):
        for failure in (
            'validate_execution_approval', 'pin_release_stages', 'stop_upgrade_writers',
            '_capture_quiesced_recovery', 'fetch_pinned_stage', 'run_stage',
        ):
            with self.subTest(failure=failure):
                self.calls.reset_mock()
                operation = getattr(self.calls, failure)
                operation.side_effect = UpgradeExecutionError('failure')
                try:
                    with self.assertRaises(UpgradeExecutionError):
                        self.execute()
                    self.calls.start_upgrade_writers.assert_not_called()
                    self.calls.wait_for_netbox_health.assert_not_called()
                    self.calls.finish_maintenance.assert_not_called()
                    self.assertEqual(self.calls.run_stage.call_count,
                                     1 if failure == 'run_stage' else 0)
                finally:
                    operation.side_effect = None

    def test_health_failure_reports_recovery_without_secrets(self):
        self.calls.wait_for_netbox_health.side_effect = RuntimeError('secret password')
        with self.assertRaisesRegex(UpgradeExecutionError, 'functional health check') as error:
            self.execute()
        self.assertIn('pre-upgrade-test', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.calls.start_upgrade_writers.assert_called_once()
        self.calls.SystemdServices.return_value.stop.assert_called_once_with(
            ['netbox-rq', 'netbox']
        )
        self.assertIn('writers stopped', str(error.exception))

    def test_health_failure_with_failed_stop_reports_uncertain_state(self):
        self.calls.wait_for_netbox_health.side_effect = RuntimeError('health failed')
        self.calls.SystemdServices.return_value.stop.side_effect = RuntimeError('stop failed')
        with self.assertRaisesRegex(UpgradeExecutionError, 'maintenance unverified'):
            self.execute()
        self.calls.start_upgrade_writers.assert_called_once()

    def test_wrong_baseline_version_blocks_before_maintenance(self):
        self.calls.read_netbox_health.return_value.version = '4.1.0'
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.begin_maintenance.assert_not_called()
        self.calls.stop_upgrade_writers.assert_not_called()

    def test_plugin_version_drift_blocks_second_stage(self):
        self.plan.stages[0].plugin_versions = (('example-plugin', '1.0'),)
        with self.assertRaisesRegex(UpgradeExecutionError, 'stage 1'):
            self.execute()
        self.calls.run_stage.assert_called_once()
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_pending_marker_blocks_upgrade(self):
        self.calls.begin_maintenance.side_effect = UpgradeExecutionError('pending')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.stop_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()
