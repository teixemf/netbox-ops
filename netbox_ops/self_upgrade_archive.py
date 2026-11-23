"""Verify official release archives before any extraction or installation.

This module is offline. Network retrieval, compatibility checks and /opt staging
are separate steps; callers must never extract a failed verification.
"""
from __future__ import annotations

import hashlib
import re
import tarfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 2000
_CHECKSUM = re.compile(r"^([0-9a-fA-F]{64})  (netbox-ops-v[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz)$")
_REQUIRED = frozenset({
    "netbox-ops/netbox_ops.py", "netbox-ops/install.sh",
    "netbox-ops/upgrade.sh", "netbox-ops/netbox_ops/__init__.py",
})


class ReleaseArchiveError(Exception):
    """An archive or its checksum is unsafe or invalid."""


def verify_checksum(archive: Path, checksum: bytes) -> None:
    """Check the published sha256sum line against a bounded local archive."""
    if len(checksum) > 256:
        raise ReleaseArchiveError("Checksum file is too large")
    try:
        line = checksum.decode("ascii").strip("\n")
    except UnicodeError as exc:
        raise ReleaseArchiveError("Checksum is not ASCII") from exc
    match = _CHECKSUM.fullmatch(line)
    if match is None or match.group(2) != archive.name:
        raise ReleaseArchiveError("Invalid checksum entry")
    try:
        size = archive.stat().st_size
        if not 0 < size <= MAX_ARCHIVE_BYTES or not archive.is_file() or archive.is_symlink():
            raise ReleaseArchiveError("Invalid archive file")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseArchiveError("Cannot read archive") from exc
    if digest.hexdigest() != match.group(1).lower():
        raise ReleaseArchiveError("Archive SHA-256 mismatch")


def validate_archive(archive: Path) -> None:
    """Reject unsafe tar entries and unsupported installation layouts."""
    seen: set[str] = set()
    files: set[str] = set()
    total = 0
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            for count, member in enumerate(bundle, 1):
                if count > MAX_MEMBERS:
                    raise ReleaseArchiveError("Too many archive members")
                name = member.name
                path = PurePosixPath(name)
                if (not name or name.startswith("/") or "\\" in name
                        or any(part in ("", ".", "..") for part in name.split("/"))
                        or not path.parts or path.parts[0] != "netbox-ops"
                        or len(path.parts) < 2):
                    # The root directory is allowed only as netbox-ops/.
                    if not (name == "netbox-ops/" and member.isdir()):
                        raise ReleaseArchiveError(f"Unsafe archive path: {name}")
                if name in seen:
                    raise ReleaseArchiveError("Duplicate archive member")
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ReleaseArchiveError("Archive contains links or special files")
                if member.mode & 0o6000:
                    raise ReleaseArchiveError("Archive contains privileged file permissions")
                if member.size < 0 or member.size > MAX_EXTRACTED_BYTES:
                    raise ReleaseArchiveError("Invalid member size")
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise ReleaseArchiveError("Archive expands beyond size limit")
                files.add(name)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ReleaseArchiveError("Invalid compressed archive") from exc
    if not _REQUIRED.issubset(files):
        raise ReleaseArchiveError("Release is missing required installation files")


def verify_release_archive(archive: Path, checksum: bytes) -> None:
    """Verify both the checksum and archive structure without extracting."""
    verify_checksum(archive, checksum)
    validate_archive(archive)
