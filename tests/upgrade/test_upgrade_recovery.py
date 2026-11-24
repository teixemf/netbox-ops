"""Offline tests for protected NetBox upgrade recovery data."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.config import Configuration
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_recovery import _capture_quiesced_recovery


@unittest.skipUnless(os.geteuid() == 0, 'Recovery files require root ownership')
class UpgradeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / 'netbox'
        (self.root / 'netbox/netbox').mkdir(parents=True)
        self.secret = b"SECRET_KEY = 'never log this'\n"
        (self.root / 'netbox/netbox/configuration.py').write_bytes(self.secret)
        self.requirements = b'example-plugin==1.2.3\n'
        (self.root / 'local_requirements.txt').write_bytes(self.requirements)
        self.backups = base / 'backups'
        self.backups.mkdir(mode=0o700)
        self.cfg = Configuration(units={}, backup_directory=self.backups)
        self.plan = SimpleNamespace(
            source=SimpleNamespace(
                installation=SimpleNamespace(root=self.root, commit='a' * 40),
                inventory=SimpleNamespace(
                    requirements_hash=hashlib.sha256(self.requirements).hexdigest(),
                ),
            ),
            current_netbox='4.4.0', target_netbox='4.4.1',
            fingerprint='approved',
            stages=[SimpleNamespace(netbox_version='4.4.1')],
            plugins=[SimpleNamespace(package='example-plugin', installed_version='1.2.3')],
        )
        self.systemd = Mock()
        self.systemd.status.return_value = [
            SimpleNamespace(name=name, loaded='loaded', active='inactive')
            for name in ('netbox', 'netbox-rq')
        ]
        patcher = patch('netbox_ops.upgrade_recovery.BackupManager')
        self.manager_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = self.manager_class.return_value
        self.manager._backup.side_effect = self.create_backup
        self.manager.verify.return_value = {
            'kind': 'combined', 'netbox_version': '4.4.0', 'database': 'netbox',
        }

    def create_backup(self, kind):
        directory = self.manager_class.call_args.args[0].backup_directory
        archive = directory / 'netbox-combined-test.combined.tar.gz'
        for path in (archive, Path(str(archive) + '.json')):
            path.write_bytes(b'backup')
            path.chmod(0o600)
        return SimpleNamespace(path=archive, kind=kind)

    def capture(self):
        return _capture_quiesced_recovery(self.cfg, self.plan, systemd=self.systemd)

    def assert_incomplete(self):
        for directory in self.backups.iterdir():
            self.assertFalse((directory / 'recovery.json').exists())

    def test_capture_creates_private_verified_recovery(self):
        recovery = self.capture()
        self.assertEqual(recovery.directory.parent, self.backups)
        self.assertEqual(stat.S_IMODE(recovery.directory.stat().st_mode), 0o700)
        self.assertEqual((recovery.directory / 'configuration.py').read_bytes(), self.secret)
        self.assertEqual((recovery.directory / 'local_requirements.txt').read_bytes(),
                         self.requirements)
        for path in recovery.directory.iterdir():
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (0, 0))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        record = json.loads(recovery.manifest.read_text())
        self.assertEqual(record['git_commit'], 'a' * 40)
        self.assertEqual(record['plugin_versions'], [['example-plugin', '1.2.3']])
        self.assertEqual(record['archive'], recovery.archive.name)
        self.assertNotIn(self.secret.decode().strip(), recovery.manifest.read_text())
        self.manager._backup.assert_called_once_with('combined')
        self.manager.verify.assert_called_once_with('combined', recovery.archive)
        self.manager.backup.assert_not_called()
        self.systemd.stop.assert_not_called()
        self.systemd.start.assert_not_called()

    def test_active_writer_blocks_capture(self):
        self.systemd.status.return_value[0].active = 'active'
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.manager_class.assert_not_called()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_symlinked_configuration_is_rejected(self):
        config = self.root / 'netbox/netbox/configuration.py'
        config.unlink()
        config.symlink_to(self.root / 'local_requirements.txt')
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.manager_class.assert_not_called()
        self.assert_incomplete()

    def test_requirements_change_blocks_backup(self):
        (self.root / 'local_requirements.txt').write_bytes(b'changed')
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.manager_class.assert_not_called()
        self.assert_incomplete()

    def test_backup_failure_does_not_leak_secrets(self):
        self.manager._backup.side_effect = RuntimeError('never log this')
        with self.assertRaises(UpgradeExecutionError) as raised:
            self.capture()
        self.assertNotIn('never log this', str(raised.exception))
        self.manager.verify.assert_not_called()
        self.assert_incomplete()

    def test_invalid_verified_version_blocks_recovery(self):
        self.manager.verify.return_value['netbox_version'] = '4.3.0'
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.assert_incomplete()

    def test_insecure_backup_directory_is_rejected(self):
        self.backups.chmod(0o755)
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.manager_class.assert_not_called()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_untrusted_archive_path_is_rejected(self):
        self.manager._backup.side_effect = None
        self.manager._backup.return_value = SimpleNamespace(
            kind='combined', path=self.root / 'outside.combined.tar.gz',
        )
        with self.assertRaises(UpgradeExecutionError):
            self.capture()
        self.manager.verify.assert_not_called()
        self.assert_incomplete()


if __name__ == '__main__':
    unittest.main()
