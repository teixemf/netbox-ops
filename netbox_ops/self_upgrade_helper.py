"""Coordinator for an external self-upgrade helper process.

The calling CLI must exit before launching the swap, and must supply trusted
preflight and post-install checks. This module has no public CLI entry point
until release compatibility metadata and the launch handshake are available.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .self_upgrade_transaction import (
    SwapError, begin_swap, cleanup_swap, finish_swap, rollback_swap,
)
from .services import ExclusiveLock, LOCK_PATH, LOG_PATH, write_audit


def run_approved_swap(
    staged: Path, *,
    preflight: Callable[[Path], None],
    validate: Callable[[Path], None],
    active: Path = Path('/opt/netbox-ops'),
    lock_path: Path = LOCK_PATH,
    audit_path: Path = LOG_PATH,
) -> None:
    """Perform one checked transaction in the separate helper process.

    Preflight must prove that the current installation and selected release
    are known, compatible and not a downgrade. Validate must check the new
    installation in place. Neither callback may manage NetBox services.
    """
    if os.geteuid() != 0:
        raise SwapError('Self-upgrade requires root privileges')
    if not callable(preflight) or not callable(validate):
        raise SwapError('Preflight and validation are required')
    backup = active.with_name('.netbox-ops-backup')
    state = active.with_name('.netbox-ops-transaction.json')

    with ExclusiveLock(lock_path):
        write_audit('SELF-UPGRADE transaction requested', path=audit_path)
        # Recheck approval only after taking the exclusive lock.
        try:
            preflight(staged)
        except Exception as exc:
            write_audit(f'SELF-UPGRADE preflight failed type={type(exc).__name__}',
                        path=audit_path)
            raise
        try:
            begin_swap(active, staged, backup, state)
            write_audit('SELF-UPGRADE installation switched; validation pending',
                        path=audit_path)
            validate(active)
            write_audit('SELF-UPGRADE validation passed', path=audit_path)
            finish_swap(active, staged, backup, state)
        except Exception as exc:
            if state.is_file():
                try:
                    rollback_swap(active, staged, backup, state)
                    write_audit('SELF-UPGRADE rollback completed', path=audit_path)
                except Exception as recovery:
                    write_audit(
                        f'SELF-UPGRADE rollback failed type={type(recovery).__name__}',
                        path=audit_path,
                    )
                    raise SwapError('Self-upgrade failed; manual recovery may be needed') from recovery
            write_audit(f'SELF-UPGRADE failed type={type(exc).__name__}', path=audit_path)
            raise
        try:
            cleanup_swap(active, staged, backup, state)
        except Exception as exc:
            # Once validation is recorded, never roll back automatically.
            # The validated installation remains in place for safe retry.
            write_audit(f'SELF-UPGRADE cleanup pending type={type(exc).__name__}',
                        path=audit_path)
            raise
        write_audit('SELF-UPGRADE finished', path=audit_path)
