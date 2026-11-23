"""Prepare a verified release on the installation filesystem; never activate it."""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from .self_upgrade_archive import (
    MAX_EXTRACTED_BYTES, ReleaseArchiveError, verify_release_archive,
)


def _safe_parent(path: Path) -> None:
    """Refuse symlinked, writable-by-others or non-directory staging parents."""
    if path.is_symlink() or not path.is_dir():
        raise ReleaseArchiveError(f'Unsafe staging directory: {path}')
    info = path.stat()
    if info.st_mode & 0o022:
        raise ReleaseArchiveError(f'Writable staging directory: {path}')
    if path == Path('/opt') and info.st_uid != 0:
        raise ReleaseArchiveError('The /opt directory must be root-owned')


def stage_release(archive: Path, checksum: bytes, *,
                  temp_root: Path = Path('/tmp'),
                  install_parent: Path = Path('/opt')) -> Path:
    """Return a private /opt staging directory, without replacing the active tree.

    The caller must remove the returned directory on cancellation or failure.
    Compatibility and target version must be checked by the caller. This
    function does not invoke install.sh, upgrade.sh or any service operation.
    """
    verify_release_archive(archive, checksum)
    _safe_parent(install_parent)
    if temp_root.is_symlink() or not temp_root.is_dir():
        raise ReleaseArchiveError('Unsafe temporary directory')
    if install_parent == Path('/opt') and os.geteuid() != 0:
        raise ReleaseArchiveError('Staging under /opt requires root')
    # Reserve enough room for a full copy, plus a small metadata margin.
    if shutil.disk_usage(install_parent).free < MAX_EXTRACTED_BYTES + 1024 * 1024:
        raise ReleaseArchiveError('Not enough free space for staging')
    temporary = Path(tempfile.mkdtemp(prefix='netbox-ops-extract-', dir=temp_root))
    staged: Path | None = None
    try:
        with tarfile.open(archive, 'r:gz') as bundle:
            # The archive is revalidated immediately above. No filters or
            # links are accepted, so extraction cannot escape temporary.
            bundle.extractall(temporary, filter='data')
        source = temporary / 'netbox-ops'
        if not source.is_dir() or source.is_symlink():
            raise ReleaseArchiveError('Invalid extracted installation')
        staged = Path(tempfile.mkdtemp(prefix='.netbox-ops-stage-', dir=install_parent))
        os.chmod(staged, 0o700)
        # Copy files, not links. The destination stays separate from the
        # active installation until the external helper performs a swap.
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
        for root, dirs, files in os.walk(staged):
            for name in dirs + files:
                path = Path(root) / name
                info = path.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ReleaseArchiveError('Unexpected staged file type')
                if path.is_symlink():
                    raise ReleaseArchiveError('Unexpected staged link')
        return staged
    except BaseException:
        if staged is not None:
            shutil.rmtree(staged)
        raise
    finally:
        shutil.rmtree(temporary)
