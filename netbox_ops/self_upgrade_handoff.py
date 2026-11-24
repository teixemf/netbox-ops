"""External, recoverable self-upgrade handoff outside the active installation.

Only root may create or use a handoff. The saved copy of the old application
runs on the verified system interpreter, not either installation's venv.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .self_upgrade_catalogue import discover_compatibility
from .self_upgrade_helper import (
    open_initiator_pidfd, operation_lock, recover_interrupted_upgrade, run_approved_swap,
)
from .self_upgrade_installation import ACTIVE, inspect_installation, verify_candidate_layout
from .self_upgrade_prepare import PreparedRelease
from .self_upgrade_releases import Version
from .self_upgrade_result import write_result
from .self_upgrade_runtime import _trusted_python, validate_installation_runtime
from .self_upgrade_transaction import SwapError, _sync_parent
from .services import LOCK_PATH, LOG_PATH, ExclusiveLock, write_audit

_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
_STAGE = re.compile(r'\.netbox-ops-stage-[A-Za-z0-9_-]+')
_MAX_RECORD = 128 * 1024
_BOOTSTRAP = """import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from netbox_ops.self_upgrade_handoff import main
raise SystemExit(main())
"""


def _root() -> None:
    if os.geteuid() != 0:
        raise SwapError('Self-upgrade handoff requires root privileges')


def _handoff(active: Path) -> Path:
    return active.with_name('.netbox-ops-handoff')


def _pending(active: Path) -> bool:
    return any(path.exists() or path.is_symlink() for path in (
        active.with_name('.netbox-ops-transaction.json'),
        active.with_name('.netbox-ops-backup'),
    ))


def _identity(path: Path) -> tuple[int, int]:
    entry = path.lstat()
    if not stat.S_ISDIR(entry.st_mode):
        raise SwapError('Expected a real installation directory')
    return entry.st_dev, entry.st_ino


def _stage_path(active: Path, name: str) -> Path:
    if not isinstance(name, str) or not _STAGE.fullmatch(name):
        raise SwapError('Invalid handoff staging name')
    return active.with_name(name)


def _jsonable(value):
    """Convert dataclass fields to a deterministic JSON representation."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _snapshot(value) -> dict:
    return _jsonable(asdict(value))


def _digest_tree(root: Path, *, sync: bool = False) -> str:
    """Bind the candidate to the reviewed bytes and persist them before rename."""
    digest = hashlib.sha256()
    for path in (root, *sorted(root.rglob('*'))):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        fields = [str(path.relative_to(root)), mode, info.st_uid, info.st_gid]
        if stat.S_ISLNK(info.st_mode):
            fields.extend(('link', os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            fields.append('directory')
        elif stat.S_ISREG(info.st_mode):
            file_digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    file_digest.update(chunk)
                if sync:
                    os.fsync(stream.fileno())
            fields.extend(('file', file_digest.hexdigest()))
        else:
            raise SwapError('Unexpected handoff file type')
        digest.update(json.dumps(fields, ensure_ascii=True).encode() + b'\n')
    if sync:
        for path in sorted(root.rglob('*'), reverse=True):
            if path.is_dir() and not path.is_symlink():
                _sync_parent(path)
        _sync_parent(root)
    return digest.hexdigest()


def _check_bundle(bundle: Path) -> None:
    info = bundle.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or bundle.resolve() != bundle):
        raise SwapError('Unsafe self-upgrade handoff directory')
    for path in bundle.rglob('*'):
        info = path.lstat()
        if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or info.st_uid != os.geteuid() or info.st_mode & 0o6022):
            raise SwapError('Unsafe self-upgrade handoff entry')


