"""Root-managed local configuration. Media backups never include application secrets."""
from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .platform import default_units, discover_service_units, PlatformError, postgres_cluster_port, has_postgres_clusters, detect_platform

CONFIG_PATH = Path('/etc/netbox-ops/config.json')
SERVICE_NAMES = ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')
UNIT_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$')
DEFAULT_UNITS = {
    'netbox': 'netbox.service', 'netbox-rq': 'netbox-rq.service',
    'nginx': 'nginx.service', 'postgresql': 'postgresql.service',
    'redis': 'redis.service',
}
DEFAULT_MEDIA_DIR = Path('/opt/netbox/netbox/media')
DEFAULT_BACKUP_DIR = Path('/var/lib/netbox-ops/backups')
DATABASE_PATTERN = re.compile(r'^[a-z][a-z0-9_]{0,39}$')
DEFAULT_RESTART = ('netbox', 'netbox-rq')


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Configuration:
    units: dict[str, str]
    backup_directory: Path = DEFAULT_BACKUP_DIR
    database: str = 'netbox'
    database_owner: str = 'netbox'
    media_directory: Path = DEFAULT_MEDIA_DIR
    pg_bin_directory: Path | None = None
    postgres_port: int | None = None

    @property
    def artifacts(self) -> tuple[tuple[str, Path, bool], ...]:
        """Compatibility with the archive backend: MEDIA ONLY, no other files."""
        return (('media', self.media_directory, True),)


def _absolute_safe_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith('/'):
        raise ValueError(f'{label} must be an absolute directory')
    path = Path(value)
    if path == Path('/') or '..' in path.parts:
        raise ValueError(f'{label} must not contain traversal or root path')
    return path


