"""Regression tests for PostgreSQL staging database settings (no live PostgreSQL)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.backups import BackupError, _stage_database_options, _stage_db
from netbox_ops.config import Configuration, DEFAULT_UNITS


class StageDatabaseLocaleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Configuration(DEFAULT_UNITS.copy())

    def _settings(self, *, encoding='UTF8', collate='C', ctype='C',
                  provider='c', icu_locale=None):
        return json.dumps({
            'encoding': encoding, 'collate': collate, 'ctype': ctype,
            'provider': provider, 'icu_locale': icu_locale,
        })

    def test_stage_uses_netbox_utf8_even_when_template0_is_sql_ascii(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / 'netbox.dump'
            dump.write_bytes(b'PGDMP_test')
            calls = []

            def fake_pg(*args, **kwargs):
                calls.append((args, kwargs))
                return ''

            def fake_sql(statement, database='postgres', cfg=None):
                if 'FROM pg_database AS d' in statement:
                    self.assertIn("d.datname = 'netbox'", statement)
                    return self._settings(encoding='UTF8', collate='C', ctype='C')
                self.assertIn('django_migrations', statement)
                return 't'

            with patch('netbox_ops.backups._pg', side_effect=fake_pg), \
                 patch('netbox_ops.backups._sql', side_effect=fake_sql):
                stage = _stage_db(self.cfg, dump)

        self.assertRegex(stage, r'^nbo_stage_[0-9a-f]{16}$')
        command = calls[0][0]
        self.assertEqual(command[0], 'createdb')
        self.assertIn('template0', command)
        self.assertEqual(command[command.index('--encoding') + 1], 'UTF8')
        self.assertEqual(command[command.index('--lc-collate') + 1], 'C')
        self.assertEqual(command[command.index('--lc-ctype') + 1], 'C')
        self.assertNotIn('--locale-provider', command)
        self.assertEqual(calls[1][0][0], 'pg_restore')

    def test_icu_database_preserves_provider_and_locale(self):
        with patch('netbox_ops.backups._sql', return_value=self._settings(
                provider='i', icu_locale='en-US-u-kn-true', collate='en_US.UTF-8',
                ctype='en_US.UTF-8')):
            options = _stage_database_options(self.cfg)
        self.assertEqual(options, [
            '--encoding', 'UTF8', '--lc-collate', 'en_US.UTF-8',
            '--lc-ctype', 'en_US.UTF-8',
            '--locale-provider', 'icu', '--icu-locale', 'en-US-u-kn-true',
        ])

    def test_old_postgres_without_provider_column_uses_libc(self):
        with patch('netbox_ops.backups._sql', return_value=self._settings(provider=None)):
            options = _stage_database_options(self.cfg)
        self.assertNotIn('--locale-provider', options)

    def test_rejects_missing_or_unknown_database_locale_without_creating_stage(self):
        for value in ('', '{}', self._settings(encoding='SQL_ASCII', provider='?'),
                      self._settings(encoding='UTF8', collate=''),
                      self._settings(provider='i', icu_locale=None)):
            with self.subTest(value=value), \
                 patch('netbox_ops.backups._sql', return_value=value), \
                 patch('netbox_ops.backups._pg') as pg:
                with self.assertRaises(BackupError):
                    _stage_database_options(self.cfg)
                pg.assert_not_called()

    def test_failed_pg_restore_drops_stage_without_touching_live_db(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / 'netbox.dump'
            dump.write_bytes(b'PGDMP_test')
            def fake_pg(*args, **kwargs):
                if args[0] == 'pg_restore':
                    raise BackupError('import failed')
                return ''
            with patch('netbox_ops.backups._sql', return_value=self._settings()), \
                 patch('netbox_ops.backups._pg', side_effect=fake_pg) as pg:
                with self.assertRaisesRegex(BackupError, 'import failed'):
                    _stage_db(self.cfg, dump)
        self.assertEqual([call.args[0] for call in pg.call_args_list],
                         ['createdb', 'pg_restore', 'dropdb'])
        self.assertEqual(pg.call_args_list[-1].args[-1],
                         pg.call_args_list[0].args[-1])
        self.assertNotIn(self.cfg.database,
                         [call.args[-1] for call in pg.call_args_list])


if __name__ == '__main__':
    unittest.main()