def _read_record(bundle: Path) -> dict:
    _check_bundle(bundle)
    path = bundle / 'request.json'
    try:
        if path.stat().st_size > _MAX_RECORD:
            raise SwapError('Oversized self-upgrade handoff record')
        record = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(record, dict)
                or set(record) != {'format', 'stage', 'identity', 'digest',
                                   'installed', 'target', 'steps', 'release', 'mode'}
                or type(record['format']) is not int or record['format'] != 1
                or not isinstance(record['steps'], list)
                or not 1 <= len(record['steps']) <= 64
                or not isinstance(record['installed'], dict)
                or not isinstance(record['release'], dict)
                or not isinstance(record['digest'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', record['digest'])
                or not isinstance(record['identity'], list)
                or len(record['identity']) != 2
                or any(type(n) is not int or n <= 0 for n in record['identity'])
                or type(record['mode']) is not int
                or not 0 <= record['mode'] <= 0o777
                or record['mode'] & 0o022):
            raise SwapError('Invalid self-upgrade handoff record')
        _stage_path(bundle.with_name('netbox-ops'), record['stage'])
        target = Version.parse(record['target'])
        steps = tuple(Version.parse(step) for step in record['steps'])
        if steps[-1] != target or tuple(sorted(set(steps))) != steps:
            raise SwapError('Invalid handoff upgrade path')
        return record
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise SwapError('Cannot read self-upgrade handoff record') from exc


def _build_bundle(prepared: PreparedRelease, active: Path, interpreter: Path) -> Path:
    """Publish a durable recovery copy before the helper can touch the tree."""
    bundle = _handoff(active)
    if bundle.exists() or bundle.is_symlink() or _pending(active):
        raise SwapError('Existing self-upgrade requires recovery first')
    stage = _stage_path(active, prepared.path.name)
    if stage != prepared.path or _identity(stage) != prepared.identity:
        raise SwapError('Prepared installation identity changed')
    if inspect_installation() != prepared.installed:
        raise SwapError('Installed release changed since preparation')
    if (not prepared.steps or prepared.steps[0] != prepared.release.version
            or prepared.steps[-1] != prepared.target
            or tuple(sorted(set(prepared.steps))) != prepared.steps
            or prepared.steps[0] <= prepared.installed.version):
        raise SwapError('Invalid prepared upgrade path')
    verify_candidate_layout(stage, prepared.release.version)
    mode = stat.S_IMODE(active.stat().st_mode)
    if mode & ~0o777 or mode & 0o022:
        raise SwapError('Unsupported active installation permissions')
    record = {
        'format': 1, 'stage': stage.name, 'identity': list(prepared.identity),
        'digest': _digest_tree(stage, sync=True),
        'installed': _snapshot(prepared.installed),
        'target': f'v{prepared.target}',
        'steps': [f'v{step}' for step in prepared.steps],
        'release': _snapshot(prepared.release),
        'mode': mode,
    }
    data = json.dumps(record, sort_keys=True).encode()
    if len(data) > _MAX_RECORD:
        raise SwapError('Self-upgrade handoff record is too large')
    temporary = Path(tempfile.mkdtemp(prefix='.netbox-ops-handoff-', dir=active.parent))
    try:
        shutil.copytree(active / 'netbox_ops', temporary / 'netbox_ops',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (temporary / 'runner.py').write_text(_BOOTSTRAP, encoding='utf-8')
        (temporary / 'request.json').write_bytes(data)
        (temporary / 'request.json').chmod(0o600)
        # The shell entry point is independent of the active launcher and venv.
        command = (f'exec {shlex.quote(str(interpreter))} -I -B '
                   f'{shlex.quote(str(bundle / "runner.py"))} recover\n')
        (temporary / 'recover').write_text('#!/bin/sh\n' + command, encoding='utf-8')
        (temporary / 'recover').chmod(0o700)
        _check_bundle(temporary)
        _digest_tree(temporary, sync=True)
        temporary.rename(bundle)
        _sync_parent(active.parent)
        return bundle
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def launch_prepared_release(prepared: PreparedRelease) -> int:
    """Start the independent helper; the caller must immediately exit.

    The inherited pidfd prevents a swap until this exact process exits.
    The same exclusive flock is held throughout handoff and child cleanup.
    The return value is the helper PID, not an installation success result.
    """
    _root()
    if not isinstance(prepared, PreparedRelease):
        raise SwapError('A verified prepared release is required')
    with ExclusiveLock(LOCK_PATH) as lock:
        interpreter = _trusted_python(prepared.installed.base_python, prepared.path)
        if lock.fd is None:
            raise SwapError('Operation lock was not acquired')
        descriptor = open_initiator_pidfd()
        bundle = None
        launched = False
        try:
            bundle = _build_bundle(prepared, ACTIVE, interpreter)
            write_result(_read_record(bundle), 'submitted')
            write_audit(f'SELF-UPGRADE handoff requested installed=v{prepared.installed.version} '
                        f'next=v{prepared.release.version} target=v{prepared.target}', path=LOG_PATH)
            child = subprocess.Popen(
                [str(interpreter), '-I', '-B', str(bundle / 'runner.py'),
                 'apply', str(descriptor), str(lock.fd)],
                cwd=bundle, env=_ENV, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(descriptor, lock.fd), start_new_session=True,
            )
            launched = True
            return child.pid
        except Exception:
            if bundle is not None:
                write_result(_read_record(bundle), 'failed')
            raise
        finally:
            os.close(descriptor)
            if not launched and bundle is not None:
                shutil.rmtree(bundle)
                _sync_parent(ACTIVE.parent)


def _preflight(active: Path, staged: Path, record: dict) -> None:
    local = inspect_installation()
    if (_snapshot(local) != record['installed']
            or stat.S_IMODE(active.stat().st_mode) != record['mode']):
        raise SwapError('Installed release changed after handoff')
    if _identity(staged) != tuple(record['identity']):
        raise SwapError('Prepared installation identity changed')
    target = Version.parse(record['target'])
    steps = tuple(Version.parse(step) for step in record['steps'])
    if steps[0] <= local.version:
        raise SwapError('Downgrade or same-version installation refused')
    catalogue = discover_compatibility(installed=local.version, environment=local.environment)
    decision = catalogue.decision_for(target)
    selected = [release for release in catalogue.releases if release.version == steps[0]]
    if (decision.status != 'compatible' or decision.steps != steps
            or len(selected) != 1 or _snapshot(selected[0]) != record['release']):
        raise SwapError('Official release or compatibility changed after handoff')
    verify_candidate_layout(staged, steps[0])
    if _digest_tree(staged) != record['digest']:
        raise SwapError('Prepared installation content changed')
    validate_installation_runtime(staged, steps[0])
    if _digest_tree(staged) != record['digest']:
        raise SwapError('Prepared installation changed during validation')


def _validate(active: Path, record: dict) -> None:
    expected = Version.parse(record['steps'][0])
    if _identity(active) != tuple(record['identity']):
        raise SwapError('New active installation identity changed')
    validate_installation_runtime(active, expected)
    local = inspect_installation()
    if (local.version != expected
            or _snapshot(local)['environment'] != record['installed']['environment']
            or str(local.base_python) != record['installed']['base_python']):
        raise SwapError('New active installation did not pass verification')
    active.chmod(record['mode'])
    _sync_parent(active)


def _clear_handoff(active: Path, bundle: Path, record: dict) -> None:
    """Remove only our identified unused candidate and independent copy."""
    if _pending(active):
        raise SwapError('Transaction requires recovery; handoff retained')
    stage = _stage_path(active, record['stage'])
    if stage.exists() or stage.is_symlink():
        if _identity(stage) != tuple(record['identity']):
            raise SwapError('Staging identity changed; handoff retained')
        stage.chmod(0o700)
        shutil.rmtree(stage)
        _sync_parent(active.parent)
    _check_bundle(bundle)
    shutil.rmtree(bundle)
    _sync_parent(active.parent)


def _apply(bundle: Path, initiator_fd: int, lock_fd: int) -> int:
    record = _read_record(bundle)
    staged = _stage_path(ACTIVE, record['stage'])
    # Retain the inherited descriptor even when the transaction raises.
    with operation_lock(LOCK_PATH, lock_fd):
        try:
            write_result(record, 'applying')
            run_approved_swap(
                staged, initiator_fd=initiator_fd, lock_fd=lock_fd,
                active=ACTIVE, lock_path=LOCK_PATH, audit_path=LOG_PATH,
                preflight=lambda active, candidate: _preflight(active, candidate, record),
                validate=lambda active: _validate(active, record),
            )
        except Exception:
            if _pending(ACTIVE):
                write_result(record, 'recovery_required')
            else:
                old_id = tuple(record['installed']['identity'])
                phase = 'rolled_back' if _identity(ACTIVE) == old_id else 'recovery_required'
                write_result(record, phase)
                if phase == 'rolled_back':
                    _clear_handoff(ACTIVE, bundle, record)
            raise
        write_result(record, 'completed')
        _clear_handoff(ACTIVE, bundle, record)
    return 0


def _recover(bundle: Path) -> str:
    with ExclusiveLock(LOCK_PATH) as lock:
        record = _read_record(bundle)
        if lock.fd is None:
            raise SwapError('Operation lock was not acquired')
        result = recover_interrupted_upgrade(active=ACTIVE, lock_path=LOCK_PATH,
                                             audit_path=LOG_PATH, lock_fd=lock.fd)
        current = _identity(ACTIVE)
        if current == tuple(record['identity']):
            phase = 'completed'
        elif current == tuple(record['installed']['identity']):
            phase = 'rolled_back' if result == 'rollback' else 'cancelled'
        else:
            raise SwapError('Unknown active installation after recovery')
        write_result(record, phase)
        _clear_handoff(ACTIVE, bundle, record)
        return result


def main() -> int:
    """Private entry point of the durable helper copy, not the public CLI."""
    _root()
    bundle = Path(__file__).resolve().parents[1]
    if bundle != _handoff(ACTIVE):
        raise SwapError('Helper must run from the independent handoff directory')
    args = sys.argv[1:]
    try:
        if len(args) == 3 and args[0] == 'apply':
            initiator_fd, lock_fd = map(int, args[1:])
            try:
                result = _apply(bundle, initiator_fd, lock_fd)
                print('Self-upgrade step completed. See operations.log for details.')
                return result
            finally:
                os.close(initiator_fd)
                os.close(lock_fd)
        if args == ['recover']:
            print(f'Self-upgrade recovery: {_recover(bundle)}')
            return 0
        raise SwapError('Invalid self-upgrade helper arguments')
    except Exception as exc:
        # Avoid printing remote metadata or untrusted exception contents.
        print(f'Self-upgrade helper failed ({type(exc).__name__}). '
              'See /var/log/netbox-ops/operations.log. '
              'If the handoff remains, run sudo /opt/.netbox-ops-handoff/recover.',
              file=sys.stderr)
        return 1
