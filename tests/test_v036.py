"""Regression tests for chronological backups, restorable full archives and TUI UX."""
from __future__ import annotations

import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops import __version__
from netbox_ops.backups import BackupManager
from netbox_ops.cli import build_parser
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu
from test_tui import FakeScreen


class BackupFix036Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        (self.media / 'upload.txt').write_text('untouched')
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.root / 'backups',
                                 'netbox', 'netbox', self.media)
        self.manager = BackupManager(self.cfg)

    def test_version(self):
        self.assertEqual(__version__, '0.4.0')

    def test_reverse_chronological_order_across_types_and_pre_restore(self):
        self.cfg.backup_directory.mkdir()
        names = [
            'netbox-db-20261120T120000Z-abc.dump',
            'netbox-media-20261120T180000Z-abc.media.tar.gz',
            'netbox-full-20261120T090000Z-abc.full.tar.gz',
            'netbox-db-pre-restore-20261120T170000Z-abc.dump',
            'netbox-files-20261119T220000Z-abc.files.tar.gz',
        ]
        for idx, name in enumerate(names):
            (self.cfg.backup_directory / name).write_bytes(b'backups')
        expected = [names[i] for i in (1, 3, 0, 2, 4)]
        self.assertEqual([p.name for p in self.manager.list_backups()], expected)
        self.assertEqual([p.path.name for p in self.manager.quick_list()], expected)

    def test_legacy_fallback_uses_mtime_and_does_not_read_archive(self):
        self.cfg.backup_directory.mkdir()
        a = self.cfg.backup_directory / 'old.dump'
        b = self.cfg.backup_directory / 'new.dump'
        a.write_bytes(b'a')
        b.write_bytes(b'b')
        import os
        os.utime(a, (100, 100))
        os.utime(b, (200, 200))
        with patch('netbox_ops.backups._read_archive_metadata',
                   side_effect=AssertionError('Do not read archive')):
            self.assertEqual([x.name for x in self.manager.list_backups()],
                             ['new.dump', 'old.dump'])

    def test_full_only_db_stages_existing_extracted_dump_before_db_operations(self):
        # A real full TAR archive is built; DB and service actions are mocked.
        with patch('netbox_ops.backups._db_dump',
                   side_effect=lambda cfg, path: path.write_bytes(b'PGDMP' + b'x' * 96)), \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups.write_audit'), \
             patch('netbox_ops.backups._version', return_value='4.4.3'), \
             patch('netbox_ops.backups.SystemdServices') as systemd_for_create:
            systemd_for_create.return_value.status.return_value=[]
            source = self.manager.backup('full').path
        self.assertEqual((self.media / 'upload.txt').read_text(), 'untouched')
        systemd = MagicMock()
        systemd.status.return_value = []
        observed = []
        def stage_db(cfg, path):
            self.assertTrue(path.is_file(), f'Missing extracted PostgreSQL dump: {path}')
            self.assertEqual(path.read_bytes()[:5], b'PGDMP')
            observed.append(path.name)
            return 'nbo_stage_test'
        with patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._preflight_db'), \
             patch('netbox_ops.backups._stage_db', side_effect=stage_db), \
             patch('netbox_ops.backups._rename_db') as rename, \
             patch('netbox_ops.backups._terminate'), \
             patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._django_check'), \
             patch('netbox_ops.backups._version', return_value='4.4.3'), \
             patch('netbox_ops.backups.write_audit'), \
             patch.object(self.manager, '_backup') as safety, \
             patch('netbox_ops.backups._replace_artifacts') as replace:
            result = self.manager.restore('full', source, only='db')
        self.assertEqual(result.kind, 'full')
        self.assertEqual(observed, ['netbox.dump'])
        rename.assert_called()
        replace.assert_not_called()
        self.assertEqual(safety.call_count, 1)
        self.assertEqual(safety.call_args.args[0], 'db')
        self.assertEqual((self.media / 'upload.txt').read_text(), 'untouched')

    def test_legacy_archive_integrity_can_be_checked_but_not_restored(self):
        import hashlib
        import io
        import tarfile
        from netbox_ops.backups import BackupError
        self.cfg.backup_directory.mkdir()
        archive = self.cfg.backup_directory / 'netbox-files-20261119T120000Z-test.files.tar.gz'
        secret = b'legacy data (not to be restored)'
        manifest = {'tool': 'netbox-ops', 'format': 1, 'kind': 'files',
                    'artifacts': [{'id': 'ldap', 'kind': 'file'}],
                    'files': {'payload/ldap': {
                        'size': len(secret), 'sha256': hashlib.sha256(secret).hexdigest()}}}
        def make_archive(data):
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo('payload/ldap')
                info.mode, info.size = 0o600, len(data)
                tar.addfile(info, io.BytesIO(data))
                payload = json.dumps(manifest).encode('utf-8')
                info = tarfile.TarInfo('manifest.json')
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        make_archive(secret)
        self.assertEqual(self.manager.verify('legacy', archive)['kind'], 'files')
        # A valid legacy archive must NEVER become eligible for restoral.
        with self.assertRaisesRegex(BackupError, 'Invalid restore kind'):
            self.manager.restore('legacy', archive)
        make_archive(b'altered but same size bytes !!!!'[:len(secret)])
        with self.assertRaisesRegex(BackupError, 'checksum mismatch'):
            self.manager.verify('legacy', archive)

    def test_delete_all_is_not_exposed_in_cli_or_tui(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as error:
            parser.parse_args(['backup', 'delete-all'])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(hasattr(self.manager, 'delete_all'))
        labels = [Translator('pt')('nbo_act_' + name) for name in
                  ('create', 'verify', 'restore', 'delete', 'select_all', 'refresh')]
        self.assertIn('Validar', labels)
        self.assertNotIn('Apagar todos', labels)

    def test_validation_is_a_visible_second_action_and_executes(self):
        self.cfg.backup_directory.mkdir()
        path = self.cfg.backup_directory / 'netbox-db-20261120T130000Z-test.dump'
        path.write_bytes(b'PGDMP fake')
        Path(str(path) + '.json').write_text(json.dumps({
            'kind': 'db', 'tool': 'netbox-ops', 'format': 1,
            'netbox_version': '4.4.3', 'netbox_version_source': 'release.yaml'}))
        screen = FakeScreen([curses.KEY_DOWN, 9, curses.KEY_RIGHT, 10, 27, 27, 27],
                            dimensions=(35, 145))
        menu = CursesMenu(screen, Translator('pt'))
        called = []
        def runner(title, worker):
            called.append(title)
            worker(lambda *args: None)
            return True
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), \
             patch('netbox_ops.backups.BackupManager', return_value=self.manager), \
             patch.object(menu, '_run_backup_job', side_effect=runner), \
             patch.object(self.manager, 'verify', return_value={}) as verify:
            menu._backups()
        self.assertEqual(len(called), 1)
        verify.assert_called_once_with('db', path)
        self.assertIn('Validar', ' '.join(s for _, _, s, _ in screen.writes))


if __name__ == '__main__':
    unittest.main()
