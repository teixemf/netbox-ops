"""Isolated tests of launcher-link installer logic; never write to /usr/sbin."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerLinkTests(unittest.TestCase):
    @staticmethod
    def link_fragment() -> str:
        install_script = Path(__file__).resolve().parents[1] / 'install.sh'
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


if __name__ == '__main__':
    unittest.main()
