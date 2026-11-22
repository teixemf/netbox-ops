from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.config import DEFAULT_UNITS, ConfigError, load_config


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


class ConfigurationTests(unittest.TestCase):
    def test_default_mapping(self):
        with patch('netbox_ops.config.default_units', return_value=DEFAULT_UNITS.copy()), \
             tempfile.TemporaryDirectory(dir='/root') as temp:
            config = load_config(Path(temp) / 'missing.json')
        self.assertEqual(config.units['postgresql'], 'postgresql.service')

    def test_local_unit_overrides(self):
        with tempfile.TemporaryDirectory(dir='/root') as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': 'postgresql-16.service'}}))
            path.chmod(0o600)
            config = load_config(path)
        self.assertEqual(config.units['postgresql'], 'postgresql-16.service')

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory(dir='/root') as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': '--dangerous'}}))
            path.chmod(0o600)
            with self.assertRaises(ConfigError):
                load_config(path)
