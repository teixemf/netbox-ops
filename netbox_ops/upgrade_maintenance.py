"""Persistent marker for interrupted NetBox upgrades."""
import os
import stat
from pathlib import Path

from .services import UPGRADE_PENDING_PATH as MARKER
from .upgrade_execution import UpgradeExecutionError


def _open_directory(path: Path) -> int:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != 0 or info.st_mode & 0o022:
        os.close(fd)
        raise UpgradeExecutionError('Unsafe maintenance directory')
    return fd


def begin_maintenance(path: Path = MARKER) -> None:
    """Create a private marker before stopping writers; never replace one."""
    try:
        directory = _open_directory(path)
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'NetBox upgrade pending; inspect audit and recovery before clearing.\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise UpgradeExecutionError('Cannot create maintenance marker; inspect pending upgrade') from None


def finish_maintenance(path: Path = MARKER) -> None:
    """Remove only a private root-owned marker after successful health checks."""
    directory = _open_directory(path)
    try:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise UpgradeExecutionError('Unsafe maintenance marker')
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
