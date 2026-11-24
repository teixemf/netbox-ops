"""Offline runtime staging tests using disposable temporary directories."""
from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_releases import Version
from netbox_ops.self_upgrade_runtime import (
    prepare_staged_runtime, stage_installation, validate_installation_runtime,
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

    def make_archive(self, version='0.6.0'):
        archive = self.parent / 'netbox-ops-v0.6.0.tar.gz'
        files = {
            'netbox-ops/netbox_ops.py': f'print("netbox-ops {version}")'.encode(),
            'netbox-ops/netbox_ops/__init__.py': f'__version__ = "{version}"\n'.encode(),
            'netbox-ops/LICENSE': b'license',
            'netbox-ops/README.md': b'readme',
            'netbox-ops/CHANGELOG.md': b'changes',
            'netbox-ops/config.example.json': b'{}',
            'netbox-ops/install.sh': b'#!/bin/sh\nexit 99\n',
            'netbox-ops/upgrade.sh': b'#!/bin/sh\nexit 99\n',
        }
        with tarfile.open(archive, 'w:gz') as bundle:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755
                bundle.addfile(member, io.BytesIO(content))
        checksum = f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}'.encode()
        return archive, checksum

    def test_stage_installation_builds_complete_candidate(self):
        archive, checksum = self.make_archive()
        result = stage_installation(archive, checksum, self.version,
                                    temp_root=self.parent, install_parent=self.parent)
        validate_installation_runtime(result, self.version)
        self.assertEqual((self.active / 'keep').read_text(), 'original')

    def test_stage_installation_cleans_failed_candidate(self):
        archive, checksum = self.make_archive(version='0.7.0')
        with self.assertRaises(ReleaseArchiveError):
            stage_installation(archive, checksum, self.version,
                               temp_root=self.parent, install_parent=self.parent)
        self.assertEqual(list(self.parent.glob('.netbox-ops-stage-*')), [self.staged])
        self.assertEqual((self.active / 'keep').read_text(), 'original')

    def test_stage_installation_rejects_wrong_archive_name(self):
        archive, checksum = self.make_archive()
        with self.assertRaises(ReleaseArchiveError):
            stage_installation(archive, checksum, Version.parse('v0.7.0'),
                               temp_root=self.parent, install_parent=self.parent)

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
