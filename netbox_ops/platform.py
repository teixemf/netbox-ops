"""RHEL/Debian systemd and PostgreSQL discovery (no package modifications).

User-configured unit names always win. Discovery never starts services; in
ambiguous multi-cluster situations we fail closed rather than selecting a
potentially unrelated PostgreSQL database.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class PlatformError(Exception):
    pass


@dataclass(frozen=True)
class LinuxPlatform:
    family: str
    distro: str
    version: str


def detect_platform(os_release: Path = Path('/etc/os-release')) -> LinuxPlatform:
    data: dict[str, str] = {}
    try:
        for line in os_release.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, val = line.split('=', 1)
                data[key] = val.strip().strip('"\'')
    except OSError:
        return LinuxPlatform('other', 'unknown', '')
    distro = data.get('ID', 'unknown').lower()
    family_ids = set((distro + ' ' + data.get('ID_LIKE', '').lower()).split())
    family = ('debian' if family_ids.intersection({'debian', 'ubuntu'}) else
              'rhel' if family_ids.intersection({'rhel', 'fedora', 'centos'}) else 'other')
    return LinuxPlatform(family, distro, data.get('VERSION_ID', ''))


def _run(args: list[str], timeout: int = 3) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pg_clusters(runner=_run) -> list[tuple[str, str, str]]:
    """pg_lsclusters columns: version name port status ... ."""
    result = runner(['pg_lsclusters', '--no-header'])
    if result is None or result.returncode:
        return []
    clusters = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and re.fullmatch(r'[0-9]+', parts[0]) and re.fullmatch(r'[\w-]+', parts[1]):
            clusters.append((parts[0], parts[1], parts[3]))
    return clusters


def _is_loaded(unit: str, runner=_run) -> bool:
    response = runner(['systemctl', 'show', '--property=LoadState', '--value', unit])
    return bool(response and response.returncode == 0 and response.stdout.strip() == 'loaded')


def discover_service_units(family: str, runner=_run, *, skip_postgres: bool = False) -> dict[str, str]:
    units = {
        'netbox': 'netbox.service', 'netbox-rq': 'netbox-rq.service',
        'nginx': 'nginx.service', 'postgresql': 'postgresql.service',
        'redis': 'redis.service',
    }
    if family == 'debian':
        units['redis'] = next((item for item in ('redis-server.service', 'redis.service')
                               if _is_loaded(item, runner)), 'redis-server.service')
        clusters = _pg_clusters(runner)
        if len(clusters) > 1 and not skip_postgres:
            raise PlatformError('Multiple PostgreSQL clusters detected. Configure services.postgresql explicitly.')
        if len(clusters) == 1 and not skip_postgres:
            version, name, _status = clusters[0]
            units['postgresql'] = f'postgresql@{version}-{name}.service'
        # Otherwise retain postgresql.service, potentially uninstalled. It will
        # appear as unavailable; do not guess a specific database cluster.
    elif family == 'rhel':
        # One query is preferable to probing every versioned PGDG unit when
        # systemd is slow or unavailable (SSH/TUI responsiveness).
        listed = runner(['systemctl', 'list-unit-files', '--type=service',
                         '--no-legend', '--no-pager'])
        installed: list[str] = []
        if listed is not None and listed.returncode == 0:
            installed = [line.split()[0] for line in listed.stdout.splitlines()
                         if line.split() and re.fullmatch(r'postgresql(?:-[0-9]+)?\.service', line.split()[0])]
        candidates = ['postgresql.service'] + [f'postgresql-{v}.service' for v in range(21, 11, -1)]
        loaded = [name for name in candidates if name in installed] if installed else [name for name in candidates if _is_loaded(name, runner)]
        if len(loaded) > 1 and not skip_postgres:
            raise PlatformError('Multiple PostgreSQL service units detected. Configure services.postgresql explicitly.')
        if loaded and not skip_postgres:
            units['postgresql'] = loaded[0]
        units['redis'] = next((item for item in ('redis.service', 'redis-server.service')
                               if _is_loaded(item, runner)), 'redis.service')
    return units


@lru_cache(maxsize=1)
def default_units() -> dict[str, str]:
    return discover_service_units(detect_platform().family)


def pg_binary(name: str, configured_bin_dir: Path | None = None) -> str:
    """Find PG client tools without assuming the root PATH includes PGDG binaries.

    For custom PGDG installs with multiple versions, configure the binary
    directory explicitly. Never silently choose between multiple candidates.
    """
    if name not in {'pg_dump', 'pg_restore', 'psql', 'createdb', 'dropdb'}:
        raise PlatformError('Unsupported PostgreSQL command')
    if configured_bin_dir is not None:
        candidate = configured_bin_dir / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise PlatformError(f'PostgreSQL executable missing: {candidate}')
        return str(candidate)
    executable = shutil.which(name)
    if executable:
        return executable
    candidates = sorted(Path('/usr').glob(f'pgsql-*/bin/{name}'))
    binaries = [p for p in candidates if p.is_file() and os.access(p, os.X_OK)]
    if len(binaries) == 1:
        return str(binaries[0])
    if len(binaries) > 1:
        raise PlatformError('Multiple PostgreSQL binary versions: configure backup.pg_bin_directory')
    raise PlatformError(f'PostgreSQL tool unavailable: {name}')


def has_postgres_clusters(runner=_run) -> bool:
    return bool(_pg_clusters(runner))


def postgres_cluster_port(unit: str, runner=_run) -> int | None:
    """Resolve the selected Debian cluster's port, never another cluster's."""
    match = re.fullmatch(r'postgresql@([0-9]+)-([A-Za-z0-9_-]+)\.service', unit)
    if not match:
        return None
    result = runner(['pg_lsclusters', '--no-header'])
    if result is None or result.returncode:
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and (fields[0], fields[1]) == match.groups():
            try:
                port = int(fields[2])
            except ValueError:
                return None
            return port if 1 <= port <= 65535 else None
    return None
