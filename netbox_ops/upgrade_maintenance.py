"""Persistent marker for interrupted NetBox upgrades."""
import os
import stat
from pathlib import Path

from .upgrade_execution import UpgradeExecutionError

MARKER = Path('/run/lock/netbox-ops-upgrade.pending')


def begin_maintenance(path: Path = MARKER) -> None:
    """Create a private marker before stopping writers; never replace one."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise UpgradeExecutionError('Cannot create maintenance marker; inspect pending upgrade') from None
    with os.fdopen(fd, 'wb') as stream:
        stream.write(b'NetBox upgrade pending; inspect audit and recovery before clearing.\n')
        stream.flush()
        os.fsync(stream.fileno())


def finish_maintenance(path: Path = MARKER) -> None:
    """Remove only a private root-owned marker after successful health checks."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise UpgradeExecutionError('Unsafe maintenance marker')
    path.unlink()
