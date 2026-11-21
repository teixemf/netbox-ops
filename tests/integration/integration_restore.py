"""Real restore integration tests on disposable PostgreSQL and media."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupError, BackupManager, _drop_db, _pg, _sql, _stage_db
from netbox_ops.config import Configuration, DEFAULT_UNITS


@unittest.skipUnless(
    os.environ.get('NBO_CI_PG_INTEGRATION') == '1'
    and os.environ.get('PGHOST', '').startswith('/tmp/nbo-pg-integration.'),
    'Requires the disposable PostgreSQL integration container',
)
class RealRestoreTests(unittest.TestCase):
    """Use the actual restore path; mock systemd, Django and audit output only."""

    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'Run only as root in the CI container')
        temp = tempfile.TemporaryDirectory(prefix='nbo-restore-it-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / 'media'
        (self.media / 'uploads').mkdir(parents=True)
        (self.media / 'empty').mkdir()
        self.media_a = b'\x00\xffOriginal media A\n'
        self.media_b = b'\xff\x00Changed media B\n'
        (self.media / 'uploads' / 'photo.bin').write_bytes(self.media_a)
        (self.media / 'readme.txt').write_bytes(b'Media state A\n')

        self.owner = 'nbo_owner_' + uuid.uuid4().hex[:10]
        self.database = 'nbo_it_' + uuid.uuid4().hex[:12]
        self.cfg = Configuration(
            DEFAULT_UNITS.copy(), self.root / 'backups',
            self.database, self.owner, self.media,
            postgres_port=int(os.environ['PGPORT']),
        )
        self.manager = BackupManager(self.cfg)

        _sql(f'CREATE ROLE "{self.owner}" LOGIN', cfg=self.cfg)
        self.addCleanup(self._cleanup_owned_databases)
        _pg('createdb', '--owner', self.owner, '--template', 'template0',
            '--encoding', 'UTF8', '--lc-collate', 'C', '--lc-ctype', 'C',
            self.database, cfg=self.cfg)
        _sql(
            'CREATE TABLE public.django_migrations '
            '(id bigint PRIMARY KEY, app text, name text); '
            'CREATE TABLE public.ci_marker '
            '(id integer PRIMARY KEY, marker text NOT NULL); '
            "INSERT INTO public.ci_marker VALUES (1, 'A')",
            self.database, cfg=self.cfg,
        )

        self.systemd = MagicMock()
        self.systemd.status.return_value = []
        for target, options in (
            ('netbox_ops.backups.SystemdServices', {'return_value': self.systemd}),
            ('netbox_ops.backups._version', {'return_value': '4.7.2'}),
            ('netbox_ops.backups.write_audit', {}),
        ):
            mocked = patch(target, **options)
            mocked.start()
            self.addCleanup(mocked.stop)

    def _cleanup_owned_databases(self):
        # Successful restores deliberately preserve nbo_prev_*; remove those too.
        owned = _sql(
            'SELECT datname FROM pg_database WHERE datdba = '
            f"(SELECT oid FROM pg_roles WHERE rolname = '{self.owner}')",
            cfg=self.cfg,
        )
        for database in owned.splitlines():
            _pg('dropdb', '--if-exists', '--force', database, cfg=self.cfg)
        _sql(f'DROP ROLE "{self.owner}"', cfg=self.cfg)

    def _marker(self, database=None):
        return _sql(
            'SELECT marker FROM public.ci_marker WHERE id = 1',
            database or self.database, cfg=self.cfg,
        )

    def _media_state(self):
        return {
            'photo': (self.media / 'uploads' / 'photo.bin').read_bytes(),
            'readme': (self.media / 'readme.txt').read_bytes(),
            'extra': (self.media / 'only-in-B.bin').read_bytes()
            if (self.media / 'only-in-B.bin').exists() else None,
            'empty_dir': (self.media / 'empty').is_dir(),
        }

    def _change_to_b(self):
        _sql("UPDATE public.ci_marker SET marker = 'B' WHERE id = 1",
             self.database, cfg=self.cfg)
        (self.media / 'uploads' / 'photo.bin').write_bytes(self.media_b)
        (self.media / 'readme.txt').write_bytes(b'Media state B\n')
        (self.media / 'only-in-B.bin').write_bytes(b'Only in state B\n')
        self.assertEqual(self._marker(), 'B')
        self.assertEqual(self._media_state()['photo'], self.media_b)

    def _verify_safety_db(self, source):
        metadata = self.manager.verify('db', source)
        self.assertEqual(metadata['purpose'], 'pre_restore')
        self.assertEqual(metadata['database'], self.database)
        self.assertEqual(
            metadata['sha256'], hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        verification_db = 'nbo_verify_' + uuid.uuid4().hex[:12]
        _pg('createdb', '--owner', self.owner, '--template', 'template0',
            '--encoding', 'UTF8', '--lc-collate', 'C', '--lc-ctype', 'C',
            verification_db, cfg=self.cfg)
        # The fixture cleanup also drops this database if an assertion fails.
        with source.open('rb') as dump:
            _pg('pg_restore', '--no-owner', '--no-acl', '--role', self.owner,
                '--exit-on-error', '--single-transaction',
                '--dbname', verification_db, stdin=dump, cfg=self.cfg)
        self.assertEqual(self._marker(verification_db), 'B')

    def _verify_safety_media(self, source):
        metadata = self.manager.verify('media', source)
        self.assertEqual(metadata['purpose'], 'pre_restore')
        with tarfile.open(source, 'r:gz') as archive:
            self.assertEqual(
                archive.extractfile('payload/media/uploads/photo.bin').read(),
                self.media_b,
            )
            self.assertEqual(
                archive.extractfile('payload/media/readme.txt').read(),
                b'Media state B\n',
            )
            self.assertEqual(
                archive.extractfile('payload/media/only-in-B.bin').read(),
                b'Only in state B\n',
            )

    def _exercise_restore(self, kind, only, expected_db, expected_media):
        self.assertEqual(self._marker(), 'A')
        self.assertEqual(self._media_state()['photo'], self.media_a)

        original = self.manager.backup(kind).path
        original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self.manager.verify(kind, original)
        self._change_to_b()

        stage_checks = []

        def check_staged_database(cfg, stage):
            self.assertEqual(cfg, self.cfg)
            self.assertRegex(stage, r'^nbo_stage_[0-9a-f]{16}$')
            # Django is mocked, but staging SQL and the unchanged active DB are real.
            self.assertEqual(self._marker(stage), 'A')
            self.assertEqual(
                _sql("SELECT to_regclass('public.django_migrations') IS NOT NULL",
                     stage, cfg=self.cfg),
                't',
            )
            self.assertEqual(self._marker(), 'B')
            stage_checks.append(stage)

        with patch('netbox_ops.backups._django_check',
                   side_effect=check_staged_database) as django:
            report = self.manager.restore(kind, original, only=only)

        restores_db = kind == 'db' or (kind == 'combined' and only != 'media')
        restores_media = kind == 'media' or (kind == 'combined' and only != 'db')
        self.assertEqual(report.kind, kind)
        self.assertEqual(self._marker(), expected_db)
        self.assertEqual(
            self._media_state(),
            {
                'photo': self.media_a if expected_media == 'A' else self.media_b,
                'readme': b'Media state A\n' if expected_media == 'A' else b'Media state B\n',
                'extra': None if expected_media == 'A' else b'Only in state B\n',
                'empty_dir': True,
            },
        )

        self.assertEqual(django.call_count, 1 if restores_db else 0)
        self.assertEqual(len(stage_checks), 1 if restores_db else 0)
        self.assertEqual(self.manager.backup_purpose(original), 'manual')
        self.assertEqual(
            hashlib.sha256(original.read_bytes()).hexdigest(), original_digest,
        )
        self.manager.verify(kind, original)

        db_safety = [
            p for p in self.manager.list_backups('db')
            if self.manager.backup_purpose(p) == 'pre_restore'
        ]
        media_safety = [
            p for p in self.manager.list_backups('media')
            if self.manager.backup_purpose(p) == 'pre_restore'
        ]
        self.assertEqual(len(db_safety), int(restores_db))
        self.assertEqual(len(media_safety), int(restores_media))
        if restores_db:
            self._verify_safety_db(db_safety[0])
            previous = re.search(r'Previous database preserved: (nbo_prev_[0-9a-f]{16})',
                                 report.detail)
            self.assertIsNotNone(previous)
            self.assertEqual(self._marker(previous.group(1)), 'B')
        else:
            self.assertNotIn('Previous database preserved', report.detail)
        if restores_media:
            self._verify_safety_media(media_safety[0])

        self.assertEqual(
            _sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'",
                 cfg=self.cfg), '0',
        )
        self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-restore-*')))
        self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-build-*')))
        self.assertFalse(list(self.root.glob('.netbox-ops-restore-*')))
        self.systemd.stop.assert_not_called()
        self.systemd.start.assert_not_called()

    def test_restore_database(self):
        self._exercise_restore('db', 'all', 'A', 'B')

    def test_restore_media(self):
        self._exercise_restore('media', 'all', 'B', 'A')

    def test_restore_full(self):
        self._exercise_restore('combined', 'all', 'A', 'A')

    def test_restore_full_database_only(self):
        self._exercise_restore('combined', 'db', 'A', 'B')

    def test_restore_full_media_only(self):
        self._exercise_restore('combined', 'media', 'B', 'A')


    def _assert_failed_restore_unchanged(self, expected_media):
        self.assertEqual(self._marker(), 'B')
        self.assertEqual(self._media_state(), expected_media)
        self.assertEqual(
            _sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'",
                 cfg=self.cfg), '0',
        )
        self.assertEqual(
            _sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_prev_%'",
                 cfg=self.cfg), '0',
        )
        self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-restore-*')))
        self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-build-*')))
        self.assertFalse(list(self.root.glob('.netbox-ops-restore-*')))
        for kind in ('db', 'media'):
            self.assertFalse(any(
                self.manager.backup_purpose(path) == 'pre_restore'
                for path in self.manager.list_backups(kind)
            ))
        self.systemd.stop.assert_not_called()
        self.systemd.start.assert_not_called()

    def _corrupt_archive_member(self, original, member_name):
        """Retain the original manifest but change one payload byte."""
        modified = self.root / ('corrupted-' + original.name)
        changed = False
        with tarfile.open(original, 'r:gz') as source, \
                tarfile.open(modified, 'w:gz') as target:
            for member in source:
                if not member.isfile():
                    target.addfile(member)
                    continue
                content = source.extractfile(member).read()
                if member.name == member_name:
                    self.assertTrue(content)
                    content = bytes([content[0] ^ 0xff]) + content[1:]
                    changed = True
                target.addfile(member, io.BytesIO(content))
        self.assertTrue(changed)
        return modified

    def test_corrupted_database_dump_is_rejected_without_changes(self):
        valid = self.manager.backup('db').path
        corrupt = self.root / 'corrupt.dump'
        # A PGDMP header alone must not be accepted as a valid custom dump.
        corrupt.write_bytes(valid.read_bytes()[:5] + b'not-a-valid-dump')
        self._change_to_b()
        expected_media = self._media_state()
        with self.assertRaises(BackupError):
            self.manager.restore('db', corrupt, accept_unknown=True)
        self._assert_failed_restore_unchanged(expected_media)

    def test_corrupted_media_and_full_archives_are_rejected(self):
        cases = (
            ('media', 'payload/media/uploads/photo.bin'),
            ('combined', 'database/netbox.dump'),
        )
        sources = [
            (kind, self._corrupt_archive_member(
                self.manager.backup(kind).path, member,
            ))
            for kind, member in cases
        ]
        self._change_to_b()
        expected_media = self._media_state()
        for kind, source in sources:
            with self.subTest(kind=kind):
                with self.assertRaises(BackupError):
                    self.manager.restore(kind, source)
                self._assert_failed_restore_unchanged(expected_media)

    def test_missing_files_and_wrong_archive_types_do_not_change_live_data(self):
        media_backup = self.manager.backup('media').path
        full_backup = self.manager.backup('combined').path
        self._change_to_b()
        expected_media = self._media_state()
        for kind, source in (
            ('db', self.root / 'nonexistent.dump'),
            ('media', self.root / 'nonexistent.media.tar.gz'),
            ('combined', self.root / 'nonexistent.combined.tar.gz'),
        ):
            with self.subTest(missing=kind):
                with self.assertRaises(FileNotFoundError):
                    self.manager.restore(kind, source)
                self._assert_failed_restore_unchanged(expected_media)
        for kind, source in (
            ('db', media_backup),
            ('media', full_backup),
            ('combined', media_backup),
        ):
            with self.subTest(wrong_kind=kind):
                with self.assertRaises(BackupError):
                    self.manager.restore(kind, source)
                self._assert_failed_restore_unchanged(expected_media)

    def test_missing_media_directory_leaves_no_partial_backup(self):
        self.assertEqual(self._marker(), 'A')
        shutil.rmtree(self.media)
        for kind in ('media', 'combined'):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(BackupError, 'Media directory|Required artifact'):
                    self.manager.backup(kind)
                self.assertFalse(self.manager.list_backups(kind))
                self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-build-*')))
                self.assertEqual(self._marker(), 'A')
        self.systemd.stop.assert_not_called()
        self.systemd.start.assert_not_called()

    def test_failed_real_pg_dump_cleans_partial_backups(self):
        # Missing DB causes the real pg_dump command to fail on both distros.
        absent = 'nbo_missing_' + uuid.uuid4().hex[:12]
        bad_config = dataclasses.replace(self.cfg, database=absent)
        manager = BackupManager(bad_config)
        for kind in ('db', 'combined'):
            with self.subTest(kind=kind):
                with self.assertRaises(BackupError):
                    manager.backup(kind)
                self.assertFalse(manager.list_backups(kind))
                self.assertFalse(list(self.cfg.backup_directory.glob('.nbo-build-*')))
                self.assertEqual(self._marker(), 'A')
        self.assertFalse(list(self.cfg.backup_directory.glob('*.json')))
        self.systemd.stop.assert_not_called()
        self.systemd.start.assert_not_called()

    def test_real_staging_failure_removes_stage_and_preserves_live_data(self):
        # A valid PostgreSQL dump without django_migrations fails after a real
        # createdb/pg_restore, before any live DB swap or service stop.
        source_db = 'nbo_source_' + uuid.uuid4().hex[:12]
        _pg('createdb', '--owner', self.owner, '--template', 'template0',
            '--encoding', 'UTF8', '--lc-collate', 'C', '--lc-ctype', 'C',
            source_db, cfg=self.cfg)
        _sql('CREATE TABLE ci_marker (id integer PRIMARY KEY, marker text); '
             "INSERT INTO ci_marker VALUES (1, 'A')", source_db, cfg=self.cfg)
        broken = self.root / 'no-migrations.dump'
        with broken.open('wb') as output:
            _pg('pg_dump', '--format=custom', '--no-owner', '--no-acl',
                '--dbname', source_db, stdout=output, cfg=self.cfg)
        self._change_to_b()
        expected_media = self._media_state()
        with self.assertRaisesRegex(BackupError, 'no django_migrations'):
            self.manager.restore('db', broken, accept_unknown=True)
        self._assert_failed_restore_unchanged(expected_media)

    def test_failed_django_check_drops_real_stage_before_changes(self):
        source = self.manager.backup('db').path
        self._change_to_b()
        expected_media = self._media_state()
        staged = []

        def reject_migrations(cfg, stage):
            staged.append(stage)
            self.assertEqual(self._marker(stage), 'A')
            self.assertEqual(self._marker(), 'B')
            raise BackupError('Django migration check failed')

        with patch('netbox_ops.backups._django_check',
                   side_effect=reject_migrations):
            with self.assertRaisesRegex(BackupError, 'Django migration check failed'):
                self.manager.restore('db', source)
        self.assertEqual(len(staged), 1)
        self._assert_failed_restore_unchanged(expected_media)

    def test_utf8_staging_in_sql_ascii_cluster_real_postgres(self):
        # Regression #24: query the actual cluster settings and stage a real dump.
        self.assertEqual(
            _sql("SELECT pg_encoding_to_char(encoding) "
                 "FROM pg_database WHERE datname = 'template0'", cfg=self.cfg),
            'SQL_ASCII',
        )
        source = self.manager.backup('db').path
        live_settings = _sql(
            "SELECT pg_encoding_to_char(encoding) || ':' || datcollate || ':' || datctype "
            f"FROM pg_database WHERE datname = '{self.database}'",
            cfg=self.cfg,
        )
        self.assertEqual(live_settings, 'UTF8:C:C')
        stage = _stage_db(self.cfg, source)
        try:
            self.assertEqual(self._marker(stage), 'A')
            self.assertEqual(
                _sql("SELECT pg_encoding_to_char(encoding) || ':' || datcollate || ':' "
                     "|| datctype FROM pg_database "
                     f"WHERE datname = '{stage}'", cfg=self.cfg),
                live_settings,
            )
            self.assertEqual(self._marker(), 'A')
        finally:
            _drop_db(stage, cfg=self.cfg)
        self.assertEqual(
            _sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'",
                 cfg=self.cfg), '0',
        )


if __name__ == '__main__':
    unittest.main()
