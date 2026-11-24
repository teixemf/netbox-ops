"""Offline tests for staging releases without touching the active tree."""
from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_stage import stage_release


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tmp = self.root / 'tmp'
        self.opt = self.root / 'opt'
        self.tmp.mkdir(mode=0o700)
        self.opt.mkdir(mode=0o700)
        self.archive = self.root / 'netbox-ops-v0.6.0.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for name in ('netbox-ops/netbox_ops.py', 'netbox-ops/install.sh',
                         'netbox-ops/upgrade.sh', 'netbox-ops/netbox_ops/__init__.py'):
                item = tarfile.TarInfo(name)
                item.size = 1
                item.mode = 0o755
                bundle.addfile(item, io.BytesIO(b'x'))
        self.checksum = (hashlib.sha256(self.archive.read_bytes()).hexdigest()
                         + '  ' + self.archive.name).encode()

    def test_stage_leaves_active_installation_untouched(self):
        active = self.opt / 'netbox-ops'
        active.mkdir()
        (active / 'keep').write_text('original')
        staged = stage_release(self.archive, self.checksum,
                               temp_root=self.tmp, install_parent=self.opt)
        self.assertTrue(staged.name.startswith('.netbox-ops-stage-'))
        self.assertEqual((active / 'keep').read_text(), 'original')
        self.assertTrue((staged / 'install.sh').is_file())
        self.assertFalse(list(self.tmp.iterdir()))

    def test_verified_snapshot_is_used_if_original_changes(self):
        from netbox_ops.self_upgrade_archive import verify_release_archive

        def verify_then_change(snapshot, checksum):
            verify_release_archive(snapshot, checksum)
            self.archive.write_bytes(b'changed after verification')

        with patch('netbox_ops.self_upgrade_stage.verify_release_archive',
                   side_effect=verify_then_change):
            staged = stage_release(self.archive, self.checksum,
                                   temp_root=self.tmp, install_parent=self.opt)
        self.assertEqual((staged / 'install.sh').read_bytes(), b'x')
        self.assertFalse(list(self.tmp.iterdir()))

    def test_reject_symlinked_archive(self):
        original = self.archive.with_name('original.tar.gz')
        self.archive.rename(original)
        self.archive.symlink_to(original)
        with self.assertRaises(OSError):
            stage_release(self.archive, self.checksum,
                          temp_root=self.tmp, install_parent=self.opt)
        self.assertFalse(list(self.tmp.iterdir()))
        self.assertFalse(list(self.opt.iterdir()))

    def test_bad_checksum_never_creates_staging(self):
        with self.assertRaises(ReleaseArchiveError):
            stage_release(self.archive, b'bad', temp_root=self.tmp,
                          install_parent=self.opt)
        self.assertFalse(list(self.opt.iterdir()))

    def test_reject_writable_parent(self):
        self.opt.chmod(0o777)
        with self.assertRaises(ReleaseArchiveError):
            stage_release(self.archive, self.checksum,
                          temp_root=self.tmp, install_parent=self.opt)

    def test_copy_failure_cleans_both_temporary_directories(self):
        with patch('netbox_ops.self_upgrade_stage.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                stage_release(self.archive, self.checksum,
                              temp_root=self.tmp, install_parent=self.opt)
        self.assertFalse(list(self.tmp.iterdir()))
        self.assertFalse(list(self.opt.iterdir()))

    def test_symlinked_parent_rejected(self):
        link = self.root / 'link'
        link.symlink_to(self.opt, target_is_directory=True)
        with self.assertRaises(ReleaseArchiveError):
            stage_release(self.archive, self.checksum,
                          temp_root=self.tmp, install_parent=link)


if __name__ == '__main__':
    unittest.main()
