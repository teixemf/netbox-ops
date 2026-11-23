"""Offline security tests for self-upgrade release archive validation."""
from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from netbox_ops.self_upgrade_archive import (
    ReleaseArchiveError, verify_release_archive,
)

REQUIRED = (
    "netbox-ops/netbox_ops.py",
    "netbox-ops/install.sh",
    "netbox-ops/upgrade.sh",
    "netbox-ops/netbox_ops/__init__.py",
)


def build_archive(path, extra=()):
    with tarfile.open(path, "w:gz") as bundle:
        for name in (*REQUIRED, *extra):
            member = tarfile.TarInfo(name)
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))


def checksum(path):
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}".encode()


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / "netbox-ops-v0.6.0.tar.gz"

    def test_valid_archive(self):
        build_archive(self.archive)
        verify_release_archive(self.archive, checksum(self.archive))

    def test_checksum_mismatch_and_invalid_name(self):
        build_archive(self.archive)
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, b"0" * 64 + b"  " + self.archive.name.encode())
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, checksum(self.archive).replace(b"v0.6.0", b"v0.7.0"))

    def test_reject_traversal_and_absolute_paths(self):
        for bad in ("netbox-ops/../escape", "/etc/passwd", "netbox-ops/./file",
                    "netbox-ops/back\\slash", "other/file"):
            with self.subTest(name=bad):
                build_archive(self.archive, (bad,))
                with self.assertRaises(ReleaseArchiveError):
                    verify_release_archive(self.archive, checksum(self.archive))

    def test_reject_symlink_hardlink_and_devices(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                build_archive(self.archive)
                with tarfile.open(self.archive, "a") if False else tarfile.open(
                        self.archive, "w:gz") as bundle:
                    for name in REQUIRED:
                        member = tarfile.TarInfo(name)
                        member.size = 1
                        bundle.addfile(member, io.BytesIO(b"x"))
                    member = tarfile.TarInfo("netbox-ops/unsafe")
                    member.type = kind
                    member.linkname = "../../etc/passwd"
                    bundle.addfile(member)
                with self.assertRaises(ReleaseArchiveError):
                    verify_release_archive(self.archive, checksum(self.archive))

    def test_reject_missing_files_and_duplicate_members(self):
        build_archive(self.archive, ())
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name in (*REQUIRED, REQUIRED[0]):
                member = tarfile.TarInfo(name)
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, checksum(self.archive))
        with tarfile.open(self.archive, "w:gz") as bundle:
            member = tarfile.TarInfo(REQUIRED[0])
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, checksum(self.archive))

    def test_reject_corrupt_archive(self):
        self.archive.write_bytes(b"not gzip")
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, checksum(self.archive))

    def test_reject_setuid(self):
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name in REQUIRED:
                member = tarfile.TarInfo(name)
                member.size = 1
                member.mode = 0o4755
                bundle.addfile(member, io.BytesIO(b"x"))
        with self.assertRaises(ReleaseArchiveError):
            verify_release_archive(self.archive, checksum(self.archive))


if __name__ == "__main__":
    unittest.main()
