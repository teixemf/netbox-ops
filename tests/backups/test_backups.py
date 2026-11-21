"""Offline tests: no systemd or PostgreSQL changes."""
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupError, BackupManager, _validate_archive
from netbox_ops.config import DEFAULT_UNITS, Configuration, ConfigError, load_config
from netbox_ops.cli import build_parser, perform_backup_action
from netbox_ops.i18n import Translator


class FileBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.configfile = self.base / 'configuration.py'
        self.configfile.write_text("SECRET_KEY = 'EXAMPLE'\n")
        self.configfile.chmod(0o600)
        self.media = self.base / 'media'
        self.media.mkdir()
        self.photo = self.media / 'photo.txt'
        self.photo.write_text('original')
        self.store = self.base / 'backups'
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.store, 'netbox', 'netbox', self.media)
        self.ops = BackupManager(self.cfg)

    def _mock_ops(self):
        # Suppress actual external services and audit, not file-system changes
        sys = MagicMock()
        sys.status.return_value = []
        return (patch('netbox_ops.backups.SystemdServices', return_value=sys),
                patch('netbox_ops.backups._django_check'),
                patch('netbox_ops.backups.write_audit'),
                patch('netbox_ops.backups._version', return_value='4.4.1'))

    def test_files_archive_and_restore(self):
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            result = self.ops.backup('media')
        self.assertTrue(result.path.name.endswith('.media.tar.gz'))
        self.assertEqual(stat.S_IMODE(result.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o700)
        manifest = self.ops.verify('media', result.path)
        self.assertEqual(manifest['netbox_version'], '4.4.1')
        self.assertIn('payload/media/photo.txt', manifest['files'])
        self.assertNotIn('payload/configuration', manifest['files'])
        self.configfile.write_text('modified')
        self.photo.write_text('changed')
        with self._mock_ops()[0], self._mock_ops()[1], self._mock_ops()[2], self._mock_ops()[3]:
            report = self.ops.restore('media', result.path)
        self.assertEqual(report.kind, 'media')
        self.assertEqual(self.configfile.read_text(), 'modified')
        self.assertEqual(self.photo.read_text(), 'original')
        self.assertEqual(stat.S_IMODE(self.configfile.stat().st_mode), 0o600)
        self.assertEqual(len(self.ops.list_backups('media')), 2)  # includes pre-restore safety copy

    def test_sha_tamper_is_rejected_before_changes(self):
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            result = self.ops.backup('media')
        altered = self.base / 'altered.tar.gz'
        with tarfile.open(result.path, 'r:gz') as src, tarfile.open(altered, 'w:gz') as dst:
            for info in src:
                if info.name == 'payload/media/photo.txt':
                    data = b'corrupt!'
                    dst.addfile(info, io.BytesIO(data))
                else:
                    dst.addfile(info, src.extractfile(info) if info.isfile() else None)
        with self.assertRaisesRegex(BackupError, 'Checksum mismatch'):
            self.ops.verify('media', altered)
        self.assertIn('EXAMPLE', self.configfile.read_text())

    def test_refuses_symlink_artifacts(self):
        (self.media / 'link').symlink_to(self.configfile)
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            with self.assertRaisesRegex(BackupError, 'Symlink'):
                self.ops.backup('media')
        self.assertEqual(self.ops.list_backups('media'), [])

    def test_archive_rejects_path_traversal(self):
        malicious = self.base / 'malicious.tar.gz'
        manifest = {'tool': 'netbox-ops', 'format': 1, 'kind': 'media',
                    'artifacts': [{'id': 'media', 'kind': 'dir'}], 'files': {}}
        payload = json.dumps(manifest).encode()
        with tarfile.open(malicious, 'w:gz') as archive:
            entry = tarfile.TarInfo('payload/media/../../etc/passwd')
            entry.size = 0
            archive.addfile(entry, io.BytesIO(b''))
            note = tarfile.TarInfo('manifest.json')
            note.size = len(payload)
            archive.addfile(note, io.BytesIO(payload))
        with self.assertRaisesRegex(BackupError, 'Unsafe'):
            _validate_archive(malicious, 'media', self.cfg)

    def test_unknown_artifacts_are_blocked(self):
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            archive = self.ops.backup('media').path
        # A valid archive does not depend on any secret configuration artifact.
        self.assertEqual({a['id'] for a in self.ops.verify('media', archive)['artifacts']}, {'media'})

    def test_rejects_untrusted_root_and_no_extra_backups(self):
        self.store.mkdir(mode=0o777)
        self.store.chmod(0o777)
        with self.assertRaisesRegex(BackupError, '0700'):
            self.ops.backup('media')

    def test_media_restore_skips_django_migration_check(self):
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            source = self.ops.backup('media').path
        self.configfile.write_text('before restore')
        self.photo.write_text('before file')
        systemd = MagicMock()
        systemd.status.return_value = []
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._django_check', side_effect=AssertionError('must skip migration check')), \
             patch('netbox_ops.backups.write_audit'), \
             patch('netbox_ops.backups._version', return_value='4.4.1'):
            self.ops.restore('media', source)
        self.assertEqual(self.configfile.read_text(), 'before restore')
        self.assertEqual(self.photo.read_text(), 'original')

    def test_full_backup_restarts_services_after_archive_failure(self):
        active = MagicMock()
        active.name, active.active = 'netbox', 'active'
        inactive = MagicMock()
        inactive.name, inactive.active = 'netbox-rq', 'inactive'
        systemd = MagicMock()
        systemd.status.return_value = [active, inactive]
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._db_dump', side_effect=BackupError('disk full')), \
             patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            with self.assertRaisesRegex(BackupError, 'disk full'):
                self.ops.backup('full')
        systemd.stop.assert_called_once_with(['netbox'])
        systemd.start.assert_called_once_with(['netbox'])
        self.assertFalse(self.ops.list_backups('full'))

    def test_full_backup_quiesces_writers_and_restores_original_service_state(self):
        active = MagicMock(name='netbox')
        active.name, active.active = 'netbox', 'active'
        inactive = MagicMock(name='netbox-rq')
        inactive.name, inactive.active = 'netbox-rq', 'inactive'
        systemd = MagicMock()
        systemd.status.return_value = [active, inactive]
        def fake_dump(cfg, target):
            target.write_bytes(b'PGDMP_FAKE_ARCHIVE')
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._db_dump', side_effect=fake_dump), \
             patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            result = self.ops.backup('full')
        self.assertEqual(result.kind, 'full')
        systemd.stop.assert_called_once_with(['netbox'])
        systemd.start.assert_called_once_with(['netbox'])
        self.assertIn('database/netbox.dump', self.ops.verify('full', result.path)['files'])

    def test_db_restore_preserves_previous_database(self):
        source = self.base / 'external.dump'
        source.write_bytes(b'PGDMP_FAKE_ARCHIVE')
        systemd = MagicMock()
        systemd.status.return_value = []
        def fake_dump(cfg, target):
            target.write_bytes(b'PGDMP_SAFETY_DUMP')
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._db_dump', side_effect=fake_dump), \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._preflight_db'), \
             patch('netbox_ops.backups._stage_db', return_value='nbo_stage_fake'), \
             patch('netbox_ops.backups._rename_db') as rename, \
             patch('netbox_ops.backups._terminate'), \
             patch('netbox_ops.backups._django_check'), \
             patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            report = self.ops.restore('db', source, accept_unknown=True)
        self.assertIn('Previous database preserved', report.detail)
        self.assertEqual(rename.call_count, 2)
        self.assertEqual(rename.call_args_list[0].args[0], 'netbox')
        self.assertEqual(rename.call_args_list[1].args[1], 'netbox')
        self.assertEqual(len(self.ops.list_backups('db')), 1)

    def test_failed_migrations_leave_active_database_unchanged(self):
        source = self.base / 'external.dump'
        source.write_bytes(b'PGDMP_FAKE_ARCHIVE')
        systemd = MagicMock()
        systemd.status.return_value = []
        def fake_dump(cfg, target):
            target.write_bytes(b'PGDMP_SAFETY_DUMP')
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd), \
             patch('netbox_ops.backups._db_dump', side_effect=fake_dump), \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._preflight_db'), \
             patch('netbox_ops.backups._stage_db', return_value='nbo_stage_fake'), \
             patch('netbox_ops.backups._rename_db') as rename, \
             patch('netbox_ops.backups._terminate') as terminate, \
             patch('netbox_ops.backups._drop_db') as drop, \
             patch('netbox_ops.backups._django_check', side_effect=BackupError('migration mismatch')) as django, \
             patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            with self.assertRaisesRegex(BackupError, 'migration mismatch'):
                self.ops.restore('db', source, accept_unknown=True)
        django.assert_called_once_with(self.cfg, 'nbo_stage_fake')
        rename.assert_not_called()
        terminate.assert_not_called()
        systemd.stop.assert_not_called()
        drop.assert_called_once_with('nbo_stage_fake', cfg=self.cfg)
        self.assertEqual(self.ops.list_backups('db'), [])

    def test_db_archive_format_guard_precedes_psql(self):
        bad = self.base / 'bad.dump'
        bad.write_bytes(b'not-a-postgres-dump')
        with patch('netbox_ops.backups._run') as run:
            with self.assertRaisesRegex(BackupError, 'PGDMP'):
                self.ops.verify('db', bad)
            run.assert_not_called()
