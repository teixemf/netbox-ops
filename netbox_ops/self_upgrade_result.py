"""Persist the outcome of the last self-upgrade step outside the swapped tree."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .self_upgrade_releases import Version
from .self_upgrade_transaction import SwapError, _sync_parent

RESULT_PATH = Path('/var/lib/netbox-ops/self-upgrade-result.json')
_PHASES = frozenset({'submitted', 'applying', 'completed', 'failed', 'rolled_back',
                     'recovery_required', 'cancelled'})


def _check_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.resolve() != path
            or info.st_uid != 0 or info.st_mode & 0o077):
        raise SwapError('Unsafe self-upgrade result directory')


def _validate(result: dict) -> dict:
    if (not isinstance(result, dict)
            or set(result) != {'format', 'phase', 'installed', 'step', 'target'}
            or type(result['format']) is not int or result['format'] != 1
            or not isinstance(result['phase'], str) or result['phase'] not in _PHASES):
        raise SwapError('Invalid self-upgrade result')
    try:
        old, step, target = (Version.parse(result[key]) for key in ('installed', 'step', 'target'))
    except (ValueError, TypeError) as exc:
        raise SwapError('Invalid self-upgrade result versions') from exc
    if not old < step <= target:
        raise SwapError('Invalid self-upgrade result path')
    return result


def result_for(record: dict, phase: str) -> dict:
    installed = Version(**record['installed']['version'])
    return _validate({'format': 1, 'phase': phase, 'installed': f'v{installed}',
                      'step': record['steps'][0], 'target': record['target']})


def write_result(record: dict, phase: str, *, path: Path = RESULT_PATH) -> None:
    """Atomically save a bounded result. Caller holds the operation lock."""
    if os.geteuid() != 0:
        raise SwapError('Self-upgrade result requires root')
    _check_directory(path.parent)
    data = result_for(record, phase)
    fd, name = tempfile.mkstemp(prefix='.self-upgrade-result-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_parent(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_result(*, path: Path = RESULT_PATH) -> dict | None:
    """Read only a trusted, bounded result; never infer success from a PID."""
    try:
        _check_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o077 or info.st_size > 4096):
                raise SwapError('Unsafe self-upgrade result file')
            data = stream.read(4097)
        if len(data) > 4096:
            raise SwapError('Oversized self-upgrade result')
        return _validate(json.loads(data))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SwapError('Cannot read self-upgrade result') from exc
