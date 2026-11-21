"""Resolve the NetBox service runtime without copying configuration or secrets.

The live process is authoritative when available. For stopped services, systemd's
resolved unit properties supply Environment=, EnvironmentFile= and drop-in values.
No environment values, command output or Django tracebacks are logged.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Configuration

_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_USERNAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*\$?$|^[0-9]+$')
_ENVFILE = re.compile(r'(?P<path>/.*?) \(ignore_errors=(?P<ignore>yes|no)\)(?=\s|$)')


class RuntimeErrorSafe(Exception):
    """Public error without secrets from the unit, environment or Django."""


@dataclass(frozen=True)
class NetBoxRuntime:
    username: str
    python: Path
    working_directory: Path
    environment: dict[str, str]


def _assignments(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        items = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise RuntimeErrorSafe('Invalid NetBox service environment settings') from exc
    result = {}
    for item in items:
        name, sep, value = item.partition('=')
        if not sep or not _ASSIGNMENT.fullmatch(name):
            raise RuntimeErrorSafe('Invalid NetBox service environment settings')
        result[name] = value
    return result


def _read_environment_file(path: Path) -> dict[str, str]:
    try:
        contents = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise RuntimeErrorSafe('Cannot read a required NetBox service environment file') from exc
    result = {}
    # EnvironmentFile is not a shell script: never execute it or expand $values.
    lines = iter(contents.splitlines())
    for line in lines:
        while line.endswith('\\'):
            line = line[:-1] + next(lines, '')
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        name, sep, value = stripped.partition('=')
        name = name.strip()
        if not sep or not _ASSIGNMENT.fullmatch(name):
            raise RuntimeErrorSafe('Invalid NetBox service environment file syntax')
        value = value.strip()
        if value.startswith(("'", '"')) or '\\' in value:
            try:
                parsed = shlex.split(value, posix=True)
            except ValueError as exc:
                raise RuntimeErrorSafe('Invalid NetBox service environment file syntax') from exc
            if len(parsed) != 1:
                raise RuntimeErrorSafe('Invalid NetBox service environment file syntax')
            value = parsed[0]
        result[name] = value
    return result


def _environment_files(raw: str) -> list[tuple[Path, bool]]:
    if not raw.strip():
        return []
    matches = list(_ENVFILE.finditer(raw))
    if not matches:
        raise RuntimeErrorSafe('Unsupported NetBox service environment file declaration')
    cursor = 0
    files = []
    for match in matches:
        if raw[cursor:match.start()].strip():
            raise RuntimeErrorSafe('Unsupported NetBox service environment file declaration')
        files.append((Path(match.group('path')), match.group('ignore') == 'yes'))
        cursor = match.end()
    if raw[cursor:].strip():
        raise RuntimeErrorSafe('Unsupported NetBox service environment file declaration')
    return files


def _stopped_service_environment(properties: dict[str, str]) -> dict[str, str]:
    if properties.get('PassEnvironment') or properties.get('PAMName'):
        raise RuntimeErrorSafe('NetBox service environment requires a running service for safe validation')
    result = _assignments(properties.get('Environment', ''))
    for path, optional in _environment_files(properties.get('EnvironmentFiles', '')):
        if optional and not path.exists():
            continue
        result.update(_read_environment_file(path))
    for name in properties.get('UnsetEnvironment', '').split():
        # systemd also supports NAME=VALUE, which removes only matching pairs.
        key, equals, value = name.partition('=')
        if equals:
            if result.get(key) == value:
                result.pop(key, None)
        else:
            result.pop(key, None)
    return result


def _running_service_environment(pid: str) -> dict[str, str]:
    try:
        content = (Path('/proc') / pid / 'environ').read_bytes()
        if not content:
            raise OSError('Empty process environment')
        result = {}
        for entry in content.split(b'\0'):
            if not entry:
                continue
            key, sep, value = entry.partition(b'=')
            if not sep:
                raise ValueError('Invalid process environment')
            name = key.decode('utf-8')
            if not _ASSIGNMENT.fullmatch(name):
                raise ValueError('Invalid environment variable')
            result[name] = value.decode('utf-8')
        return result
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeErrorSafe('Cannot read the running NetBox service environment safely') from exc


def _paths(properties: dict[str, str]) -> tuple[Path, Path]:
    working = properties.get('WorkingDirectory', '')
    command = properties.get('ExecStart', '')
    match = re.search(r'(?:^|[ {;])path=([^ ;}]+)', command)
    if not working.startswith('/') or not match:
        raise RuntimeErrorSafe('Cannot discover the NetBox runtime from its service')
    base = Path(working)
    executable = Path(match.group(1))
    if not executable.is_absolute():
        raise RuntimeErrorSafe('Cannot discover the NetBox executable')
    # gunicorn/uwsgi or python installed in the application's virtualenv.
    python = executable.parent / 'python'
    manage = base / 'manage.py'
    if not manage.is_file():
        manage = base / 'netbox' / 'manage.py'
    if not python.is_file() or not manage.is_file():
        raise RuntimeErrorSafe('Cannot locate NetBox Python and manage.py from its service')
    return python, manage.parent


def netbox_runtime(cfg: Configuration, runner=subprocess.run) -> NetBoxRuntime:
    unit = cfg.units.get('netbox')
    if not unit:
        raise RuntimeErrorSafe('NetBox service is not configured')
    try:
        response = runner(
            ['systemctl', 'show', '--no-pager',
             '--property=User,WorkingDirectory,ExecStart,MainPID,Environment,EnvironmentFiles,UnsetEnvironment,PassEnvironment,PAMName',
             unit],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeErrorSafe('Cannot inspect the configured NetBox service') from exc
    if response.returncode:
        raise RuntimeErrorSafe('Cannot inspect the configured NetBox service')
    properties = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
    username = properties.get('User', '')
    if not _USERNAME.fullmatch(username) or username in ('root', '0'):
        raise RuntimeErrorSafe('NetBox service must have a non-root service user')
    python, cwd = _paths(properties)
    pid = properties.get('MainPID', '')
    environment = (_running_service_environment(pid) if pid.isdigit() and int(pid) > 0
                   else _stopped_service_environment(properties))
    return NetBoxRuntime(username, python, cwd, environment)


# This code runs under the NetBox virtualenv and user, not the netbox-ops venv.
# It changes in-memory Django settings only; it cannot apply migrations.
DJANGO_STAGE_CHECK = """
import io
import sys

