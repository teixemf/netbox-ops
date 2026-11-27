"""Exercise orchestration without touching systemd, Git or backups."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from netbox_ops import upgrade_orchestration as orchestration
from netbox_ops.upgrade_diagnostics import CommandFailure
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_git import PinnedStage
from netbox_ops.upgrade_health import NetBoxHealth
from netbox_ops.upgrade_runtime import LtreeEvidence


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = Mock()
        names = (
            'validate_execution_approval', 'pin_release_stages', 'SystemdServices',
            'write_audit', 'begin_maintenance', 'finish_maintenance', 'stop_upgrade_writers', '_capture_quiesced_recovery',
            'require_quiesced_writers', 'start_upgrade_writers',
            'resolve_base_python', 'read_netbox_health', 'wait_for_netbox_health',
            'inventory_plugins', 'validate_netbox_only_pins',
            'validate_upgrade_runtime', '_private_directory', 'preflight_plugin_upgrade', 'run_plugin_stage',
        )
        for name in names:
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.enterContext(patch.object(orchestration.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(orchestration, 'ExclusiveLock', nullcontext))
        self.calls.pin_release_stages.return_value = (
            PinnedStage('4.4.0', 'b' * 40), PinnedStage('4.4.1', 'c' * 40),
        )
        self.installation = SimpleNamespace(root=Path('/opt/netbox'), python_version='3.12.1')
        self.inventory = SimpleNamespace(requirements_hash='hash', enabled=(), packages=())
        self.plan = SimpleNamespace(
            source=SimpleNamespace(installation=self.installation, inventory=self.inventory,
                                   python_by_stage=(('4.4.1', '3.12.1'),)),
            current_netbox='4.3.9', target_netbox='4.4.1',
            stages=(SimpleNamespace(netbox_version='4.4.0', plugin_versions=()),
                    SimpleNamespace(netbox_version='4.4.1', plugin_versions=())),
        )
        self.plan.warnings = ()
        self.baseline = NetBoxHealth(self.plan.current_netbox, 'netbox', (), 1)
        self.calls.read_netbox_health.return_value = self.baseline
        for name in ('prepare_combined_stages', 'recheck_combined_stage', 'run_combined_stage'):
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.calls.prepare_combined_stages.return_value = ('first', 'second')
        self.calls.run_combined_stage.return_value = (self.installation, ())
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
        ordered = ('rebuild', 'validate_execution_approval', 'validate_netbox_only_pins', 'resolve_base_python',
                   'validate_upgrade_runtime', '_private_directory', 'read_netbox_health',
                   'pin_release_stages', 'prepare_combined_stages',
                   'begin_maintenance', 'stop_upgrade_writers', '_capture_quiesced_recovery',
                   'recheck_combined_stage', 'run_combined_stage', 'start_upgrade_writers',
                   'wait_for_netbox_health', 'finish_maintenance')
        indices = [names.index(name) for name in ordered]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(calls.run_combined_stage.call_count, 2)
        self.assertIs(calls.run_combined_stage.call_args.args[0], self.installation)

    def test_failure_never_starts_writers(self):
        for failure in (
            'validate_execution_approval', 'validate_netbox_only_pins', 'validate_upgrade_runtime',
            'pin_release_stages', 'stop_upgrade_writers',
            '_capture_quiesced_recovery', 'prepare_combined_stages', 'recheck_combined_stage', 'run_combined_stage',
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
                    self.assertEqual(self.calls.run_combined_stage.call_count,
                                     1 if failure == 'run_combined_stage' else 0)
                finally:
                    operation.side_effect = None

    def test_stage_failure_reports_only_reviewed_diagnostics(self):
        for message, visible in (
            ('NetBox upgrade script failed', True),
            ('NetBox upgrade script failed: secret token', False),
        ):
            with self.subTest(message=message):
                self.calls.run_combined_stage.side_effect = UpgradeExecutionError(message)
                with self.assertRaises(UpgradeExecutionError) as error:
                    self.execute()
                self.assertEqual('detail: NetBox upgrade script failed;' in str(error.exception), visible)
                self.assertNotIn('secret token', str(error.exception))
        self.calls.start_upgrade_writers.assert_not_called()

    def test_command_failure_reports_stage_and_safe_audit(self):
        self.calls.run_combined_stage.side_effect = CommandFailure('netbox.upgrade script', 'exit', code=17)
        with self.assertRaises(UpgradeExecutionError) as error:
            self.execute()
        message = str(error.exception)
        self.assertIn('exit status 17', message)
        self.assertIn('4.4.0', message)
        self.assertIn('pre-upgrade-test', message)
        self.assertIn('exit status 17', str(self.calls.write_audit.call_args))
        self.calls.start_upgrade_writers.assert_not_called()

    def test_interruption_during_stage_retains_recovery(self):
        self.calls.run_combined_stage.side_effect = KeyboardInterrupt
        with self.assertRaisesRegex(UpgradeExecutionError, 'interruption requested') as error:
            self.execute()
        self.assertIn('pre-upgrade-test', str(error.exception))
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_interruption_during_health_stops_both_writers(self):
        self.calls.wait_for_netbox_health.side_effect = KeyboardInterrupt
        with self.assertRaisesRegex(UpgradeExecutionError, 'writers stopped'):
            self.execute()
        self.assertEqual(self.calls.SystemdServices.return_value.stop.call_args_list,
                         [call(['netbox-rq']), call(['netbox'])])
        self.calls.finish_maintenance.assert_not_called()

    def test_health_failure_reports_recovery_without_secrets(self):
        self.calls.wait_for_netbox_health.side_effect = RuntimeError('secret password')
        with self.assertRaisesRegex(UpgradeExecutionError, 'functional health check') as error:
            self.execute()
        self.assertIn('pre-upgrade-test', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        self.calls.start_upgrade_writers.assert_called_once()
        self.assertEqual(self.calls.SystemdServices.return_value.stop.call_args_list,
                         [call(['netbox-rq']), call(['netbox'])])
        self.assertIn('writers stopped', str(error.exception))

    def test_health_failure_with_failed_stop_reports_uncertain_state(self):
        self.calls.wait_for_netbox_health.side_effect = RuntimeError('health failed')
        self.calls.SystemdServices.return_value.stop.side_effect = RuntimeError('stop failed')
        self.calls.require_quiesced_writers.side_effect = [None] * 4 + [UpgradeExecutionError('not stopped')]
        with self.assertRaisesRegex(UpgradeExecutionError, 'maintenance unverified'):
            self.execute()
        self.calls.start_upgrade_writers.assert_called_once()
        self.assertEqual(self.calls.SystemdServices.return_value.stop.call_args_list,
                         [call(['netbox-rq']), call(['netbox'])])

    def test_failed_start_quiesces_both_writers(self):
        self.calls.start_upgrade_writers.side_effect = RuntimeError('start failed')
        with self.assertRaisesRegex(UpgradeExecutionError, 'start writers; writers stopped'):
            self.execute()
        self.assertEqual(self.calls.SystemdServices.return_value.stop.call_args_list,
                         [call(['netbox-rq']), call(['netbox'])])
        self.calls.wait_for_netbox_health.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_ltree_native_migration_must_be_usable_before_restart(self):
        self.plan.stages = (SimpleNamespace(netbox_version='4.7.3', plugin_versions=()),)
        self.plan.target_netbox = '4.7.3'
        self.plan.dependency_reviews = (SimpleNamespace(ltree='available', ltree_identity='db'),)
        self.calls.pin_release_stages.return_value = (PinnedStage('4.7.3', 'b' * 40),)
        self.calls.prepare_combined_stages.return_value = ('prepared',)
        with patch.object(orchestration, 'probe_ltree', side_effect=[
            LtreeEvidence('available', identity='db'), LtreeEvidence('unusable', identity='db'),
        ]):
            with self.assertRaisesRegex(UpgradeExecutionError, 'ltree not usable after migrations'):
                self.execute()
        self.calls.run_combined_stage.assert_called_once()
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_ltree_database_drift_blocks_stage(self):
        self.plan.stages = (SimpleNamespace(netbox_version='4.7.3', plugin_versions=()),)
        self.plan.target_netbox = '4.7.3'
        self.plan.dependency_reviews = (SimpleNamespace(ltree='available', ltree_identity='db'),)
        self.calls.pin_release_stages.return_value = (PinnedStage('4.7.3', 'b' * 40),)
        self.calls.prepare_combined_stages.return_value = ('prepared',)
        with patch.object(orchestration, 'probe_ltree',
                          return_value=LtreeEvidence('available', identity='different')):
            with self.assertRaisesRegex(UpgradeExecutionError, 'ltree prerequisites changed'):
                self.execute()
        self.calls.run_combined_stage.assert_not_called()
        self.calls.start_upgrade_writers.assert_not_called()

    def test_wrong_baseline_version_blocks_before_maintenance(self):
        self.calls.read_netbox_health.return_value = replace(self.baseline, version='4.1.0')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.begin_maintenance.assert_not_called()
        self.calls.stop_upgrade_writers.assert_not_called()

    def test_plugin_version_drift_blocks_second_stage(self):
        self.calls.run_combined_stage.return_value = (self.installation, (('unexpected', '1.0'),))
        with self.assertRaisesRegex(UpgradeExecutionError, 'stage 1'):
            self.execute()
        self.calls.run_combined_stage.assert_called_once()
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_full_netbox_preflight_failure_keeps_services_running(self):
        self.calls.prepare_combined_stages.side_effect = UpgradeExecutionError('conflict')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.begin_maintenance.assert_not_called()
        self.calls.stop_upgrade_writers.assert_not_called()
        self.calls._capture_quiesced_recovery.assert_not_called()
        self.calls.run_combined_stage.assert_not_called()

    def test_pending_marker_blocks_upgrade(self):
        self.calls.begin_maintenance.side_effect = UpgradeExecutionError('pending')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.stop_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()


class PluginOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = Mock()
        names = (
            'validate_execution_approval', 'pin_release_stages', 'SystemdServices',
            'write_audit', 'begin_maintenance', 'finish_maintenance', 'stop_upgrade_writers',
            '_capture_quiesced_recovery', 'require_quiesced_writers',
            'start_upgrade_writers', 'resolve_base_python', 'read_netbox_health',
            'wait_for_netbox_health', 'inventory_plugins',
            'validate_netbox_only_pins', 'validate_upgrade_runtime', '_private_directory',
            'preflight_plugin_upgrade', 'run_plugin_stage',
        )
        for name in names:
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.enterContext(patch.object(orchestration.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(orchestration, 'ExclusiveLock', nullcontext))
        self.installation = SimpleNamespace(
            root=Path('/opt/netbox'), version='4.4.1', commit='a' * 40,
            python_version='3.12.1',
        )
        self.inventory = SimpleNamespace(requirements_hash='hash', enabled=('example_plugin',), packages=())
        self.plan = SimpleNamespace(
            source=SimpleNamespace(installation=self.installation, inventory=self.inventory,
                                   python_by_stage=(('4.4.1', '3.12.1'),)),
            current_netbox='4.4.1', target_netbox='4.4.1',
            stages=(SimpleNamespace(netbox_version='4.4.1', plugin_versions=(('example-plugin', '2.0.0'),)),),
        )
        self.baseline = NetBoxHealth('4.4.1', 'netbox', (('example_plugin', '1.0.0'),), 1)
        for name in ('prepare_combined_stages', 'recheck_combined_stage', 'run_combined_stage'):
            self.enterContext(patch.object(orchestration, name, getattr(self.calls, name)))
        self.calls.read_netbox_health.return_value = self.baseline
        self.calls.run_plugin_stage.return_value = (('example_plugin', '2.0.0'),)
        self.calls.inventory_plugins.return_value = self.inventory
        self.calls._capture_quiesced_recovery.return_value.directory = Path('/backup/pre-upgrade-test')

    def execute(self):
        return orchestration.execute_plugin_upgrade(
            Mock(), self.plan, Mock(), rebuild_plan=self.calls.rebuild,
            health_url='https://netbox.example/api/status/',
        )

    def test_plugin_execution_reuses_recovery_and_never_changes_checkout(self):
        result = self.execute()
        calls = self.calls
        self.assertIs(result, calls._capture_quiesced_recovery.return_value)
        names = [item[0] for item in calls.mock_calls]
        ordered = ('validate_execution_approval', 'preflight_plugin_upgrade',
                   'begin_maintenance', 'stop_upgrade_writers', '_capture_quiesced_recovery',
                   'run_plugin_stage', 'start_upgrade_writers', 'wait_for_netbox_health',
                   'finish_maintenance')
        positions = [names.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))
        calls.pin_release_stages.assert_not_called()
        calls.prepare_combined_stages.assert_not_called()
        calls.run_combined_stage.assert_not_called()
        self.assertEqual(calls._capture_quiesced_recovery.call_args.kwargs['pinned_stages'],
                         (PinnedStage('4.4.1', 'a' * 40),))
        self.assertEqual(calls.wait_for_netbox_health.call_args.args[2].plugins,
                         (('example_plugin', '2.0.0'),))

    def test_resolver_failure_never_enters_maintenance(self):
        self.calls.preflight_plugin_upgrade.side_effect = UpgradeExecutionError('conflict')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.begin_maintenance.assert_not_called()
        self.calls.stop_upgrade_writers.assert_not_called()
        self.calls.run_plugin_stage.assert_not_called()

    def test_backup_failure_never_installs_plugins(self):
        self.calls._capture_quiesced_recovery.side_effect = UpgradeExecutionError('backup failed')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.run_plugin_stage.assert_not_called()
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_installation_failure_leaves_recovery_and_no_restart(self):
        self.calls.run_plugin_stage.side_effect = RuntimeError('private package credentials')
        with self.assertRaisesRegex(UpgradeExecutionError, 'plugin installation') as caught:
            self.execute()
        self.assertIn('pre-upgrade-test', str(caught.exception))
        self.assertNotIn('private package credentials', str(caught.exception))
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_missing_loaded_plugin_blocks_restart(self):
        self.calls.run_plugin_stage.return_value = ()
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.start_upgrade_writers.assert_not_called()

    def test_failed_http_health_keeps_recovery_and_stops_writers(self):
        self.calls.wait_for_netbox_health.side_effect = UpgradeExecutionError('unhealthy')
        with self.assertRaisesRegex(UpgradeExecutionError, 'functional health check; writers stopped'):
            self.execute()
        self.assertEqual(self.calls.SystemdServices.return_value.stop.call_args_list,
                         [call(['netbox-rq']), call(['netbox'])])
        self.calls.finish_maintenance.assert_not_called()


class CombinedOrchestrationTests(unittest.TestCase):
    def setUp(self):
        PluginOrchestrationTests.setUp(self)
        self.plan.target_netbox = '4.6.1'
        self.plan.warnings = ()
        self.plan.stages = (
            SimpleNamespace(netbox_version='4.5.1', plugin_versions=(('example-plugin', '1.0.0'),)),
            SimpleNamespace(netbox_version='4.6.1', plugin_versions=(('example-plugin', '2.0.0'),)),
        )
        self.pinned = (PinnedStage('4.5.1', 'b' * 40), PinnedStage('4.6.1', 'c' * 40))
        self.calls.pin_release_stages.return_value = self.pinned
        self.prepared = ('prepared-first', 'prepared-final')
        self.calls.prepare_combined_stages.return_value = self.prepared
        self.intermediate = SimpleNamespace(root=self.installation.root, version='4.5.1')
        self.final = SimpleNamespace(root=self.installation.root, version='4.6.1')
        self.calls.run_combined_stage.side_effect = [
            (self.intermediate, self.baseline.plugins),
            (self.final, (('example_plugin', '2.0.0'),)),
        ]

    def execute(self):
        return orchestration.execute_combined_upgrade(
            Mock(), self.plan, Mock(), rebuild_plan=self.calls.rebuild,
            health_url='https://netbox.example/api/status/',
        )

    def test_single_recovery_and_sequential_combined_stages(self):
        calls = self.calls
        self.assertIs(self.execute(), calls._capture_quiesced_recovery.return_value)
        names = [item[0] for item in calls.mock_calls]
        ordered = ('prepare_combined_stages', 'begin_maintenance', 'stop_upgrade_writers',
                   '_capture_quiesced_recovery', 'recheck_combined_stage',
                   'run_combined_stage', 'start_upgrade_writers',
                   'wait_for_netbox_health', 'finish_maintenance')
        positions = [names.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))
        calls._capture_quiesced_recovery.assert_called_once()
        calls.stop_upgrade_writers.assert_called_once()
        calls.start_upgrade_writers.assert_called_once()
        self.assertEqual(calls.recheck_combined_stage.call_count, 2)
        self.assertEqual(calls.run_combined_stage.call_count, 2)
        self.assertIs(calls.run_combined_stage.call_args.args[0], self.intermediate)
        self.assertEqual(calls.run_combined_stage.call_args.args[2], 'prepared-final')
        calls.run_plugin_stage.assert_not_called()
        self.assertEqual(calls.wait_for_netbox_health.call_args.args[2].plugins,
                         (('example_plugin', '2.0.0'),))

    def test_final_preflight_conflict_stops_after_first_stage(self):
        self.calls.recheck_combined_stage.side_effect = [None, UpgradeExecutionError('conflict')]
        with self.assertRaisesRegex(UpgradeExecutionError,
                                    'completed stages: 1/2; last completed release: 4.5.1') as caught:
            self.execute()
        self.assertIn('stage 2 preflight', str(caught.exception))
        self.assertIn('pre-upgrade-test', str(caught.exception))
        self.calls.run_combined_stage.assert_called_once()
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()

    def test_full_preflight_failure_never_enters_maintenance(self):
        self.calls.prepare_combined_stages.side_effect = UpgradeExecutionError('conflict')
        with self.assertRaises(UpgradeExecutionError):
            self.execute()
        self.calls.begin_maintenance.assert_not_called()
        self.calls._capture_quiesced_recovery.assert_not_called()

    def test_intermediate_plugin_change_stops_before_second_stage(self):
        self.calls.run_combined_stage.side_effect = [(self.intermediate, (('example_plugin', '2.0.0'),))]
        with self.assertRaisesRegex(UpgradeExecutionError, 'completed stages: 0/2'):
            self.execute()
        self.calls.run_combined_stage.assert_called_once()
        self.calls.start_upgrade_writers.assert_not_called()

    def test_second_stage_failure_never_restarts_and_reports_recovery(self):
        self.calls.run_combined_stage.side_effect = [
            (self.intermediate, self.baseline.plugins), RuntimeError('private secret'),
        ]
        with self.assertRaisesRegex(UpgradeExecutionError, 'completed stages: 1/2') as caught:
            self.execute()
        self.assertNotIn('private secret', str(caught.exception))
        self.calls.start_upgrade_writers.assert_not_called()
        self.calls.finish_maintenance.assert_not_called()
