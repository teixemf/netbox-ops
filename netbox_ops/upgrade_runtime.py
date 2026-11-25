"""Read-only checks that the backup and upgrade target the running installation."""
from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import replace
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

# NetBox 4.7+: ltree is trusted; a missing extension requires CREATE on the
# database and target schema. Query catalogs only, never CREATE EXTENSION.
_LTREE_SQL = """
SELECT
    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'ltree'),
    EXISTS (
        SELECT 1 FROM pg_extension e
        JOIN pg_type t ON t.typnamespace = e.extnamespace AND t.typname = 'ltree'
        WHERE e.extname = 'ltree'
          AND pg_type_is_visible(t.oid)
          AND has_schema_privilege(e.extnamespace, 'USAGE')
          AND has_type_privilege(t.oid, 'USAGE')
    ),
    EXISTS (
        SELECT 1 FROM pg_available_extensions a
        JOIN pg_available_extension_versions v
          ON v.name = a.name AND v.version = a.default_version
        WHERE a.name = 'ltree' AND (v.trusted OR NOT v.superuser)
    ),
    has_database_privilege(current_database(), 'CREATE')
        AND COALESCE(has_schema_privilege(current_schema(), 'CREATE'), false)
"""
_LTREE_PROBE = """
import json, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
from django.db import connections
with connections['default'].cursor() as cursor:
    cursor.execute(%r)
    print(json.dumps(cursor.fetchone()))
""" % _LTREE_SQL


def probe_ltree(cfg: Configuration, live: GitNetBoxInstallation, *,
                runner: Callable = subprocess.run, discover: Callable = netbox_runtime) -> str:
    """Check both actual service and restricted script environments without writes."""
    states = []
    try:
        for name in ('netbox', 'netbox-rq'):
            service_cfg = replace(cfg, units={**cfg.units, 'netbox': cfg.units[name]})
            runtime = discover(service_cfg, runner=runner)
            if runtime.python != live.python or runtime.working_directory != live.root / 'netbox':
                return 'unknown'
            account = (pwd.getpwuid(int(runtime.username)) if runtime.username.isdigit()
                       else pwd.getpwnam(runtime.username))
            if account.pw_uid == 0:
                return 'unknown'
            for env in (runtime.environment, upgrade_environment(live.python)):
                result = runner(
                    [str(runtime.python), '-c', _LTREE_PROBE], cwd=runtime.working_directory,
                    env=env, user=account.pw_uid, group=account.pw_gid,
                    extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                    capture_output=True, text=True, timeout=30, check=False,
                )
                values = json.loads(result.stdout) if not result.returncode else None
                if not isinstance(values, list) or len(values) != 4 or any(type(x) is not bool for x in values):
                    return 'unknown'
                installed, usable, available, permitted = values
                if installed:
                    states.append('installed' if usable else 'permission')
                elif not available:
                    states.append('missing')
                else:
                    states.append('available' if permitted else 'permission')
    except Exception:
        return 'unknown'
    for status in ('missing', 'permission', 'unknown', 'available'):
        if status in states:
            return status
    return 'installed' if states else 'unknown'
