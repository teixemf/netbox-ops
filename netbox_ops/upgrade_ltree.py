"""Explicit, fingerprinted installation of ltree on a verified local database."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .backups import BackupManager, _sql
from .config import Configuration
from .i18n import Translator
from .services import ExclusiveLock, upgrade_recovery_pending, write_audit
from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .upgrade_execution import UpgradeExecutionError
from .upgrade_recovery import _private_directory, _private_file
from .upgrade_runtime import LtreeEvidence, _IDENTITY_SQL, _ltree_admin_sql, probe_ltree


@dataclass(frozen=True)
class LtreeInstallReview:
    installation: GitNetBoxInstallation
    evidence: LtreeEvidence
    database: str
    backup_directory: Path
    fingerprint: str | None


def build_ltree_review(cfg: Configuration, *, root: Path = Path('/opt/netbox')) -> LtreeInstallReview:
    """Read-only review, never infer an administrative target."""
    live = inspect_git_installation(root)
    evidence = probe_ltree(cfg, live)
    fingerprint = None
    if root == Path('/opt/netbox') and evidence.status == 'permission' and evidence.admin_port:
        payload = (str(live.root), live.commit, live.version, str(live.python), live.python_version,
                   cfg.database, cfg.database_owner, str(cfg.backup_directory),
                   str(cfg.pg_bin_directory), cfg.postgres_port, sorted(cfg.units.items()),
                   evidence.status, evidence.details, evidence.identity, evidence.admin_port)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return LtreeInstallReview(live, evidence, cfg.database, cfg.backup_directory, fingerprint)


def ltree_review_lines(review: LtreeInstallReview, t: Translator) -> list[str]:
    lines = [t('upgrade_ltree_status', status=t('upgrade_ltree_' + review.evidence.status)),
             *review.evidence.details]
    if review.fingerprint:
        lines.extend((t('upgrade_ltree_admin_review', database=review.database,
                        port=review.evidence.admin_port, backup=review.backup_directory),
                      t('upgrade_ltree_admin_fingerprint', fingerprint=review.fingerprint)))
    elif review.evidence.status not in ('installed', 'available'):
        lines.append(t('upgrade_ltree_admin_unavailable'))
    return lines


def apply_ltree_review(cfg: Configuration, review: LtreeInstallReview, fingerprint: str, *,
                       progress: Callable[[str, int, int], None] | None = None) -> Path:
    """Verify the exact local target and backup before the single fixed DDL operation."""
    if os.geteuid() != 0 or not review.fingerprint or fingerprint != review.fingerprint:
        raise UpgradeExecutionError('Root and exact ltree installation approval are required')

    def report(phase, done):
        if progress:
            try:
                progress(phase, done, 3)
            except Exception:
                pass

    with ExclusiveLock():
        if upgrade_recovery_pending():
            raise UpgradeExecutionError('NetBox upgrade recovery is pending; inspect recovery first')
        report('preflight', 0)
        fresh = build_ltree_review(cfg, root=review.installation.root)
        if fresh != review or fresh.fingerprint != fingerprint:
            raise UpgradeExecutionError('PostgreSQL ltree target changed after approval')
        live = fresh.installation
        backup = cfg.backup_directory
        if (backup.is_relative_to(live.root) or live.root.is_relative_to(backup)
                or backup.is_relative_to(cfg.media_directory)
                or cfg.media_directory.is_relative_to(backup)):
            raise UpgradeExecutionError('Unsafe ltree backup directory')
        _private_directory(backup)
        manager = BackupManager(cfg)
        path = None
        try:
            report('backup', 1)
            result = manager._backup('db')
            path = result.path
            if result.kind != 'db' or path.parent != backup:
                raise UpgradeExecutionError('Unexpected ltree database backup result')
            _private_file(path)
            _private_file(Path(str(path) + '.json'))
            verified = manager.verify('db', path)
            if (not isinstance(verified, dict) or verified.get('kind') != 'db'
                    or verified.get('database') != cfg.database
                    or verified.get('netbox_version') != live.version):
                raise UpgradeExecutionError('ltree database backup did not match the installation')
            # The operation lock does not prevent an external DBA changing the
            # database. Re-probe after backup and guard inside the DDL transaction.
            if build_ltree_review(cfg, root=live.root) != review:
                raise UpgradeExecutionError('PostgreSQL ltree target changed after backup')
            identity = _sql(_IDENTITY_SQL, database=cfg.database, cfg=cfg)
            if hashlib.sha256(identity.encode()).hexdigest() != review.evidence.identity:
                raise UpgradeExecutionError('PostgreSQL ltree database identity changed')
            write_audit(f'LTREE INSTALL requested database={cfg.database} backup={path.name}')
            report('stage', 2)
            if _ltree_admin_sql(cfg, identity, review.evidence.admin_port, install=True) != 'installed':
                raise UpgradeExecutionError('PostgreSQL ltree administrative guard refused installation')
            observed = probe_ltree(cfg, live)
            if observed.status != 'installed' or observed.identity != review.evidence.identity:
                raise UpgradeExecutionError('PostgreSQL ltree installation could not be verified')
            write_audit(f'LTREE INSTALL OK database={cfg.database} backup={path.name}')
            report('validated', 3)
            return path
        except (Exception, KeyboardInterrupt):
            # Never remove an extension or grant privileges on failure.
            try:
                write_audit(f'LTREE INSTALL not verified database={cfg.database}')
            except Exception:
                pass
            if path is None:
                raise UpgradeExecutionError('Cannot prepare verified database backup for ltree installation') from None
            raise UpgradeExecutionError(
                f'ltree installation not verified; backup: {path}; inspect database; no automatic rollback'
            ) from None


def run_ltree_cli(args, cfg: Configuration, t: Translator) -> int:
    review = build_ltree_review(cfg)
    for line in ltree_review_lines(review, t):
        print(line)
    if not args.install:
        if args.approve_ltree:
            raise UpgradeExecutionError('Use --install with the exact ltree review fingerprint')
        return 0 if review.fingerprint or review.evidence.status in ('installed', 'available') else 1
    backup = apply_ltree_review(cfg, review, args.approve_ltree or '')
    print(t('upgrade_ltree_admin_success', backup=backup))
    return 0
