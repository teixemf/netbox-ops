"""Protected recovery data for a quiesced NetBox upgrade.

Call only after fresh plan approval, while holding the shared ExclusiveLock
and with NetBox writers stopped. The caller owns service and lock lifecycle.
No checkout, migration or automatic rollback is performed here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .backups import BackupManager
from .config import Configuration
from .services import SystemdServices
from .upgrade_execution import UpgradeExecutionError
from .upgrade_plan import UpgradePlan


_CONFIG_FILES = (
    'netbox/netbox/configuration.py',
    'netbox/netbox/ldap_config.py',
    'gunicorn.py',
    'local_requirements.txt',
)
_MAX_CONFIG = 16 * 1024 * 1024


@dataclass(frozen=True)
class UpgradeRecovery:
    directory: Path
    archive: Path
    manifest: Path


def _private_directory(path: Path) -> None:
    if not path.is_absolute() or path == Path('/') or path.resolve() != path:
        raise UpgradeExecutionError('Unsafe recovery directory path')
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise UpgradeExecutionError('Recovery directory must be root:root 0700')
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise UpgradeExecutionError('Unsafe recovery directory ancestor')


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchown(stream.fileno(), 0, 0)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_config(root: Path, relative: str) -> bytes | None:
    path = root / relative
    if path.resolve() != path:
        raise UpgradeExecutionError('Symlinked upgrade configuration is unsafe')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG:
            raise UpgradeExecutionError('Unsupported upgrade configuration file')
        data = stream.read(_MAX_CONFIG + 1)
        after = os.fstat(stream.fileno())
        if len(data) > _MAX_CONFIG or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise UpgradeExecutionError('Upgrade configuration changed during capture')
        return data


def _private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise UpgradeExecutionError('Recovery file must be root:root 0600')


def _capture_quiesced_recovery(
    cfg: Configuration, plan: UpgradePlan, *, systemd: SystemdServices,
) -> UpgradeRecovery:
    """Create and verify a separate recovery point under the caller's lock.

    A failed or incomplete recovery directory is retained for inspection.
    Only a returned UpgradeRecovery with a written manifest is usable.
    Ordinary backup inventory does not include these separate directories.
    """
    if os.geteuid() != 0:
        raise UpgradeExecutionError('Root privileges required for upgrade recovery')
    root = plan.source.installation.root
    if not root.is_absolute() or root == Path('/') or root.resolve() != root:
        raise UpgradeExecutionError('Unsafe NetBox configuration root')
    if not re.fullmatch(r'[0-9a-f]{40}', plan.source.installation.commit):
        raise UpgradeExecutionError('Invalid previous Git revision')
    states = systemd.status(['netbox', 'netbox-rq'])
    if ({s.name for s in states} != {'netbox', 'netbox-rq'}
            or any(s.loaded != 'loaded' or s.active != 'inactive' for s in states)):
        raise UpgradeExecutionError('NetBox writers must be stopped before upgrade recovery')

    _private_directory(cfg.backup_directory)
    directory = Path(tempfile.mkdtemp(prefix='pre-upgrade-', dir=cfg.backup_directory))
    os.chown(directory, 0, 0)
    os.chmod(directory, 0o700)
    _private_directory(directory)
    try:
        files = {}
        requirements_hash = None
        for relative in _CONFIG_FILES:
            data = _read_config(root, relative)
            if data is None:
                if relative == _CONFIG_FILES[0]:
                    raise UpgradeExecutionError('NetBox configuration.py is missing')
                continue
            name = Path(relative).name
            digest = hashlib.sha256(data).hexdigest()
            _write_private(directory / name, data)
            files[relative] = {'file': name, 'sha256': digest}
            if relative == 'local_requirements.txt':
                requirements_hash = digest
        if requirements_hash != plan.source.inventory.requirements_hash:
            raise UpgradeExecutionError('Plugin requirements changed after plan approval')

        # The existing restore path uses _backup under the same exclusive lock.
        # Do not call backup(), which would acquire another lock and restart writers.
        manager = BackupManager(replace(cfg, backup_directory=directory))
        report = manager._backup('combined')
        archive = report.path
        if (report.kind != 'combined' or archive.parent != directory
                or not archive.name.endswith('.combined.tar.gz')):
            raise UpgradeExecutionError('Unexpected upgrade backup result')
        _private_file(archive)
        _private_file(Path(str(archive) + '.json'))
        verified = manager.verify('combined', archive)
        if (not isinstance(verified, dict) or verified.get('kind') != 'combined'
                or verified.get('netbox_version') != plan.current_netbox
                or verified.get('database') != cfg.database):
            raise UpgradeExecutionError('Upgrade backup verification did not match the installation')

        manifest = directory / 'recovery.json'
        record = {
            'format': 1,
            'purpose': 'pre_upgrade',
            'git_commit': plan.source.installation.commit,
            'netbox_root': str(root),
            'current_netbox': plan.current_netbox,
            'target_netbox': plan.target_netbox,
            'plan_fingerprint': plan.fingerprint,
            'stages': [s.netbox_version for s in plan.stages],
            'plugin_versions': [[p.package, p.installed_version] for p in plan.plugins],
            'configuration': files,
            'archive': archive.name,
        }
        # Publish the completion marker only after the private record is complete.
        pending = directory / '.recovery.pending'
        _write_private(pending, json.dumps(record, indent=2, sort_keys=True).encode('utf-8'))
        os.replace(pending, manifest)
        return UpgradeRecovery(directory, archive, manifest)
    except Exception:
        # Never echo exception text: subprocess output or config may contain secrets.
        # No successful marker is returned, and all captured evidence is retained.
        raise UpgradeExecutionError(
            'Upgrade recovery failed; inspect the protected pre-upgrade directory'
        ) from None
