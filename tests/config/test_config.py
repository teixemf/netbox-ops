from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.cli import (MENU_ACTIONS, build_parser, dispatch, interactive_menu,
                            perform_restart, perform_service_action, perform_service_batch,
                            order_services, challenge)
from netbox_ops.config import (CONFIG_PATH, DEFAULT_UNITS, SERVICE_NAMES,
                               Configuration, ConfigError, load_config)
from netbox_ops.i18n import CATALOGUE_DIR, Translator
from netbox_ops.services import ServiceError, SystemdServices


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


class ConfigurationTests(unittest.TestCase):
    def test_default_mapping(self):
        with patch('netbox_ops.config.default_units', return_value=DEFAULT_UNITS.copy()), \
             tempfile.TemporaryDirectory() as temp:
            config = load_config(Path(temp) / 'missing.json')
        self.assertEqual(config.units['postgresql'], 'postgresql.service')

    def test_local_unit_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': 'postgresql-16.service'}}))
            config = load_config(path)
        self.assertEqual(config.units['postgresql'], 'postgresql-16.service')

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': '--dangerous'}}))
            with self.assertRaises(ConfigError):
                load_config(path)
