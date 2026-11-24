"""Exercise stage execution without running NetBox or systemd."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_git import PinnedStage
from netbox_ops.upgrade_stage import run_stage


class StageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'upgrade.sh').touch()
        self.old = SimpleNamespace(root=root, commit='a' * 40,
                                   python=root / 'venv/bin/python',
                                   python_version='3.12.1', version='4.3.9')
        self.stage = PinnedStage('4.4.1', 'b' * 40)
        self.live = SimpleNamespace(**vars(self.old))
        self.live.commit = self.stage.commit
        self.live.version = self.stage.version
        self.inspector = Mock(return_value=self.live)
        self.checkout = Mock()
        self.base_python = Mock(return_value=Path('/usr/bin/python3.12'))
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.systemd = Mock()
        self.systemd.status.return_value = [
            SimpleNamespace(name=name, loaded='loaded', active='inactive')
            for name in ('netbox', 'netbox-rq')
        ]

    def run_upgrade(self):
        return run_stage(self.old, self.stage, systemd=self.systemd,
                         runner=self.runner, inspector=self.inspector,
                         checkout=self.checkout, base_python=self.base_python)

    def test_success_uses_selected_python_and_checks_migrations(self):
        self.assertIs(self.run_upgrade(), self.live)
        calls = self.runner.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs['env']['PYTHON'], '/usr/bin/python3.12')
        self.assertEqual(calls[1].args[0][0], str(self.old.python))
        self.assertEqual(calls[2].args[0][-2:], ['migrate', '--check'])
        self.systemd.start.assert_not_called()

    def test_missing_base_python_blocks_checkout(self):
        self.base_python.side_effect = UpgradeExecutionError('base Python unavailable')
        with self.assertRaises(UpgradeExecutionError):
            self.run_upgrade()
        self.checkout.assert_not_called()
        self.runner.assert_not_called()

    def test_active_writer_blocks_checkout(self):
        self.systemd.status.return_value[0].active = 'active'
        with self.assertRaises(UpgradeExecutionError):
            self.run_upgrade()
        self.checkout.assert_not_called()
        self.runner.assert_not_called()

    def test_python_drift_blocks_script(self):
        self.live.python_version = '3.13.1'
        with self.assertRaises(UpgradeExecutionError):
            self.run_upgrade()
        self.runner.assert_not_called()

    def test_script_failure_blocks_checks(self):
        self.runner.return_value.returncode = 1
        with self.assertRaisesRegex(UpgradeExecutionError, 'upgrade script failed'):
            self.run_upgrade()
        self.assertEqual(self.runner.call_count, 1)
        self.systemd.start.assert_not_called()

    def test_timeout_redacts_command(self):
        self.runner.side_effect = subprocess.TimeoutExpired('secret', 3600)
        with self.assertRaises(UpgradeExecutionError) as caught:
            self.run_upgrade()
        self.assertNotIn('secret', str(caught.exception))

    def test_offline_check_failure_blocks_migration_check(self):
        self.runner.side_effect = [
            SimpleNamespace(returncode=0), SimpleNamespace(returncode=1),
        ]
        with self.assertRaisesRegex(UpgradeExecutionError, 'Django system check failed'):
            self.run_upgrade()
        self.assertEqual(self.runner.call_count, 2)

    def test_symlinked_script_blocks_execution(self):
        script = self.old.root / 'upgrade.sh'
        script.unlink()
        script.symlink_to('/bin/true')
        with self.assertRaises(UpgradeExecutionError):
            self.run_upgrade()
        self.runner.assert_not_called()
