"""Install the root-owned netbox-ops logrotate policy without rotating logs."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

POLICY_DIR = Path('/etc/logrotate.d')
LOG_FILE = Path('/var/log/netbox-ops/operations.log')


def policy_text(log_path: Path = LOG_FILE) -> str:
    if not log_path.is_absolute() or any(c.isspace() or c in '{}"\\' for c in str(log_path)):
        raise ValueError('Invalid log path')
    return f"""{log_path} {{
    weekly
    rotate 4
    missingok
    notifempty
    compress
    delaycompress
    su root root
    create 0600 root root
}}
"""


def install_policy(directory: Path = POLICY_DIR, log_path: Path = LOG_FILE) -> bool:
    """Validate before publishing; never replace an administrator's policy."""
    if os.geteuid() != 0:
        raise PermissionError('Logrotate configuration requires root')
    executable = shutil.which('logrotate', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not executable:
        raise OSError('logrotate is unavailable; install the OS logrotate package and rerun upgrade.sh')
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or directory.resolve() != directory):
        raise OSError('Unsafe logrotate configuration directory')
    destination = directory / 'netbox-ops'
    desired = policy_text(log_path).encode()
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != 0
                or existing.st_mode & 0o022 or existing.st_size != len(desired)
                or destination.read_bytes() != desired):
            raise OSError('Existing logrotate policy differs or is unsafe; review it manually')
        return False
    fd, name = tempfile.mkstemp(prefix='.netbox-ops-', dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        # Debug mode checks configuration without rotating or updating state.
        result = subprocess.run([executable, '-d', '-s', '/dev/null', str(temporary)],
                                capture_output=True, text=True, timeout=30, check=False,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise OSError(f'Logrotate policy validation failed: {result.stderr.strip()}')
        # Publish atomically without following or overwriting an existing path.
        os.link(temporary, destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    try:
        if install_policy():
            print('Installed netbox-ops logrotate policy.')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Warning: netbox-ops log rotation was not configured: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
