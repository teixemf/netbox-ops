"""Verify official release archives before any extraction or installation.

This module is offline. Network retrieval, compatibility checks and /opt staging
are separate steps; callers must never extract a failed verification.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import tarfile
import zlib
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 2000
# Include tar headers, padding and bounded extended metadata in the stream limit.
MAX_TAR_BYTES = MAX_EXTRACTED_BYTES + MAX_MEMBERS * 1024 + 16 * 1024 * 1024
_CHECKSUM = re.compile(r"^([0-9a-fA-F]{64})  (netbox-ops-v[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz)$")
_REQUIRED = frozenset({
    "netbox-ops/netbox_ops.py", "netbox-ops/install.sh",
    "netbox-ops/upgrade.sh", "netbox-ops/netbox_ops/__init__.py",
    "netbox-ops/LICENSE", "netbox-ops/README.md",
    "netbox-ops/CHANGELOG.md", "netbox-ops/config.example.json",
})
_ROOT_FILES = frozenset(PurePosixPath(name).name for name in _REQUIRED
                        if len(PurePosixPath(name).parts) == 2) | {'uninstall.sh'}


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



def _check_tar_stream(archive: Path) -> None:
    """Bound the full decompressed stream before tarfile parses extended headers."""
    try:
        total = 0
        with gzip.open(archive, 'rb') as stream:
            while chunk := stream.read(min(1024 * 1024, MAX_TAR_BYTES + 1 - total)):
                total += len(chunk)
                if total > MAX_TAR_BYTES:
                    raise ReleaseArchiveError("Compressed archive expands beyond tar stream limit")
    except (OSError, EOFError, zlib.error) as exc:
        raise ReleaseArchiveError("Invalid compressed archive") from exc


def validate_archive(archive: Path) -> None:
    """Reject unsafe tar entries and unsupported installation layouts."""
    _check_tar_stream(archive)
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
                    if not (name in ("netbox-ops", "netbox-ops/") and member.isdir()):
                        raise ReleaseArchiveError(f"Unsafe archive path: {name}")
                name = str(path)
                if len(path.parts) >= 2:
                    top = path.parts[1]
                    if top == "netbox_ops":
                        if len(path.parts) == 2 and not member.isdir():
                            raise ReleaseArchiveError("Invalid package directory")
                        if any(part in {".git", "venv", "__pycache__"} for part in path.parts[2:]):
                            raise ReleaseArchiveError("Archive contains installation runtime or Git data")
                    elif top not in _ROOT_FILES or len(path.parts) != 2 or not member.isfile():
                        raise ReleaseArchiveError("Unknown release archive layout")
                if any(not char.isprintable() for char in name):
                    raise ReleaseArchiveError("Unsafe archive path characters")
                if name in seen:
                    raise ReleaseArchiveError("Duplicate archive member")
                seen.add(name)
                if member.mode & 0o6000:
                    raise ReleaseArchiveError("Archive contains privileged permissions")
                if member.isdir():
                    if member.size != 0:
                        raise ReleaseArchiveError("Directory contains unexpected data")
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None:
                    raise ReleaseArchiveError("Archive contains links or special files")
                if member.size < 0 or member.size > MAX_EXTRACTED_BYTES:
                    raise ReleaseArchiveError("Invalid member size")
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise ReleaseArchiveError("Archive expands beyond size limit")
                files.add(name)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ReleaseArchiveError("Invalid compressed archive") from exc
    if any(str(parent) in files for name in seen for parent in PurePosixPath(name).parents):
        raise ReleaseArchiveError("Archive contains conflicting file and directory paths")
    if not _REQUIRED.issubset(files):
        raise ReleaseArchiveError("Release is missing required installation files")


def verify_release_archive(archive: Path, checksum: bytes) -> None:
    """Verify both the checksum and archive structure without extracting."""
    verify_checksum(archive, checksum)
    validate_archive(archive)
