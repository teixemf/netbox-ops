"""Security checks for root-controlled JSON configuration files."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.config import ConfigError, DEFAULT_UNITS, load_config


@unittest.skipUnless(os.geteuid() == 0, 'Secure config filesystem tests require root')
class SecureConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/root')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'config.json'
        self.path.write_text('{"services": {}}', encoding='utf-8')
        self.path.chmod(0o600)
        self.units_patch = patch('netbox_ops.config.default_units',
                                return_value=DEFAULT_UNITS.copy())
        self.units_patch.start()
        self.addCleanup(self.units_patch.stop)

    def test_accepts_root_owned_0600_and_0400(self):
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                self.assertEqual(load_config(self.path).units, DEFAULT_UNITS)

    def test_rejects_group_or_other_access(self):
        for mode in (0o640, 0o644, 0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaisesRegex(ConfigError, 'accessible by group or others'):
                    load_config(self.path)

    def test_rejects_nonroot_owner(self):
        os.chown(self.path, 65534, -1)
        with self.assertRaisesRegex(ConfigError, 'owned by root'):
            load_config(self.path)

    def test_rejects_file_symlink_including_broken_link(self):
        link = self.directory / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ConfigError, 'symbolic links'):
            load_config(link)
        link.unlink()
        link.symlink_to(self.directory / 'missing.json')
        with self.assertRaisesRegex(ConfigError, 'symbolic links'):
            load_config(link)

    def test_rejects_parent_symlink(self):
        link = self.directory / 'alias'
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, 'symbolic links'):
            load_config(link / self.path.name)

    def test_rejects_writable_parent_directory(self):
        self.directory.chmod(0o777)
        with self.assertRaisesRegex(ConfigError, 'unsafe configuration directory'):
            load_config(self.path)

    def test_rejects_nonroot_parent_directory(self):
        os.chown(self.directory, 65534, -1)
        with self.assertRaisesRegex(ConfigError, 'unsafe configuration directory'):
            load_config(self.path)

    def test_rejects_fifo_without_blocking(self):
        fifo = self.directory / 'named-pipe'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ConfigError, 'regular file'):
            load_config(fifo)

    def test_missing_file_or_directory_keeps_defaults(self):
        for path in (self.directory / 'missing.json', self.directory / 'missing' / 'config.json'):
            with self.subTest(path=str(path)):
                self.assertEqual(load_config(path).units, DEFAULT_UNITS)

    def test_missing_file_inside_unsafe_directory_is_rejected(self):
        self.directory.chmod(0o777)
        with self.assertRaisesRegex(ConfigError, 'unsafe configuration directory'):
            load_config(self.directory / 'missing.json')

    def test_invalid_json_and_invalid_utf8_raise_config_error(self):
        for content in (b'{', b'\xff'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.path.chmod(0o600)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_preserves_content_validation(self):
        self.path.write_text(json.dumps({'services': {'postgresql': '--unsafe'}}), encoding='utf-8')
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ConfigError, 'invalid systemd unit'):
            load_config(self.path)

    def test_missing_explicit_config_still_reports_discovery_failure(self):
        from netbox_ops.platform import PlatformError
        with patch('netbox_ops.config.default_units', side_effect=PlatformError('ambiguous service')):
            with self.assertRaisesRegex(ConfigError, 'ambiguous service'):
                load_config(self.directory / 'missing.json')

    def test_explicit_overrides_work_when_discovery_fails(self):
        from netbox_ops.platform import PlatformError
        self.path.write_text(json.dumps({
            'services': {'postgresql': 'postgresql@16-main.service'},
            'backup': {'postgres_port': 5432},
        }), encoding='utf-8')
        self.path.chmod(0o600)
        with patch('netbox_ops.config.default_units', side_effect=PlatformError('ambiguous service')):
            cfg = load_config(self.path)
        self.assertEqual(cfg.units['postgresql'], 'postgresql@16-main.service')

    def test_rejects_root_and_traversal_paths(self):
        for path in (self.directory / '..' / 'config.json', Path('/')):
            with self.subTest(path=str(path)):
                with self.assertRaisesRegex(ConfigError, 'root or contain traversal'):
                    load_config(path)

    def test_relative_path_uses_trusted_working_directory(self):
        with patch('netbox_ops.config.Path.cwd', return_value=self.directory):
            self.assertEqual(load_config(Path('config.json')).units, DEFAULT_UNITS)

    def test_relative_path_rejects_untrusted_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'config.json').write_text('{}', encoding='utf-8')
            with patch('netbox_ops.config.Path.cwd', return_value=Path(temp)):
                with self.assertRaisesRegex(ConfigError, 'unsafe configuration directory'):
                    load_config(Path('config.json'))


if __name__ == '__main__':
    unittest.main()
