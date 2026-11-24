"""Offline runtime staging tests using disposable temporary directories."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_releases import Version
from netbox_ops.self_upgrade_runtime import (
    prepare_staged_runtime, validate_installation_runtime,
)


@unittest.skipUnless(sys.version_info >= (3, 12), 'Python 3.12 required')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.staged = self.parent / '.netbox-ops-stage-test'
        self.staged.mkdir(mode=0o700)
        self.active = self.parent / 'netbox-ops'
        self.active.mkdir()
        (self.active / 'keep').write_text('original')
        (self.staged / 'netbox_ops.py').write_text(
            'import sys\n'
            'assert sys.argv[1:] == ["--version"]\n'
            'print("netbox-ops 0.6.0")\n'
        )
        self.version = Version.parse('v0.6.0')

    def test_runtime_works_before_and_after_same_filesystem_rename(self):
        prepare_staged_runtime(self.staged, self.version)
        self.assertEqual((self.active / 'keep').read_text(), 'original')
        target = self.parent / 'relocated'
        self.staged.rename(target)
        validate_installation_runtime(target, self.version)
        self.assertFalse((target / 'venv/bin/python').is_symlink())

    def test_wrong_version_removes_only_staged_runtime(self):
        with self.assertRaises(ReleaseArchiveError):
            prepare_staged_runtime(self.staged, Version.parse('v0.7.0'))
        self.assertFalse((self.staged / 'venv').exists())
        self.assertTrue((self.staged / 'netbox_ops.py').is_file())
        self.assertEqual((self.active / 'keep').read_text(), 'original')

    def test_preexisting_venv_is_not_modified(self):
        venv = self.staged / 'venv'
        venv.mkdir()
        (venv / 'keep').write_text('existing')
        with self.assertRaises(ReleaseArchiveError):
            prepare_staged_runtime(self.staged, self.version)
        self.assertEqual((venv / 'keep').read_text(), 'existing')

    def test_unsafe_staged_file_is_rejected(self):
        (self.staged / 'netbox_ops.py').chmod(0o666)
        with self.assertRaises(ReleaseArchiveError):
            prepare_staged_runtime(self.staged, self.version)
        self.assertFalse((self.staged / 'venv').exists())

    def test_old_installation_interpreter_is_rejected(self):
        python = self.active / 'python'
        shutil.copy2(sys._base_executable, python)
        with self.assertRaises(ReleaseArchiveError):
            prepare_staged_runtime(self.staged, self.version, python=python)

    def test_partial_runtime_failure_is_cleaned_up(self):
        def create_partial(args, cwd, *, timeout):
            if '-m' in args:
                (self.staged / 'venv').mkdir()
                raise ReleaseArchiveError('simulated venv failure')
            return '3 12'

        with patch('netbox_ops.self_upgrade_runtime._run', side_effect=create_partial):
            with self.assertRaises(ReleaseArchiveError):
                prepare_staged_runtime(self.staged, self.version)
        self.assertFalse((self.staged / 'venv').exists())
        self.assertTrue((self.active / 'keep').exists())


if __name__ == '__main__':
    unittest.main()
