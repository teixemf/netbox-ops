"""Read-only runtime preflight tests with all external processes mocked."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.config import Configuration
from netbox_ops.upgrade_execution import UpgradeExecutionError, validate_netbox_only_pins
from netbox_ops.upgrade_runtime import validate_upgrade_runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        media = base / 'media'
        media.mkdir()
        backups = base / 'backups'
        backups.mkdir()
        self.cfg = Configuration(
            units={name: name + '.service' for name in
                   ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')},
            media_directory=media, backup_directory=backups,
        )
        root = Path('/opt/netbox')
        self.installation = SimpleNamespace(root=root, python=root / 'venv/bin/python')
        self.runtime = SimpleNamespace(
            python=self.installation.python, working_directory=root / 'netbox',
            username='netbox', environment={'SECRET': 'never log this'},
        )
        self.discover = Mock(return_value=self.runtime)
        self.sql = Mock(return_value='netbox|5432|1234.5')
        self.runner = Mock(return_value=SimpleNamespace(
            returncode=0, stdout='netbox|5432|1234.5',
        ))
        self.enterContext(patch('netbox_ops.upgrade_runtime.pwd.getpwnam', return_value=
                                SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name='netbox')))
        self.enterContext(patch('netbox_ops.upgrade_runtime.os.getgrouplist', return_value=[1001]))

    def verify(self):
        validate_upgrade_runtime(
            self.cfg, self.installation, Path('/usr/bin/python3.12'), ('example_plugin',),
            runner=self.runner, discover=self.discover, sql=self.sql,
        )

    def test_checks_both_writers_and_exact_upgrade_environment(self):
        self.verify()
        self.assertEqual(self.discover.call_count, 2)
        self.assertEqual(self.discover.call_args_list[1].args[0].units['netbox'],
                         'netbox-rq.service')
        self.assertEqual(self.runner.call_count, 4)
        for call in self.runner.call_args_list:
            self.assertEqual(call.kwargs['user'], 1001)
            self.assertEqual(call.kwargs['env']['NBO_EXPECTED_DB'], 'netbox')
            self.assertEqual(call.kwargs['env']['NBO_EXPECTED_MEDIA'], str(self.cfg.media_directory))
            self.assertEqual(call.kwargs['env']['NBO_EXPECTED_PLUGINS'], '["example_plugin"]')
        self.assertIn('SECRET', self.runner.call_args_list[0].kwargs['env'])
        self.assertNotIn('SECRET', self.runner.call_args_list[1].kwargs['env'])
        self.assertEqual(self.runner.call_args_list[1].kwargs['env']['PYTHON'],
                         '/usr/bin/python3.12')

    def test_mismatched_database_identity_blocks_upgrade(self):
        self.runner.return_value.stdout = 'netbox|5432|different-server'
        with self.assertRaises(UpgradeExecutionError):
            self.verify()
        self.assertEqual(self.runner.call_count, 1)

    def test_unexpected_service_python_blocks_probe(self):
        self.runtime.python = Path('/usr/bin/python3')
        with self.assertRaises(UpgradeExecutionError):
            self.verify()
        self.runner.assert_not_called()

    def test_duplicate_unit_blocks_all_probes(self):
        self.cfg.units['netbox-rq'] = self.cfg.units['postgresql']
        with self.assertRaises(UpgradeExecutionError):
            self.verify()
        self.sql.assert_not_called()
        self.discover.assert_not_called()

    def test_nonstandard_root_blocks_mismatched_backup_version_detection(self):
        self.installation.root = Path('/srv/netbox')
        with self.assertRaises(UpgradeExecutionError):
            self.verify()
        self.sql.assert_not_called()

    def test_root_service_user_rejected(self):
        with patch('netbox_ops.upgrade_runtime.pwd.getpwnam',
                   return_value=SimpleNamespace(pw_uid=0, pw_gid=0, pw_name='root')):
            with self.assertRaises(UpgradeExecutionError):
                self.verify()
        self.runner.assert_not_called()

    def test_probe_failure_does_not_disclose_environment(self):
        self.runner.side_effect = OSError('never log this')
        with self.assertRaises(UpgradeExecutionError) as caught:
            self.verify()
        self.assertNotIn('never log this', str(caught.exception))


class NetBoxOnlyPinsTests(unittest.TestCase):
    def setUp(self):
        self.package = SimpleNamespace(name='example-plugin', version='1.2.3',
                                       modules=('example_plugin',))
        self.requirement = SimpleNamespace(name='example-plugin', version='1.2.3', kind='pinned')
        self.inventory = SimpleNamespace(enabled=('example_plugin',), packages=(self.package,),
                                         requirements=(self.requirement,))
        self.plan = SimpleNamespace(
            source=SimpleNamespace(inventory=self.inventory),
            stages=(SimpleNamespace(plugin_versions=(('example-plugin', '1.2.3'),)),),
        )

    def test_matching_pins_allow_netbox_only_upgrade(self):
        validate_netbox_only_pins(self.plan)

    def test_unpinned_plugin_is_not_implicitly_upgraded(self):
        self.requirement.kind = 'bare'
        with self.assertRaisesRegex(UpgradeExecutionError, 'Pin configured plugins'):
            validate_netbox_only_pins(self.plan)

    def test_wrong_pin_and_unmapped_plugin_are_rejected(self):
        self.requirement.version = '2.0.0'
        with self.assertRaises(UpgradeExecutionError):
            validate_netbox_only_pins(self.plan)
        self.inventory.packages = ()
        with self.assertRaises(UpgradeExecutionError):
            validate_netbox_only_pins(self.plan)

    def test_unknown_dynamic_configuration_requires_review(self):
        self.inventory.enabled = None
        with self.assertRaises(UpgradeExecutionError):
            validate_netbox_only_pins(self.plan)


if __name__ == '__main__':
    unittest.main()
