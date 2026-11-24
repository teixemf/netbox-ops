"""Uninstall tests confined to a disposable synthetic filesystem."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.logrotate_policy import policy_text
from netbox_ops.uninstall import Layout, LAUNCHER, POLICY, UninstallError, plan, uninstall


@unittest.skipUnless(os.geteuid() == 0, 'root ownership checks')
class UninstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.layout = Layout(Path(temp.name))
        layout = self.layout
        for directory in (layout.app / 'netbox_ops', layout.launcher.parent, layout.link.parent,
                          layout.policy.parent, layout.config.parent, layout.logs, layout.data / 'backups', layout.lock.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (layout.app / 'netbox_ops.py').write_text('pass\n')
        (layout.app / 'tests/backups').mkdir(parents=True)
        (layout.app / 'tests/backups/test_backup.py').write_text('pass\n')
        layout.launcher.write_bytes(LAUNCHER)
        layout.link.symlink_to('/usr/local/sbin/netbox-ops')
        layout.policy.write_bytes(POLICY)
        layout.config.write_text('{}')
        layout.config.chmod(0o600)
        self.log = layout.logs / 'operations.log'
        self.log.write_text('audit')
        self.log.chmod(0o600)
        self.backup = layout.data / 'backups' / 'backup.dump'
        self.backup.write_text('backup')
        self.unrelated = layout.root / 'opt/netbox'
        self.unrelated.mkdir()
        (self.unrelated / 'configuration.py').write_text('untouched')
        self.proc = patch('netbox_ops.uninstall.check_processes')
        self.proc.start()
        self.addCleanup(self.proc.stop)

    def execute(self, *responses):
        iterator = iter(responses)
        with patch('builtins.print'):
            uninstall(self.layout, ask=lambda _: next(iterator))

    def test_default_and_repeat_preserve_data(self):
        self.assertEqual(POLICY, policy_text().encode())
        layout = self.layout
        self.execute('UNINSTALL', '', '')
        for path in (layout.app, layout.launcher, layout.link, layout.policy):
            self.assertFalse(path.exists() or path.is_symlink())
        self.assertEqual(layout.config.read_text(), '{}')
        self.assertEqual(self.log.read_text(), 'audit')
        self.assertEqual(self.backup.read_text(), 'backup')
        self.assertTrue((self.unrelated / 'configuration.py').exists())
        self.execute('UNINSTALL', '', '')
        self.assertTrue(layout.lock.exists())

    def test_separate_optional_cleanup_preserves_unknown_files_and_backups(self):
        layout = self.layout
        other = layout.logs / 'local-note'
        other.write_text('keep')
        rotated = layout.logs / 'operations.log.2.gz'
        rotated.write_bytes(b'audit')
        rotated.chmod(0o600)
        self.execute('UNINSTALL', 'REMOVE CONFIG', 'REMOVE LOGS', 'CONFIRM')
        self.assertFalse(layout.config.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse(rotated.exists())
        self.assertTrue(other.exists())
        self.assertTrue(self.backup.exists())

    def test_decline_does_not_change_files(self):
        self.execute('no')
        self.assertTrue(self.layout.app.exists())
        self.assertFalse(self.layout.lock.exists())
        self.execute('UNINSTALL', 'REMOVE CONFIG', '', 'no')
        self.assertTrue(self.layout.config.exists())
        self.assertTrue(self.layout.app.exists())

    def test_busy_lock_blocks_removal(self):
        with self.layout.lock.open('w') as stream:
            self.layout.lock.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(UninstallError, 'operation is running'):
                self.execute('UNINSTALL', '', '')
        self.assertTrue(self.layout.app.exists())

    def test_unknown_link_and_custom_policy_are_never_removed(self):
        layout = self.layout
        layout.link.unlink()
        layout.link.symlink_to('/usr/sbin/other')
        layout.launcher.write_text('another application')
        layout.policy.write_text('custom policy')
        self.execute('UNINSTALL', '', '')
        self.assertTrue(layout.link.is_symlink())
        self.assertEqual(layout.launcher.read_text(), 'another application')
        self.assertEqual(layout.policy.read_text(), 'custom policy')

    def test_active_session_blocks_removal(self):
        from netbox_ops.uninstall import check_processes
        check_processes.side_effect = UninstallError('netbox-ops is still running')
        with self.assertRaisesRegex(UninstallError, 'still running'):
            self.execute('UNINSTALL', '', '')
        self.assertTrue(self.layout.app.exists())
        self.assertTrue(self.layout.launcher.exists())

    def test_optional_log_cleanup_refuses_configured_backup_location(self):
        layout = self.layout
        layout.config.write_text(json.dumps({'backup': {'directory': str(layout.logs / 'custom')}}))
        with self.assertRaisesRegex(UninstallError, 'protected data'):
            self.execute('UNINSTALL', '', 'REMOVE LOGS')
        self.assertTrue(layout.app.exists())
        self.assertTrue(self.log.exists())

    def test_unexpected_app_symlink_and_mount_block_all_removal(self):
        layout = self.layout
        link = layout.app / 'netbox_ops/foreign'
        link.symlink_to(self.unrelated, target_is_directory=True)
        with self.assertRaisesRegex(UninstallError, 'symbolic link'):
            self.execute('UNINSTALL', '', '')
        link.unlink()
        with self.assertRaisesRegex(UninstallError, 'Mounted filesystem'):
            plan(layout, mounts=[layout.app / 'netbox_ops'])
        self.assertTrue(layout.launcher.exists())
        self.assertTrue(self.unrelated.exists())

    def test_configured_backup_in_app_blocks_uninstall(self):
        layout = self.layout
        layout.config.write_text(json.dumps({'backup': {'directory': str(layout.app / 'custom')}}))
        with self.assertRaisesRegex(UninstallError, 'protected data'):
            self.execute('UNINSTALL', '', '')
        self.assertTrue(layout.app.exists())

    def test_backup_like_file_unknown_local_data_and_pending_recovery_block(self):
        layout = self.layout
        archive = layout.app / 'netbox_ops/snapshot.dump'
        archive.write_text('backup')
        with self.assertRaisesRegex(UninstallError, 'Possible backup'):
            plan(layout)
        archive.unlink()
        unknown = layout.app / 'local-data'
        unknown.mkdir()
        with self.assertRaisesRegex(UninstallError, 'Unknown local'):
            plan(layout)
        unknown.rmdir()
        (layout.app.parent / '.netbox-ops-handoff').mkdir()
        with self.assertRaisesRegex(UninstallError, 'recovery is pending'):
            plan(layout)
        self.assertTrue(layout.app.exists())

    def test_partial_install_with_normal_venv_symlink(self):
        layout = self.layout
        layout.link.unlink()
        layout.launcher.unlink()
        (layout.app / 'venv/bin').mkdir(parents=True)
        (layout.app / 'venv/bin/python').symlink_to('/usr/bin/python3')
        self.execute('UNINSTALL', '', '')
        self.assertFalse(layout.app.exists())
        self.assertTrue(self.backup.exists())
