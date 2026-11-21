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
        for action in ('db', 'media', 'full'):
            self.assertEqual(parser.parse_args(['backup', action]).action, action)
            args = parser.parse_args(['restore', action, '--file', '/tmp/a.dump', '--yes',
                                      '--acknowledge-data-loss'])
            self.assertTrue(args.acknowledge_data_loss)
        self.assertEqual(parser.parse_args(['backup', 'list', '--kind', 'db']).kind, 'db')

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
            path.chmod(0o600)
            p.chmod(0o600)
            with self.assertRaises(ConfigError):
                load_config(p)
