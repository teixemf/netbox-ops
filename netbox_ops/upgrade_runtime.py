"""Read-only checks that the backup and upgrade target the running installation."""
from __future__ import annotations

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
