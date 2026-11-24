"""Offline-safe directory swap transaction for the external self-upgrade helper.

The caller must hold the exclusive operations lock, validate compatibility and
ownership, and ensure the running process has exited before using this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class SwapError(RuntimeError):
    """The installation swap cannot be performed safely."""


def _write_state(path: Path, phase: str) -> None:
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump({'format': 1, 'phase': phase}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SwapError('Cannot read swap transaction state') from exc
    if not isinstance(data, dict) or data.get('format') != 1 or data.get('phase') not in (
        'prepared', 'old_moved', 'new_active', 'validated'
    ):
        raise SwapError('Invalid swap transaction state')
    return data['phase']


def _validate_paths(active: Path, staged: Path, backup: Path, state: Path) -> None:
    paths = (active, staged, backup, state)
    if any(path.parent != active.parent for path in paths):
        raise SwapError('All transaction paths must share one parent')
    if len(set(paths)) != len(paths):
        raise SwapError('Transaction paths must be distinct')
    parent = active.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o022:
        raise SwapError('Unsafe installation parent')
    if any(path.is_symlink() for path in paths):
        raise SwapError('Symlinked transaction path')


def begin_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Rename a staged tree into place; retain the old tree for validation.

    This is intentionally not a public upgrade entry point. The caller must
    validate the new installation and then call finish_swap or rollback_swap.
    """
    _validate_paths(active, staged, backup, state)
    if state.exists() or backup.exists():
        raise SwapError('Existing transaction must be recovered first')
    if not active.is_dir() or not staged.is_dir():
        raise SwapError('Expected active and staged installation directories')
    _write_state(state, 'prepared')
    os.rename(active, backup)
    _write_state(state, 'old_moved')
    os.rename(staged, active)
    _write_state(state, 'new_active')


def rollback_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Restore the original tree after a failed or interrupted swap."""
    _validate_paths(active, staged, backup, state)
    phase = _read_state(state)
    if phase == 'validated':
        raise SwapError('Cannot roll back a validated installation')
    if backup.is_dir():
        if active.exists():
            if staged.exists():
                raise SwapError('Staging path already occupied; manual recovery needed')
            os.rename(active, staged)
        os.rename(backup, active)
    elif phase != 'prepared':
        raise SwapError('Backup missing; manual recovery needed')
    state.unlink()


def finish_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Mark validation complete. Cleanup is deliberately left to the caller."""
    _validate_paths(active, staged, backup, state)
    if _read_state(state) != 'new_active' or not active.is_dir() or not backup.is_dir():
        raise SwapError('Swap is not ready for validation completion')
    _write_state(state, 'validated')