def _secure_config_text(path: Path) -> str | None:
    """Read a root-controlled regular file, without following any symlink."""
    path = Path(path)
    if path == Path('/') or '..' in path.parts:
        raise ConfigError(f'{path}: configuration path must not be root or contain traversal')
    # Keep supporting relative --config paths, but only from trusted directories.
    if not path.is_absolute():
        path = Path.cwd() / path

    directory_fd = None
    try:
        directory_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_info = os.fstat(directory_fd)
        if root_info.st_uid != 0 or root_info.st_mode & 0o022:
            raise ConfigError(f'{path}: unsafe configuration directory: /')
        # Opening each directory relative to its parent's fd prevents a
        # path-based check/open race and disallows symlinks in any component.
        for component in path.parent.parts[1:]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
            except FileNotFoundError:
                return None  # Missing config retains the built-in defaults.
            try:
                info = os.fstat(next_fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ConfigError(f'{path}: unsafe configuration directory: {component}')
            except BaseException:
                os.close(next_fd)
                raise
            os.close(directory_fd)
            directory_fd = next_fd

        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | os.O_NONBLOCK
        try:
            file_fd = os.open(path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ConfigError(f'{path}: configuration must be a regular file')
            if info.st_uid != 0:
                raise ConfigError(f'{path}: configuration must be owned by root')
            if info.st_mode & 0o077:
                raise ConfigError(f'{path}: configuration must not be accessible by group or others')
            with os.fdopen(file_fd, 'r', encoding='utf-8') as stream:
                file_fd = -1
                return stream.read()
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    except ConfigError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ConfigError(f'{path}: symbolic links or non-directories are not allowed') from exc
        raise ConfigError(f'{path}: cannot read configuration: {exc.strerror}') from exc
    except UnicodeError as exc:
        raise ConfigError(f'{path}: configuration must be valid UTF-8') from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def load_config(path: Path = CONFIG_PATH) -> Configuration:
    raw_config = _secure_config_text(path)
    # Explicit unit overrides always win. Discovery is cached per process to
    # avoid a systemctl probe on every menu refresh.
    try:
        units = default_units().copy()
    except PlatformError as exc:
        # With an explicit configuration, defer discovery until we know which
        # units are actually omitted; fully pinned configurations remain usable.
        if raw_config is None:
            raise ConfigError(str(exc)) from exc
        units = discover_service_units(detect_platform().family, skip_postgres=True)
        discovery_error = exc
    else:
        discovery_error = None
    if raw_config is None:
        port = (postgres_cluster_port(units['postgresql'])
                if detect_platform().family == 'debian' else None)
        return Configuration(units, postgres_port=port)
    try:
        data = json.loads(raw_config)
        if not isinstance(data, dict) or set(data) - {'services', 'backup'}:
            raise ValueError('top-level keys: only services and backup are supported')
        overrides = data.get('services', {})
        if not isinstance(overrides, dict):
            raise ValueError('services must be an object')
        if discovery_error and 'postgresql' not in overrides:
            raise ConfigError(str(discovery_error))
        for name, unit in overrides.items():
            if name not in SERVICE_NAMES:
                raise ValueError(f'unknown service: {name}')
            if not isinstance(unit, str) or not UNIT_PATTERN.fullmatch(unit):
                raise ValueError(f'invalid systemd unit for {name}')
            units[name] = unit
        backup = data.get('backup', {})
        if not isinstance(backup, dict) or set(backup) - {'directory', 'database', 'owner', 'media_directory', 'artifacts', 'pg_bin_directory', 'postgres_port'}:
            raise ValueError('backup supports directory, database, owner, media_directory and legacy artifacts')
        directory = _absolute_safe_path(backup.get('directory', str(DEFAULT_BACKUP_DIR)), 'backup.directory')
        database = backup.get('database', 'netbox')
        owner = backup.get('owner', 'netbox')
        if not isinstance(database, str) or not DATABASE_PATTERN.fullmatch(database):
            raise ValueError('invalid backup.database')
        if not isinstance(owner, str) or not DATABASE_PATTERN.fullmatch(owner):
            raise ValueError('invalid backup.owner')
        media = _absolute_safe_path(backup.get('media_directory', str(DEFAULT_MEDIA_DIR)), 'backup.media_directory')
        # Read a previously installed 0.2.x config without silently backing up
        # configuration.py, LDAP secrets, Nginx or any other legacy artifact.
        # The new explicit media_directory takes precedence.
        legacy = backup.get('artifacts', None)
        if legacy is not None:
            if not isinstance(legacy, list) or any(not isinstance(item, dict) for item in legacy):
                raise ValueError('legacy backup.artifacts must be a list of objects')
            if 'media_directory' not in backup:
                matches = [entry for entry in legacy if entry.get('id') == 'media']
                if len(matches) > 1:
                    raise ValueError('duplicate legacy media artifact')
                if matches:
                    media = _absolute_safe_path(matches[0].get('path'), 'legacy media path')
        bin_directory = backup.get('pg_bin_directory')
        if bin_directory is not None:
            bin_directory = _absolute_safe_path(bin_directory, 'backup.pg_bin_directory')
            if bin_directory.is_symlink() or not bin_directory.is_dir():
                raise ValueError('backup.pg_bin_directory must be an existing non-symlink directory')
            info = bin_directory.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('backup.pg_bin_directory must be root-controlled and not writable by group/others')
        postgres_port = backup.get('postgres_port')
        if postgres_port is not None and (type(postgres_port) is not int or not 1 <= postgres_port <= 65535):
            raise ValueError('backup.postgres_port must be a TCP port (1–65535)')
        if detect_platform().family == 'debian' and units['postgresql'] == 'postgresql.service' and has_postgres_clusters():
            raise ValueError('Debian postgresql.service is not a cluster unit; configure services.postgresql (e.g. postgresql@16-main.service)')
        if postgres_port is None and detect_platform().family == 'debian':
            postgres_port = postgres_cluster_port(units['postgresql'])
            if units['postgresql'].startswith('postgresql@') and postgres_port is None:
                raise ValueError('Cannot resolve Debian PostgreSQL cluster port; set backup.postgres_port')
        if media == directory or directory in media.parents or media in directory.parents:
            raise ValueError('backup and media directories must not overlap')
        return Configuration(units, directory, database, owner, media, bin_directory, postgres_port)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ConfigError(f'{path}: {exc}') from exc
