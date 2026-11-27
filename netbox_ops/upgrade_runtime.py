"""Read-only checks that the backup and upgrade target the running installation."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .backups import _sql
from .config import Configuration
from .restore_environment import netbox_runtime
from .upgrade_discovery import GitNetBoxInstallation
from .upgrade_execution import UpgradeExecutionError

# Compare the real database connection with the local postgres backup connection.
# The identity is not logged and contains no password or connection string.
_IDENTITY_SQL = (
    "SELECT current_database() || '|' || current_setting('port') || '|' || "
    "extract(epoch from pg_postmaster_start_time())::text"
)

_PROBE = """
import json
import os
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
from django.conf import settings
from django.db import connections
databases = settings.DATABASES
if set(databases) != {'default'}:
    raise SystemExit(1)
db = databases['default']
if db.get('ENGINE') != 'django.db.backends.postgresql':
    raise SystemExit(1)
if db.get('NAME') != os.environ.pop('NBO_EXPECTED_DB'):
    raise SystemExit(1)
if Path(settings.MEDIA_ROOT).resolve(strict=True) != Path(os.environ.pop('NBO_EXPECTED_MEDIA')):
    raise SystemExit(1)
if sorted(settings.PLUGINS) != json.loads(os.environ.pop('NBO_EXPECTED_PLUGINS')):
    raise SystemExit(1)
with connections['default'].cursor() as cursor:
    cursor.execute(%r)
    print(cursor.fetchone()[0])
""" % _IDENTITY_SQL


def upgrade_environment(base_python: Path) -> dict[str, str]:
    """Do not pass inherited service, Git or Python overrides to the root script."""
    return {
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': '/root', 'LANG': 'C.UTF-8', 'PYTHON': str(base_python),
        'PIP_NO_INPUT': '1', 'GIT_TERMINAL_PROMPT': '0',
    }


def validate_upgrade_runtime(
    cfg: Configuration, installation: GitNetBoxInstallation, base_python: Path,
    enabled_plugins: tuple[str, ...],
    *, runner: Callable = subprocess.run, discover: Callable = netbox_runtime,
    sql: Callable = _sql,
) -> None:
    """Verify both writers and the upgrade script will use the backed-up DB/media.

    Support the standard Git layout, as the existing backup version detection
    reads /opt/netbox. A mismatched service, database, media path or environment
    is a blocker, not an unverified compatibility warning.
    """
    try:
        root = installation.root
        if root != Path('/opt/netbox'):
            raise ValueError
        units = [cfg.units[name] for name in ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        if len(set(units)) != len(units):
            raise ValueError
        media = cfg.media_directory.resolve(strict=True)
        if media != cfg.media_directory or not media.is_dir():
            raise ValueError
        # The recovery point must not live in the worktree or media being copied.
        backup = cfg.backup_directory.resolve()
        if (backup != cfg.backup_directory or backup.is_relative_to(root)
                or root.is_relative_to(backup) or backup.is_relative_to(media)
                or media.is_relative_to(backup)):
            raise ValueError
        expected = sql(_IDENTITY_SQL, database=cfg.database, cfg=cfg)
        if not expected.startswith(cfg.database + '|'):
            raise ValueError
        for name in ('netbox', 'netbox-rq'):
            service_cfg = replace(cfg, units={**cfg.units, 'netbox': cfg.units[name]})
            runtime = discover(service_cfg, runner=runner)
            if (runtime.python != installation.python
                    or runtime.working_directory != root / 'netbox'):
                raise ValueError
            account = (pwd.getpwuid(int(runtime.username)) if runtime.username.isdigit()
                       else pwd.getpwnam(runtime.username))
            if account.pw_uid == 0:
                raise ValueError
            # Probe the actual service environment and the restricted script
            # environment as the non-root NetBox user. Never execute a
            # root-owned helper with environment read from a service process.
            for environment in (runtime.environment, upgrade_environment(base_python)):
                env = dict(environment)
                env['NBO_EXPECTED_DB'] = cfg.database
                env['NBO_EXPECTED_MEDIA'] = str(media)
                env['NBO_EXPECTED_PLUGINS'] = json.dumps(sorted(enabled_plugins))
                result = runner(
                    [str(runtime.python), '-c', _PROBE], cwd=runtime.working_directory,
                    env=env, user=account.pw_uid, group=account.pw_gid,
                    extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if result.returncode or result.stdout.strip() != expected:
                    raise ValueError
    except Exception:
        raise UpgradeExecutionError(
            'NetBox services, upgrade environment and local database/media backup targets '
            'could not be matched'
        ) from None

# NetBox 4.7+: catalogue availability is distinct from installation privileges.
# Use a read-only transaction; never create extensions during plan review.
_LTREE_SQL = f"""
SELECT
    ({_IDENTITY_SQL}),
    current_setting('server_version_num'), current_user::text,
    session_user::text, COALESCE(current_schema(), ''), current_setting('search_path'),
    COALESCE((SELECT n.nspname FROM pg_extension e JOIN pg_namespace n
              ON n.oid = e.extnamespace WHERE e.extname = 'ltree'), ''),
    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'ltree'),
    EXISTS (
        SELECT 1 FROM pg_extension e
        JOIN pg_type t ON t.typnamespace = e.extnamespace AND t.typname = 'ltree'
        WHERE e.extname = 'ltree' AND pg_type_is_visible(t.oid)
          AND has_schema_privilege(e.extnamespace, 'USAGE')
          AND has_type_privilege(t.oid, 'USAGE')
    ),
    EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'ltree'
            AND default_version IS NOT NULL),
    EXISTS (
        SELECT 1 FROM pg_available_extensions a
        JOIN pg_available_extension_versions v
          ON v.name = a.name AND v.version = a.default_version
        WHERE a.name = 'ltree' AND (v.trusted OR NOT v.superuser
              OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper))
    ) AND has_database_privilege(current_database(), 'CREATE')
      AND COALESCE(has_schema_privilege(current_schema(), 'CREATE'), false),
    COALESCE(has_schema_privilege(current_schema(), 'USAGE'), false)
