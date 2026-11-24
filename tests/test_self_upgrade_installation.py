"""Offline checks for the installed archive layout and safe swap candidate."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_installation import (
    ARCHIVE_LAYOUT, inspect_installation, verify_candidate_layout,
)
from netbox_ops.self_upgrade_releases import Version
from netbox_ops.self_upgrade_runtime import prepare_staged_runtime


@unittest.skipUnless(sys.version_info >= (3, 12), 'Python 3.12 required')
class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / 'netbox-ops'
        self.root.mkdir()
        self.version = Version(0, 5, 0)
        self.make_package(self.root, self.version)
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip',
                        str(self.root / 'venv')], check=True, timeout=30)
        self.launcher = self.parent / 'launcher'
        self.launcher.write_text(
            f'#!/bin/sh\nexec {self.root}/venv/bin/python {self.root}/netbox_ops.py "$@"\n')
        self.launcher.chmod(0o755)
        self.link = self.parent / 'link'
        self.link.symlink_to(self.launcher)

    @staticmethod
    def make_package(root, version):
        for name in ('LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
                     'install.sh', 'upgrade.sh'):
            (root / name).write_text('fixture')
        (root / 'netbox_ops.py').write_text(f'print("netbox-ops {version}")\n')
        (root / 'netbox_ops').mkdir()
        (root / 'netbox_ops/__init__.py').write_text(f'__version__ = "{version}"\n')

    def inspect(self):
        return inspect_installation(self.root, launcher=self.launcher, link=self.link)

    def test_installer_style_venv_and_launcher(self):
        result = self.inspect()
        self.assertEqual(result.version, self.version)
        self.assertEqual(result.environment.layout, ARCHIVE_LAYOUT)
        self.assertEqual(result.environment.python, tuple(sys.version_info[:2]))
        self.assertEqual(result.base_python, Path(sys._base_executable).resolve())
        self.assertEqual(result.identity, (self.root.stat().st_dev, self.root.stat().st_ino))

    def test_reject_git_checkout_or_unknown_local_data(self):
        for name in ('.git', 'local-backups'):
            path = self.root / name
            path.mkdir()
            with self.assertRaisesRegex(ReleaseArchiveError, 'Unknown installation layout'):
                self.inspect()
            path.rmdir()

    def test_reject_writable_package_file_before_execution(self):
        (self.root / 'netbox_ops.py').chmod(0o666)
        with (patch('netbox_ops.self_upgrade_installation._run') as run,
              self.assertRaises(ReleaseArchiveError)):
            self.inspect()
        run.assert_not_called()

    def test_reject_unexpected_nested_link(self):
        (self.root / 'netbox_ops/unknown').symlink_to(self.parent)
        with self.assertRaises(ReleaseArchiveError):
            self.inspect()

    def test_reject_modified_launcher_before_execution(self):
        self.launcher.write_text('#!/bin/sh\nexit 0\n')
        with (patch('netbox_ops.self_upgrade_installation._run') as run,
              self.assertRaises(ReleaseArchiveError)):
            self.inspect()
        run.assert_not_called()

    def test_reject_wrong_launcher_link(self):
        self.link.unlink()
        self.link.symlink_to(self.root / 'netbox_ops.py')
        with self.assertRaises(ReleaseArchiveError):
            self.inspect()

    def test_declared_version_must_match_runtime(self):
        (self.root / 'netbox_ops/__init__.py').write_text('__version__ = "0.7.0"\n')
        with self.assertRaisesRegex(ReleaseArchiveError, 'does not match'):
            self.inspect()

    def test_candidate_layout_checked_before_execution(self):
        target = Version(0, 6, 0)
        staged = self.parent / '.netbox-ops-stage-test'
        staged.mkdir(mode=0o700)
        self.make_package(staged, target)
        prepare_staged_runtime(staged, target)
        verify_candidate_layout(staged, target)
        with self.assertRaises(ReleaseArchiveError):
            verify_candidate_layout(staged, self.version)
        (staged / 'netbox_ops/__init__.py').chmod(0o666)
        with self.assertRaises(ReleaseArchiveError):
            verify_candidate_layout(staged, target)

    def test_candidate_rejects_world_readable_stage(self):
        staged = self.parent / '.netbox-ops-stage-test'
        staged.mkdir(mode=0o755)
        with self.assertRaises(ReleaseArchiveError):
            verify_candidate_layout(staged, self.version)

    def test_reject_nonliteral_version(self):
        (self.root / 'netbox_ops/__init__.py').write_text(
            'import os\n__version__ = os.getenv("VERSION")\n')
        with (patch('netbox_ops.self_upgrade_installation._run') as run,
              self.assertRaises(ReleaseArchiveError)):
            self.inspect()
        run.assert_not_called()

    def test_reject_python_link_to_writable_interpreter(self):
        python = self.root / 'venv/bin/python'
        python.unlink()
        external = self.parent / 'fake-python'
        external.write_text('#!/bin/sh\nexit 0\n')
        external.chmod(0o777)
        python.symlink_to(external)
        with self.assertRaises(ReleaseArchiveError):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
