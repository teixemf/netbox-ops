"""Offline integration tests for compatibility-gated staging."""
from __future__ import annotations

import gzip
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netbox_ops import __version__
from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_prepare import prepare_release
from netbox_ops.self_upgrade_releases import (
    DOWNLOAD_PREFIX, OfficialRelease, ReleaseAsset, Version,
)
from netbox_ops.self_upgrade_runtime import validate_installation_runtime


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / 'netbox-ops'
        self.active.mkdir()
        (self.active / 'keep').write_text('original')
        self.version = Version.parse(f'v{__version__}')
        self.installed = Version(0, 0, 0)
        self.download = self.root / 'download'
        self.download.mkdir()
        self.archive = self.download / f'netbox-ops-v{self.version}.tar.gz'
        self.archive.write_bytes(b'example')
        self.checksum = self.archive.with_name(self.archive.name + '.sha256')
        self.checksum.write_bytes(b'checksum')
        self.release = self.make_release()

    def make_release(self):
        tag = f'v{self.version}'
        prefix = DOWNLOAD_PREFIX + tag + '/'
        return OfficialRelease(
            self.version, tag, '', '2026-11-24',
            ReleaseAsset(self.archive.name, prefix + self.archive.name,
                         self.archive.stat().st_size),
            ReleaseAsset(self.checksum.name, prefix + self.checksum.name,
                         self.checksum.stat().st_size),
        )

    def prepare(self, approve):
        return prepare_release(self.release, installed=self.installed, approve=approve,
                               temp_root=self.root, install_parent=self.root)

    def test_rejected_approval_does_not_download(self):
        with patch('netbox_ops.self_upgrade_prepare.download_release') as fetch:
            with self.assertRaises(ReleaseArchiveError):
                self.prepare(Mock(side_effect=ReleaseArchiveError('not compatible')))
            fetch.assert_not_called()

    def test_downgrade_and_same_version_rejected_before_approval(self):
        for current in (self.version, Version(999, 0, 0)):
            self.installed = current
            approve = Mock()
            with self.assertRaises(ReleaseArchiveError):
                self.prepare(approve)
            approve.assert_not_called()

    def test_approval_download_stage_order_and_cleanup(self):
        calls = []
        candidate = self.root / '.netbox-ops-stage-test'

        def download(*args, **kwargs):
            calls.append('download')
            return self.archive

        def stage(*args, **kwargs):
            calls.append('stage')
            self.assertEqual(args, (self.archive, b'checksum', self.version))
            return candidate

        with (patch('netbox_ops.self_upgrade_prepare.download_release', side_effect=download),
              patch('netbox_ops.self_upgrade_prepare.stage_installation', side_effect=stage)):
            self.assertEqual(self.prepare(lambda release: calls.append('approve')), candidate)
        self.assertEqual(calls, ['approve', 'download', 'stage'])
        self.assertFalse(self.download.exists())
        self.assertEqual((self.active / 'keep').read_text(), 'original')

    def test_stage_failure_cleans_download(self):
        with (patch('netbox_ops.self_upgrade_prepare.download_release', return_value=self.archive),
              patch('netbox_ops.self_upgrade_prepare.stage_installation',
                    side_effect=ReleaseArchiveError('staging failed'))):
            with self.assertRaises(ReleaseArchiveError):
                self.prepare(Mock())
        self.assertFalse(self.download.exists())
        self.assertEqual((self.active / 'keep').read_text(), 'original')

    @unittest.skipUnless(sys.version_info >= (3, 12), 'Python 3.12 required')
    def test_stage_real_committed_release_and_validate_after_rename(self):
        repo = Path(__file__).resolve().parents[1]
        if not shutil.which('git') or not (repo / '.git').exists():
            self.skipTest('Requires a Git checkout')
        tar = subprocess.run(
            ['git', '-c', f'safe.directory={repo}', 'archive', '--format=tar',
             '--prefix=netbox-ops/', 'HEAD',
             'LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
             'install.sh', 'upgrade.sh', 'netbox_ops.py', 'netbox_ops'],
            cwd=repo, capture_output=True, timeout=30)
        self.assertEqual(tar.returncode, 0, tar.stderr.decode(errors='replace'))
        tar = tar.stdout
        self.archive.write_bytes(gzip.compress(tar, mtime=0))
        self.checksum.write_text(
            f'{hashlib.sha256(self.archive.read_bytes()).hexdigest()}  {self.archive.name}\n')
        self.release = self.make_release()
        with patch('netbox_ops.self_upgrade_prepare.download_release', return_value=self.archive):
            staged = self.prepare(Mock())
        self.assertFalse(self.download.exists())
        validate_installation_runtime(staged, self.version)
        relocated = self.root / 'relocated'
        staged.rename(relocated)
        validate_installation_runtime(relocated, self.version)
        self.assertEqual((self.active / 'keep').read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