stage = os.environ.pop('NBO_STAGE_DATABASE')
active = os.environ.pop('NBO_ACTIVE_DATABASE')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')

# Reject unexpected Django database connections, including during app setup.
from django.db.backends.base.base import BaseDatabaseWrapper
original_ensure = BaseDatabaseWrapper.ensure_connection

def stage_only(self):
    if self.settings_dict.get('NAME') != stage:
        raise RuntimeError('Database access outside staged restore is blocked')
    return original_ensure(self)

BaseDatabaseWrapper.ensure_connection = stage_only

try:
    from django.conf import settings
    databases = settings.DATABASES
    if set(databases) != {'default'}:
        raise RuntimeError('Unsupported multiple database aliases')
    db = databases['default']
    if db.get('ENGINE') != 'django.db.backends.postgresql' or db.get('NAME') != active:
        raise RuntimeError('NetBox database settings do not match the configured backup')
    db['NAME'] = stage

    import django
    django.setup()

    from django.db import connections
    with connections['default'].cursor() as cursor:
        cursor.execute('SELECT current_database()')
        if cursor.fetchone()[0] != stage:
            raise RuntimeError('Staged database connection verification failed')

    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connections['default'])
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        sys.exit(10)

    from django.core.management import call_command
    call_command('migrate', check=True, interactive=False, verbosity=0,
                 stdout=io.StringIO(), stderr=io.StringIO())
except SystemExit:
    raise
except BaseException:
    sys.exit(11)
"""


def check_staged_migrations(cfg: Configuration, database: str, runner=subprocess.run) -> None:
    """Check core and enabled plugin migrations without touching the active DB."""
    if not re.fullmatch(r'nbo_stage_[0-9a-f]{16}', database):
        raise RuntimeErrorSafe('Invalid staged database name')
    runtime = netbox_runtime(cfg)
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'LANG': 'C.UTF-8'}
    env.update(runtime.environment)
    env['NBO_STAGE_DATABASE'] = database
    env['NBO_ACTIVE_DATABASE'] = cfg.database
    try:
        result = runner(['runuser', '-u', runtime.username, '--',
                         str(runtime.python), '-c', DJANGO_STAGE_CHECK],
                        cwd=runtime.working_directory, env=env,
                        capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeErrorSafe('Cannot execute the staged Django migration check') from exc
    # Never include child stdout/stderr: Django or plugins can print secrets.
    if result.returncode == 10:
        raise RuntimeErrorSafe('Restored database has unapplied NetBox or plugin migrations')
    if result.returncode:
        raise RuntimeErrorSafe('Cannot validate NetBox and plugin migrations on staged database')
