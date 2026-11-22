"""Isolated tests of launcher-link installer logic; never write to /usr/sbin."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerLinkTests(unittest.TestCase):
    @staticmethod
    def link_fragment() -> str:
        install_script = Path(__file__).resolve().parents[2] / 'install.sh'
        contents = install_script.read_text(encoding='utf-8')
        return contents.split('LINK=/usr/sbin/netbox-ops\n', 1)[1].split('\nprintf ', 1)[0].replace('TARGET=/usr/local/sbin/netbox-ops\n', '')

    def try_fragment(self, link: Path, target: Path) -> subprocess.CompletedProcess[str]:
        fragment = ('set -euo pipefail\n'
                    f'LINK={link}\nTARGET={target}\n' + self.link_fragment())
        return subprocess.run(['bash', '-c', fragment], capture_output=True, text=True)

    def test_create_and_repeat_keeps_correct_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'sbin' / 'netbox-ops'
            link.parent.mkdir()
            target = Path(temp) / 'local-sbin-netbox-ops'
            target.write_text('#!/bin/sh\n')
            self.assertEqual(self.try_fragment(link, target).returncode, 0)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.readlink(), target)
            self.assertEqual(self.try_fragment(link, target).returncode, 0)
            self.assertEqual(link.readlink(), target)

    def test_never_overwrites_other_file_or_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'netbox-ops'
            target = Path(temp) / 'correct'
            link.write_text('another tool')
            self.assertNotEqual(self.try_fragment(link, target).returncode, 0)
            self.assertEqual(link.read_text(), 'another tool')
            link.unlink()
            link.symlink_to(Path(temp) / 'different')
            self.assertNotEqual(self.try_fragment(link, target).returncode, 0)
            self.assertEqual(link.readlink(), Path(temp) / 'different')


class ConfigInstallPathTests(unittest.TestCase):
    def check(self, directory: Path) -> subprocess.CompletedProcess[str]:
        installer = Path(__file__).resolve().parents[2] / 'install.sh'
        return subprocess.run(['bash', '-c', 'source "$1"; check_config_paths "$2"',
                               'bash', str(installer), str(directory)],
                              capture_output=True, text=True)

    def test_missing_or_regular_config_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'etc-netbox-ops'
            self.assertEqual(self.check(directory).returncode, 0)
            directory.mkdir()
            config = directory / 'config.json'
            config.write_text('{}', encoding='utf-8')
            self.assertEqual(self.check(directory).returncode, 0)
            self.assertEqual(config.read_text(encoding='utf-8'), '{}')

    def test_rejects_config_symlink_and_broken_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'etc-netbox-ops'
            directory.mkdir()
            target = Path(temp) / 'real.json'
            target.write_text('{}', encoding='utf-8')
            config = directory / 'config.json'
            for destination in (target, Path(temp) / 'nonexistent.json'):
                with self.subTest(destination=destination):
                    config.symlink_to(destination)
                    result = self.check(directory)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('must not be a symbolic link', result.stderr)
                    self.assertTrue(config.is_symlink())
                    config.unlink()
            self.assertEqual(target.read_text(encoding='utf-8'), '{}')

    def test_rejects_config_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'etc-netbox-ops'
            directory.symlink_to(Path(temp) / 'missing-directory')
            self.assertNotEqual(self.check(directory).returncode, 0)
            self.assertTrue(directory.is_symlink())


if __name__ == '__main__':
    unittest.main()