"""

_LTREE_PROBE = """
import json, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
from django.conf import settings
from django.db import connections, transaction
db = settings.DATABASES.get('default', {})
if (set(settings.DATABASES) != {'default'}
        or db.get('ENGINE') != 'django.db.backends.postgresql'
        or db.get('NAME') != os.environ.pop('NBO_EXPECTED_DB')):
    print(json.dumps({'status': 'mismatch'}))
else:
    migration = os.environ.pop('NBO_MIGRATION_CONTEXT') == '1'
    # Do not assume the root upgrade script can use a service account's
    # peer/OS-user-dependent database authentication.
    if migration and (not db.get('USER') or not db.get('HOST')
                      or str(db['HOST']).startswith('/')):
        print(json.dumps({'status': 'unknown'}))
    else:
        with transaction.atomic(using='default'):
            with connections['default'].cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                cursor.execute(%r)
                row = cursor.fetchone()
        if migration and row[3] != db['USER']:
            print(json.dumps({'status': 'mismatch'}))
        else:
            print(json.dumps(row))
""" % _LTREE_SQL


@dataclass(frozen=True)
class LtreeEvidence:
    status: str
    details: tuple[str, ...] = ()
    identity: str = ''


def probe_ltree(cfg: Configuration, live: GitNetBoxInstallation, *,
                runner: Callable = subprocess.run, discover: Callable = netbox_runtime,
                sql: Callable = _sql) -> LtreeEvidence:
    """Match the backup database and check the service and migration roles."""
    rows = []
    details = []
    identity = ''
    try:
        expected = sql(_IDENTITY_SQL, database=cfg.database, cfg=cfg)
        if not expected.startswith(cfg.database + '|'):
            return LtreeEvidence('mismatch')
        identity = hashlib.sha256(expected.encode()).hexdigest()
        for name in ('netbox', 'netbox-rq'):
            service_cfg = replace(cfg, units={**cfg.units, 'netbox': cfg.units[name]})
            runtime = discover(service_cfg, runner=runner)
            if runtime.python != live.python or runtime.working_directory != live.root / 'netbox':
                return LtreeEvidence('mismatch', tuple(details), identity)
            account = (pwd.getpwuid(int(runtime.username)) if runtime.username.isdigit()
                       else pwd.getpwnam(runtime.username))
            if account.pw_uid == 0:
                return LtreeEvidence('unknown', tuple(details), identity)
            for migration, environment in ((False, runtime.environment),
                                           (True, upgrade_environment(live.python))):
                env = dict(environment)
                env['NBO_EXPECTED_DB'] = cfg.database
                env['NBO_MIGRATION_CONTEXT'] = str(int(migration))
                result = runner(
                    [str(runtime.python), '-c', _LTREE_PROBE], cwd=runtime.working_directory,
                    env=env, user=account.pw_uid, group=account.pw_gid,
                    extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                    capture_output=True, text=True, timeout=30, check=False,
                )
                values = json.loads(result.stdout) if not result.returncode else None
                if isinstance(values, dict) and values.get('status') in ('unknown', 'mismatch'):
                    return LtreeEvidence(values['status'], tuple(details), identity)
                if (not isinstance(values, list) or len(values) != 12
                        or any(type(x) is not str for x in values[:7])
                        or any(type(x) is not bool for x in values[7:])):
                    return LtreeEvidence('unknown', tuple(details), identity)
                actual, version, role, session, schema, search, extension, installed, usable, available, permitted, usage = values
                if actual != expected:
                    return LtreeEvidence('mismatch', tuple(details), identity)
                # Only catalogue fields; never subprocess errors or connection strings.
                fields = tuple(''.join(c if c.isprintable() else '?' for c in x)[:160]
                               for x in (version, role, session, schema, search, extension))
                context = 'migration' if migration else 'service'
                details.append(f'{name} / {context}: PostgreSQL={fields[0]}, role={fields[1]}, '
                               f'session={fields[2]}, schema={fields[3]}, search_path={fields[4]}, '
                               f'extension_schema={fields[5]}, installed={installed}, '
                               f'usable={usable}, available={available}, install_allowed={permitted}')
                rows.append((migration, schema, installed, usable, available, permitted, usage))
    except Exception:
        return LtreeEvidence('unknown', tuple(details), identity)
    if not rows:
        status = 'unknown'
    elif all(row[2] for row in rows):
        status = 'installed' if all(row[3] for row in rows) else 'unusable'
    elif any(row[2] for row in rows):
        status = 'unknown'
    elif not all(row[4] for row in rows):
        status = 'missing' if not any(row[4] for row in rows) else 'unknown'
    elif not all(row[5] for row in rows if row[0]):
        status = 'permission'
    elif len({row[1] for row in rows}) != 1 or not all(row[1] and row[6] for row in rows):
        status = 'unusable'
    else:
        status = 'available'
    return LtreeEvidence(status, tuple(details), identity)
