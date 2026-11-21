"""Offline tests: no systemd or PostgreSQL changes."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.config import DEFAULT_UNITS, Configuration, ConfigError, load_config
from netbox_ops.cli import build_parser, perform_backup_action
from netbox_ops.i18n import Translator


class BackupCliTests(unittest.TestCase):
    def test_commands_and_confirmation_flags(self):
        parser = build_parser()
        for action in ('db', 'media', 'combined'):
            self.assertEqual(parser.parse_args(['backup', action]).action, action)
        for action in ('db', 'media', 'combined', 'full'):
            args = parser.parse_args(['restore', action, '--file', '/tmp/a.dump', '--yes',
                                      '--acknowledge-data-loss'])
            self.assertTrue(args.acknowledge_data_loss)
        for kind in ('combined', 'full'):
            args = parser.parse_args(['restore', kind, '--file', '/tmp/a.tar.gz', '--only', 'db'])
            self.assertEqual(args.only, 'db')
            self.assertEqual(parser.parse_args(['backup', 'list', '--kind', kind]).kind, kind)
            self.assertEqual(parser.parse_args(['backup', 'verify', '--kind', kind,
                                                '--file', '/tmp/a.tar.gz']).kind, kind)
        self.assertEqual(parser.parse_args(['backup', 'list', '--kind', 'db']).kind, 'db')
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                parser.parse_args(['backup', 'full'])

    def test_no_restore_when_only_yes(self):
        import contextlib
        with patch('netbox_ops.backups.BackupManager') as manager, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = perform_backup_action('restore', 'db', Configuration(DEFAULT_UNITS),
                                       Translator('en'), source=Path('/tmp/a.dump'), yes=True)
        self.assertEqual(rc, 2)
        manager.return_value.restore.assert_not_called()

    def test_config_rejects_unknown_backup_fields(self):
        with tempfile.TemporaryDirectory(dir='/root') as folder:
            p = Path(folder) / 'cfg.json'
            p.write_text(json.dumps({'backup': {'unknown': True}}))
            p.chmod(0o600)
            with self.assertRaises(ConfigError):
                load_config(p)
