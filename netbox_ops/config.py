"""Root-managed local configuration. Media backups never include application secrets."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

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


def load_config(path: Path = CONFIG_PATH) -> Configuration:
    units = DEFAULT_UNITS.copy()
    if not path.exists():
        return Configuration(units)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) - {'services', 'backup'}:
            raise ValueError('top-level keys: only services and backup are supported')
        overrides = data.get('services', {})
        if not isinstance(overrides, dict):
            raise ValueError('services must be an object')
        for name, unit in overrides.items():
            if name not in SERVICE_NAMES:
                raise ValueError(f'unknown service: {name}')
            if not isinstance(unit, str) or not UNIT_PATTERN.fullmatch(unit):
                raise ValueError(f'invalid systemd unit for {name}')
            units[name] = unit
        backup = data.get('backup', {})
        if not isinstance(backup, dict) or set(backup) - {'directory', 'database', 'owner', 'media_directory', 'artifacts'}:
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
        if media == directory or directory in media.parents or media in directory.parents:
            raise ValueError('backup and media directories must not overlap')
        return Configuration(units, directory, database, owner, media)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ConfigError(f'{path}: {exc}') from exc
