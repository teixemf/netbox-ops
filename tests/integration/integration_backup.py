"""Backup integration tests using real PostgreSQL in disposable CI containers."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupManager, _pg, _sql
from netbox_ops.config import Configuration, DEFAULT_UNITS


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(
    os.environ.get('NBO_CI_PG_INTEGRATION') == '1'
    and os.environ.get('PGHOST', '').startswith('/tmp/nbo-pg-integration.'),
    'Requires the disposable PostgreSQL integration container',
)
class RealBackupTests(unittest.TestCase):
    """Mock only systemd, audit output and NetBox version."""

    def setUp(self):
        self.assertEqual(os.geteuid(), 0)
        temporary = tempfile.TemporaryDirectory(prefix='nbo-backup-it-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.media = root / 'media'
        (self.media / 'uploads').mkdir(parents=True)
        (self.media / 'empty').mkdir()
        self.photo = b'\x00\xff\x01netbox-ops-media\n'
        (self.media / 'uploads' / 'photo.bin').write_bytes(self.photo)
        (self.media / 'readme.txt').write_text('Original media A\n')
        self.owner = 'nbo_owner_' + uuid.uuid4().hex[:10]
        self.database = 'nbo_it_' + uuid.uuid4().hex[:12]
        self.cfg = Configuration(
            DEFAULT_UNITS.copy(), root / 'backups',
            self.database, self.owner, self.media,
            postgres_port=int(os.environ['PGPORT']),
        )
        self.manager = BackupManager(self.cfg)
        _sql(f'CREATE ROLE "{self.owner}" LOGIN', cfg=self.cfg)
        self.addCleanup(self._clean_databases)
        self._create_database(self.database)
        _sql(
            'CREATE TABLE public.django_migrations '
            '(id bigint PRIMARY KEY, app text, name text); '
            'CREATE TABLE public.ci_marker '
            '(id integer PRIMARY KEY, marker text NOT NULL); '
            "INSERT INTO public.ci_marker VALUES (1, 'A')",
            database=self.database, cfg=self.cfg,
        )
        for target, options in (
            ('netbox_ops.backups._version', {'return_value': '4.7.2'}),
            ('netbox_ops.backups.write_audit', {}),
        ):
            mocked = patch(target, **options)
            mocked.start()
            self.addCleanup(mocked.stop)

    def _create_database(self, name):
        _pg('createdb', '--owner', self.owner, '--template', 'template0',
            '--encoding', 'UTF8', '--lc-collate', 'C', '--lc-ctype', 'C',
            name, cfg=self.cfg)

    def _clean_databases(self):
        _pg('dropdb', '--if-exists', '--force', self.database, cfg=self.cfg)
        _sql(f'DROP ROLE "{self.owner}"', cfg=self.cfg)

    def _verify_dump_data(self, path):
        """An actual pg_restore proves that the dump contains the marker A."""
        database = 'nbo_verify_' + uuid.uuid4().hex[:12]
        self._create_database(database)
        self.addCleanup(_pg, 'dropdb', '--if-exists', '--force', database, cfg=self.cfg)
        with path.open('rb') as source:
            _pg('pg_restore', '--no-owner', '--no-acl', '--role', self.owner,
                '--exit-on-error', '--single-transaction', '--dbname', database,
                stdin=source, cfg=self.cfg)
        self.assertEqual(
            _sql('SELECT marker FROM public.ci_marker WHERE id=1', database, cfg=self.cfg),
            'A',
        )
        self.assertEqual(
            _sql("SELECT to_regclass('public.django_migrations') IS NOT NULL",
                 database, cfg=self.cfg), 't',
        )

    def _check_permissions(self, path):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(path) + '.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cfg.backup_directory.stat().st_mode), 0o700)

    def test_database_backup(self):
        report = self.manager.backup('db')
        self._check_permissions(report.path)
        self.assertEqual(report.path.read_bytes()[:5], b'PGDMP')
        metadata = self.manager.verify('db', report.path)
        self.assertEqual(metadata['kind'], 'db')
        self.assertEqual(metadata['database'], self.database)
        self.assertEqual(metadata['netbox_version'], '4.7.2')
        self.assertEqual(metadata['sha256'], sha256(report.path.read_bytes()))
        self.assertEqual(
            json.loads(Path(str(report.path) + '.json').read_text()), metadata,
        )
        self._verify_dump_data(report.path)
        self.assertEqual(
            _sql('SELECT marker FROM public.ci_marker WHERE id=1',
                 self.database, cfg=self.cfg), 'A',
        )
        self.assertEqual(self.manager.list_backups('db'), [report.path])

    def test_media_backup(self):
        report = self.manager.backup('media')
        self._check_permissions(report.path)
        manifest = self.manager.verify('media', report.path)
        self.assertEqual(manifest['kind'], 'media')
        self.assertEqual(manifest['purpose'], 'manual')
        self.assertEqual({a['id'] for a in manifest['artifacts']}, {'media'})
        self.assertEqual(set(manifest['files']), {
            'payload/media/readme.txt', 'payload/media/uploads/photo.bin',
        })
        with tarfile.open(report.path, 'r:gz') as archive:
            self.assertIn('payload/media/empty', archive.getnames())
            for name, expected in (
                ('payload/media/readme.txt', b'Original media A\n'),
                ('payload/media/uploads/photo.bin', self.photo),
            ):
                self.assertEqual(archive.extractfile(name).read(), expected)
                self.assertEqual(manifest['files'][name]['size'], len(expected))
                self.assertEqual(manifest['files'][name]['sha256'], sha256(expected))
        self.assertEqual(self.manager.list_backups('media'), [report.path])

    def test_combined_backup(self):
        systemd = MagicMock()
        systemd.status.return_value = []
        with patch('netbox_ops.backups.SystemdServices', return_value=systemd):
            report = self.manager.backup('combined')
        systemd.status.assert_called_once_with(['netbox', 'netbox-rq'])
        systemd.stop.assert_not_called()
        systemd.start.assert_not_called()
        self._check_permissions(report.path)
        manifest = self.manager.verify('combined', report.path)
        self.assertEqual(manifest['kind'], 'combined')
        self.assertEqual(set(manifest['files']), {
            'database/netbox.dump',
            'payload/media/readme.txt', 'payload/media/uploads/photo.bin',
        })
        with tarfile.open(report.path, 'r:gz') as archive:
            dump_bytes = archive.extractfile('database/netbox.dump').read()
            self.assertTrue(dump_bytes.startswith(b'PGDMP'))
            self.assertEqual(
                manifest['files']['database/netbox.dump']['sha256'], sha256(dump_bytes),
            )
            self.assertEqual(
                archive.extractfile('payload/media/uploads/photo.bin').read(), self.photo,
            )
        extracted = report.path.parent.parent / 'combined-dump.dump'
        extracted.write_bytes(dump_bytes)
        self._verify_dump_data(extracted)
        self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-build-*')))
        self.assertEqual(self.manager.list_backups('combined'), [report.path])


if __name__ == '__main__':
    unittest.main()
