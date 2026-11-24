"""Prepare verified releases without replacing the active installation."""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from .self_upgrade_archive import (
    MAX_ARCHIVE_BYTES, MAX_EXTRACTED_BYTES, ReleaseArchiveError, verify_release_archive,
)


def _safe_parent(path: Path) -> None:
    if (not path.is_absolute() or path.resolve() != path
            or path.is_symlink() or not path.is_dir()):
        raise ReleaseArchiveError(f'Unsafe staging directory: {path}')
    info = path.stat()
    if info.st_mode & 0o022:
        raise ReleaseArchiveError(f'Writable staging directory: {path}')
    if path == Path('/opt') and (info.st_uid != 0 or os.geteuid() != 0):
        raise ReleaseArchiveError('Staging under /opt requires root ownership and privileges')


def _snapshot_archive(archive: Path, destination: Path) -> None:
    """Copy bounded bytes into a private directory before verification."""
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE_BYTES:
            raise ReleaseArchiveError('Invalid archive file')
        total = 0
        with destination.open('xb') as output:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ReleaseArchiveError('Archive exceeds size limit')
                output.write(chunk)
        if total != info.st_size:
            raise ReleaseArchiveError('Archive changed during staging')


def _check_tree(staged: Path) -> None:
    for root, dirs, files in os.walk(staged, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ReleaseArchiveError('Unexpected staged file type')
            if info.st_uid != os.geteuid() or info.st_mode & 0o6022:
                raise ReleaseArchiveError('Unsafe staged permissions or ownership')


def stage_release(archive: Path, checksum: bytes, *,
                  temp_root: Path = Path('/tmp'),
                  install_parent: Path = Path('/opt')) -> Path:
    """Return a private staging directory containing verified release files.

    The caller must validate compatibility before calling this function and
    remove the staging directory on cancellation. No installation is activated.
    """
    _safe_parent(install_parent)
    if (not temp_root.is_absolute() or temp_root.resolve() != temp_root
            or not temp_root.is_dir()):
        raise ReleaseArchiveError('Unsafe temporary directory')
    info = temp_root.stat()
    if info.st_mode & 0o022 and (not info.st_mode & stat.S_ISVTX or info.st_uid != 0):
        raise ReleaseArchiveError('Unsafe temporary directory permissions')
    margin = 1024 * 1024
    if shutil.disk_usage(install_parent).free < MAX_EXTRACTED_BYTES + margin:
        raise ReleaseArchiveError('Not enough free space for staging')
    if shutil.disk_usage(temp_root).free < MAX_ARCHIVE_BYTES + MAX_EXTRACTED_BYTES + margin:
        raise ReleaseArchiveError('Not enough free space for extraction')
    temporary = Path(tempfile.mkdtemp(prefix='netbox-ops-extract-', dir=temp_root))
    staged: Path | None = None
    try:
        snapshot = temporary / archive.name
        _snapshot_archive(archive, snapshot)
        verify_release_archive(snapshot, checksum)
        # Verification and extraction use the same private copy.
        with tarfile.open(snapshot, 'r:gz') as bundle:
            bundle.extractall(temporary, filter='data')
        source = temporary / 'netbox-ops'
        if not source.is_dir() or source.is_symlink():
            raise ReleaseArchiveError('Invalid extracted installation')
        staged = Path(tempfile.mkdtemp(prefix='.netbox-ops-stage-', dir=install_parent))
        os.chmod(staged, 0o700)
        for entry in source.iterdir():
            if entry.is_symlink():
                raise ReleaseArchiveError('Unexpected link in extracted release')
            target = staged / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=False)
            elif entry.is_file():
                shutil.copy2(entry, target, follow_symlinks=False)
            else:
                raise ReleaseArchiveError('Unexpected installation file type')
        _check_tree(staged)
        return staged
    except BaseException:
        if staged is not None:
            shutil.rmtree(staged)
        raise
    finally:
        shutil.rmtree(temporary)
