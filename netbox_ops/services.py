"""Read systemd state and journal; control configured services with explicit actions."""
from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import re
import time
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .config import Configuration, SERVICE_NAMES
from .netbox_version import read_netbox_version

LOCK_PATH = Path('/run/lock/netbox-ops.lock')
LOG_DIR = Path('/var/log/netbox-ops')
LOG_PATH = LOG_DIR / 'operations.log'


class ServiceError(Exception):
    pass


@dataclass(frozen=True)
class ServiceState:
    name: str
    unit: str
    loaded: str
    active: str
    substate: str
    enabled: str
    main_pid: str = "0"
    cpu_usage_nsec: int | None = None
    memory_current: int | None = None
    active_enter_usec: int | None = None

    @property
    def healthy(self) -> bool:
        return self.loaded == 'loaded' and self.active == 'active'

    @property
    def uptime_seconds(self) -> int | None:
        """Time elapsed since the unit entered active state, from boot clock."""
        if not self.healthy or not self.active_enter_usec:
            return None
        elapsed = time.monotonic() - self.active_enter_usec / 1_000_000
        return max(0, int(elapsed)) if elapsed >= 0 else None


class SystemdServices:
    def __init__(self, config: Configuration, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.config = config
        self.runner = runner

    def _run(self, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(args, capture_output=True, text=True, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(f'{args[0]}: {exc}') from exc

    def status(self, names: list[str] | tuple[str, ...] = SERVICE_NAMES) -> list[ServiceState]:
        states = []
        for name in names:
            if name not in self.config.units:
                raise ServiceError(f'Unknown service: {name}')
            unit = self.config.units[name]
            result = self._run(['systemctl', 'show', '--no-pager',
                                '--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,CPUUsageNSec,MemoryCurrent,ControlGroup,ActiveEnterTimestampMonotonic', unit])
            if result.returncode:
                raise ServiceError(f'{unit}: {result.stderr.strip() or result.stdout.strip()}')
            values = {}
            for line in result.stdout.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    values[k] = v
            def metric(key: str) -> int | None:
                value = values.get(key, '')
                try:
                    number = int(value)
                    return number if 0 <= number < (1 << 63) else None
                except (TypeError, ValueError):
                    return None
            cpu_ns, memory_bytes = metric('CPUUsageNSec'), metric('MemoryCurrent')
            # RHEL with cgroup v2 may expose live metrics even when systemd
            # CPU/MemoryAccounting properties are unset. Never trust path
            # traversal supplied in a unit's ControlGroup field.
            if cpu_ns is None or memory_bytes is None:
                cg = values.get('ControlGroup', '')
                base = Path('/sys/fs/cgroup')
                if cg.startswith('/') and '..' not in PurePosixPath(cg).parts:
                    try:
                        root = (base / cg.lstrip('/')).resolve(strict=True)
                        if root.is_relative_to(base) and root.is_dir():
                            if memory_bytes is None:
                                raw = (root / 'memory.current').read_text().strip()
                                memory_bytes = int(raw) if raw.isdigit() else None
                            if cpu_ns is None:
                                for line in (root / 'cpu.stat').read_text().splitlines():
                                    if line.startswith('usage_usec '):
                                        cpu_ns = int(line.split()[1]) * 1000
                                        break
                    except (OSError, ValueError, OverflowError):
                        pass
            states.append(ServiceState(name, unit,
                                       values.get('LoadState', 'unknown'),
                                       values.get('ActiveState', 'unknown'),
                                       values.get('SubState', 'unknown'),
                                       values.get('UnitFileState', 'unknown'),
                                       values.get('MainPID', '0'), cpu_ns, memory_bytes, metric('ActiveEnterTimestampMonotonic')))
        return states

    def logs(self, name: str, lines: int = 20) -> list[str]:
        """Return up to `lines` recent journal records without opening a pager."""
        if name not in self.config.units:
            raise ServiceError(f'Unknown service: {name}')
        if type(lines) is not int or not 1 <= lines <= 500:
            raise ServiceError('Log lines must be between 1 and 500')
        result = self._run(['journalctl', '--no-pager', '--quiet',
                            '--output=short-iso', '--unit', self.config.units[name],
                            '--lines', str(lines)], timeout=30)
        if result.returncode:
            raise ServiceError(f'{self.config.units[name]} journal: '
                               f'{result.stderr.strip() or result.stdout.strip()}')
        return [''.join(char if char.isprintable() else ' ' for char in record)
                for record in result.stdout.splitlines()]

    def _change(self, action: str, names: list[str]) -> list[ServiceState]:
        if action not in ('start', 'stop', 'restart', 'enable', 'disable'):
            raise ServiceError(f'Invalid service action: {action}')
        if not names:
            raise ServiceError('No services selected')
        for name in names:
            if name not in self.config.units:
                raise ServiceError(f'Unknown service: {name}')
        states = []
        for name in dict.fromkeys(names):
            unit = self.config.units[name]
            result = self._run(['systemctl', action, unit], timeout=120)
            if result.returncode:
                raise ServiceError(f'{unit}: {result.stderr.strip() or result.stdout.strip()}')
            state = self.status([name])[0]
            states.append(state)
            if action == 'stop':
                if state.active != 'inactive':
                    raise ServiceError(f'{unit}: stop returned success but state is {state.active}')
            elif action in ('start', 'restart') and not state.healthy:
                raise ServiceError(f'{unit}: {action} returned success but state is {state.active}')
            elif action == 'enable' and state.enabled not in ('enabled', 'enabled-runtime'):
                raise ServiceError(f'{unit}: enable returned success but startup is {state.enabled}')
            elif action == 'disable' and state.enabled != 'disabled':
                raise ServiceError(f'{unit}: disable returned success but startup is {state.enabled}')
        return states

    def start(self, names: list[str]) -> list[ServiceState]:
        return self._change('start', names)

    def stop(self, names: list[str]) -> list[ServiceState]:
        return self._change('stop', names)

    def restart(self, names: list[str]) -> list[ServiceState]:
        return self._change('restart', names)

    def enable(self, names: list[str]) -> list[ServiceState]:
        # Never use --now: enabling at boot must not start a service.
        return self._change('enable', names)

    def disable(self, names: list[str]) -> list[ServiceState]:
        # Never use --now: disabling at boot must not stop a service.
        return self._change('disable', names)


def write_audit(message: str, path: Path = LOG_PATH) -> None:
    """Require a root-owned log location for service changes; never silently skip logging."""
    import datetime

    directory = path.parent
    if directory.is_symlink():
        raise ServiceError(f'Unsafe log directory: {directory}')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ServiceError(f'Unsafe log directory permissions: {directory}')
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ServiceError(f'Unsafe log file permissions: {path}')
        date = datetime.datetime.now(datetime.timezone.utc).isoformat()
        os.write(fd, f'{date} {message}\n'.encode('utf-8'))
    finally:
        os.close(fd)


class ExclusiveLock:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.fd: int | None = None

    def __enter__(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        self.fd = os.open(self.path, flags, 0o600)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            os.close(self.fd)
            self.fd = None
            raise ServiceError('Unsafe lock file')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise ServiceError('Another netbox-ops operation is running') from exc
        return self

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ServiceVersionCache:
    """Query installed versions once per netbox-ops process, not per TUI redraw.

    Lookup is asynchronous, so slow NetBox/Django imports cannot freeze navigation.
    Reset after a future successful upgrade implementation changes a component.
    """

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.runner = runner
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='nbo-version')
        self._futures: dict[str, concurrent.futures.Future[str]] = {}

    @staticmethod
    def _canonical(name: str) -> str:
        return 'netbox' if name == 'netbox-rq' else name

    def request(self, name: str) -> None:
        key = self._canonical(name)
        if key not in self._futures:
            self._futures[key] = self._executor.submit(self._lookup, key)

    def get(self, name: str) -> str | None:
        """None means fetching; '—' indicates unavailable."""
        key = self._canonical(name)
        self.request(key)
        future = self._futures[key]
        if not future.done():
            return None
        try:
            return future.result() or '—'
        except Exception:
            return '—'

    def reset(self, names: list[str] | None = None) -> None:
        """Invalidate cached version(s) after a successful upgrade."""
        targets = (list(self._futures) if names is None else [self._canonical(n) for n in names])
        for key in targets:
            self._futures.pop(key, None)

    def _lookup(self, name: str) -> str:
        commands = {
            'nginx': ['nginx', '-v'],
            'postgresql': ['runuser', '-u', 'postgres', '--', 'psql', '-X', '-A', '-t',
                           '-d', 'postgres', '-c', 'SHOW server_version'],
            'redis': ['redis-server', '--version'],
        }
        if name == 'netbox':
            return read_netbox_version() or '—'
        if name not in commands:
            return '—'
        try:
            cp = self.runner(commands[name], text=True, capture_output=True, timeout=12,
                             check=False)
        except (OSError, subprocess.TimeoutExpired):
            return '—'
        if cp.returncode:
            return '—'
        output = (cp.stdout or cp.stderr or '').strip()
        if name == 'nginx':
            match = re.search(r'nginx/([\w.\-]+)', output)
            return match.group(1) if match else '—'
        if name == 'redis':
            match = re.search(r'\bv=([\w.\-]+)', output)
            return match.group(1) if match else '—'
        return output.splitlines()[0][:50] if output else '—'
