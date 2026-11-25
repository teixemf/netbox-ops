"""Recoverable same-filesystem directory swap for the self-upgrade helper.

This module does not select versions, execute installers or restart services.
The caller must validate the source and target before starting the transaction.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


class SwapError(RuntimeError):
    """A directory swap cannot be completed or recovered safely."""


_BACKUP_NAME = '.netbox-ops-backup'
_STATE_NAME = '.netbox-ops-transaction.json'
_STAGE_NAME = re.compile(r'\.netbox-ops-stage-[A-Za-z0-9_-]+')


def _sync_parent(parent: Path) -> None:
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise SwapError(f'Not a real installation directory: {path}')
    return info.st_dev, info.st_ino


def _check_parent(parent: Path) -> None:
    if (not parent.is_absolute() or '..' in parent.parts or not parent.is_dir()
            or parent.is_symlink() or parent.resolve() != parent):
        raise SwapError('Unsafe installation parent')
    info = parent.stat()
    if info.st_mode & 0o022 or (parent == Path('/opt') and info.st_uid != 0):
        raise SwapError('Installation parent is not root-controlled')


def _check_paths(active: Path, staged: Path, backup: Path, state: Path) -> None:
    paths = (active, staged, backup, state)
    if any(not path.is_absolute() or '..' in path.parts for path in paths):
        raise SwapError('Transaction paths must be absolute and non-traversing')
    if len(set(paths)) != len(paths) or any(p.parent != active.parent for p in paths):
        raise SwapError('Transaction paths must be distinct siblings')
    if (active.name != 'netbox-ops' or not _STAGE_NAME.fullmatch(staged.name)
            or backup.name != _BACKUP_NAME or state.name != _STATE_NAME):
        raise SwapError('Unknown installation layout')
    parent = active.parent
    _check_parent(parent)
    if state.is_symlink():
        raise SwapError('Symlinked transaction state')
    for path in (active, staged, backup):
        if _identity(path) is not None and parent == Path('/opt'):
            entry = path.lstat()
            if entry.st_uid != 0 or entry.st_mode & 0o022:
                raise SwapError('Installation directory is not root-controlled')


def _write_state(path: Path, data: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix='.netbox-ops-state-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_parent(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> dict:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or (path.parent == Path('/opt') and info.st_uid != 0)
                or info.st_size > 4096):
            raise SwapError('Unsafe transaction state file')
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SwapError('Cannot read swap transaction state') from exc
    if (not isinstance(data, dict) or set(data) != {'format', 'phase', 'old', 'new', 'staged'}
            or data['format'] != 3
            or not isinstance(data['staged'], str)
            or not _STAGE_NAME.fullmatch(data['staged'])
            or type(data['phase']) is not str
            or data['phase'] not in {'prepared', 'old_moved', 'new_active', 'validated'}
            or any(not isinstance(data[k], list) or len(data[k]) != 2
                   or any(type(n) is not int or n < 1 for n in data[k])
                   for k in ('old', 'new'))):
        raise SwapError('Invalid swap transaction state')
    return data


def _read_bound_state(state: Path, staged: Path) -> dict:
    data = _read_state(state)
    if data['staged'] != staged.name:
        raise SwapError('Staging directory does not match the transaction')
    return data


def begin_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Prepare the swap and preserve the original installation for rollback."""
    _check_paths(active, staged, backup, state)
    if state.exists() or backup.exists():
        raise SwapError('Existing transaction must be recovered first')
    old_id, new_id = _identity(active), _identity(staged)
    if old_id is None or new_id is None:
        raise SwapError('Active or staged installation is missing')
    if old_id[0] != new_id[0]:
        raise SwapError('The installations are not on the same filesystem')
    data = {'format': 3, 'phase': 'prepared', 'old': list(old_id),
            'new': list(new_id), 'staged': staged.name}
    _write_state(state, data)
    os.rename(active, backup)
    _sync_parent(active.parent)
    _write_state(state, {**data, 'phase': 'old_moved'})
    os.rename(staged, active)
    _sync_parent(active.parent)
    _write_state(state, {**data, 'phase': 'new_active'})


def rollback_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Recover the old installation, even after an interrupted prior rollback."""
    _check_paths(active, staged, backup, state)
    data = _read_bound_state(state, staged)
    if data['phase'] == 'validated':
        raise SwapError('Cannot roll back an approved installation')
    old_id, new_id = tuple(data['old']), tuple(data['new'])
    current, candidate, saved = _identity(active), _identity(staged), _identity(backup)
    if saved is not None and saved != old_id:
        raise SwapError('Backup identity changed; manual recovery required')
    if current not in (None, old_id, new_id) or candidate not in (None, new_id):
        raise SwapError('Installation identity changed; manual recovery required')
    if saved is not None:
        if current == old_id:
            raise SwapError('Both active and backup claim to be the old installation')
        if current == new_id:
            if candidate is not None:
                raise SwapError('Staging path occupied; manual recovery required')
            os.rename(active, staged)
            _sync_parent(active.parent)
        elif candidate is None:
            raise SwapError('New installation missing; manual recovery required')
        os.rename(backup, active)
        _sync_parent(active.parent)
    elif current != old_id or candidate != new_id:
        raise SwapError('Original installation missing; manual recovery required')
    state.unlink()
    _sync_parent(active.parent)


def finish_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Record that the new installation passed the caller's validation."""
    _check_paths(active, staged, backup, state)
    data = _read_bound_state(state, staged)
    if (data['phase'] != 'new_active'
            or _identity(active) != tuple(data['new'])
            or _identity(backup) != tuple(data['old'])
            or _identity(staged) is not None):
        raise SwapError('Swap is not ready for validation completion')
    _write_state(state, {**data, 'phase': 'validated'})


def cleanup_swap(active: Path, staged: Path, backup: Path, state: Path) -> None:
    """Remove the old tree only after validation. Safe to retry after a crash."""
    _check_paths(active, staged, backup, state)
    data = _read_bound_state(state, staged)
    if (data['phase'] != 'validated' or _identity(active) != tuple(data['new'])
            or _identity(staged) is not None):
        raise SwapError('New installation is not validated')
    saved = _identity(backup)
    if saved is not None:
        if saved != tuple(data['old']):
            raise SwapError('Backup identity changed; manual cleanup required')
        shutil.rmtree(backup)
        _sync_parent(active.parent)
    state.unlink()
    _sync_parent(active.parent)


def recover_swap(active: Path) -> str:
    """Recover a recorded transaction without trusting a caller's stage path.

    The caller must hold the exclusive operation lock. An unvalidated swap
    is rolled back; a validated swap only resumes backup cleanup. Unknown
    state is never guessed from directory names. No application code is run.
    """
    if active.name != 'netbox-ops':
        raise SwapError('Unknown installation layout')
    _check_parent(active.parent)
    backup = active.with_name(_BACKUP_NAME)
    state = active.with_name(_STATE_NAME)
    if not state.exists() and not state.is_symlink():
        if backup.exists() or backup.is_symlink():
            raise SwapError('Backup exists without transaction state; manual recovery required')
        return 'none'
    data = _read_state(state)
    staged = active.with_name(data['staged'])
    if data['phase'] == 'validated':
        cleanup_swap(active, staged, backup, state)
        return 'cleanup'
    rollback_swap(active, staged, backup, state)
    return 'rollback'
