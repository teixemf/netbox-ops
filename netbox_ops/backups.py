"""Root-only local PostgreSQL and artifact backups with preflighted restores.

No shell=True, no YAML, no NetBox venv dependency for database recovery.
Only PostgreSQL custom dumps and our own manifest-based archives are supported.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .config import Configuration
from .netbox_version import read_netbox_version
from .platform import pg_binary, PlatformError
from .services import (ExclusiveLock, ServiceError, SystemdServices,
                       require_service_action_allowed, upgrade_recovery_pending, write_audit)
from .restore_environment import RuntimeErrorSafe, check_staged_migrations

FORMAT = 1
KINDS = ('db', 'media', 'combined')
RESTORE_KINDS = (*KINDS, 'full')
COMBINED_KINDS = ('combined', 'full')
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBERS = 200_000
MAX_DATA_BYTES = 200 * 1024**3  # Review for unusually large media stores.
DB_ID = re.compile(r'^[a-z][a-z0-9_]{0,39}$')


class BackupError(Exception):
    pass


@dataclass
class BackupReport:
    path: Path
    kind: str
    detail: str = ''


@dataclass(frozen=True)
class BackupEntry:
    path: Path
    kind: str
    created: str
    size: int
    version: str
    legacy: bool = False
    purpose: str = 'manual'  # manual | pre_restore


@dataclass(frozen=True)
class BackupSummary:
    """Filesystem observation, not evidence of archive validity."""
    path: Path
    kind: str
    timestamp: dt.datetime
    source: str  # filename | mtime
    purpose: str  # manual | pre_restore | unknown


def _backup_filename_time(path: Path) -> dt.datetime | None:
    match = re.search(r'(?<!\d)(\d{8}T\d{6}Z)(?!\d)', path.name)
    try:
        return (dt.datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ')
                .replace(tzinfo=dt.timezone.utc) if match else None)
    except (OverflowError, ValueError):
        return None


ProgressCallback = Callable[[str, int, int, str], None]


def _progress(callback: ProgressCallback | None, title: str, done: int, total: int, detail: str = '') -> None:
    if callback is not None:
        callback(title, done, total, detail)


def _run(argv: list[str], *, timeout: int = 600, stdin=None, stdout=None) -> str:
    try:
        cp = subprocess.run(argv, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f'{argv[0]}: {exc}') from exc
    if cp.returncode:
        raise BackupError(f'{argv[0]} (exit {cp.returncode}): '
                          f'{cp.stderr.decode("utf-8", "replace")[:1600]}')
    return cp.stdout.decode('utf-8', 'replace').strip() if stdout is None else ''


def _pg(*args: str, timeout: int = 600, stdin=None, stdout=None,
        cfg: Configuration | None = None) -> str:
    try:
        command = pg_binary(args[0], cfg.pg_bin_directory if cfg else None)
    except PlatformError as exc:
        raise BackupError(str(exc)) from exc
    options = ['--port', str(cfg.postgres_port)] if cfg and cfg.postgres_port else []
    return _run(['runuser', '-u', 'postgres', '--', command, *options, *args[1:]],
                timeout=timeout, stdin=stdin, stdout=stdout)


def _sql(sql: str, database: str = 'postgres', cfg: Configuration | None = None) -> str:
    return _pg('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-A', '-t', '-d', database,
               '-c', sql, timeout=90, cfg=cfg)


def _safe_directory(path: Path) -> Path:
    """Require root-controlled directory; refuse symlink and world/group access."""
    if not path.is_absolute() or str(path) == '/' or path.is_symlink():
        raise BackupError(f'Unsafe backup directory: {path}')
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise BackupError(f'Backup directory must be root:root 0700: {path}')
    else:
        path.mkdir(mode=0o700, parents=True)
        os.chmod(path, 0o700)
    return path


def _new_file(parent: Path, name: str) -> Path:
    path = parent / name
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _version() -> str:
    """NetBox release from release.yaml (manage.py --version is Django!)."""
    return read_netbox_version()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _base_manifest(kind: str, cfg: Configuration, purpose: str = 'manual') -> dict:
    return {'format': FORMAT, 'tool': 'netbox-ops', 'kind': kind,
            'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'purpose': purpose,
            'netbox_version': _version(), 'netbox_version_source': 'release.yaml',
            'database': cfg.database,
            'artifacts': [], 'files': {}}


def _json_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


def _db_dump(cfg: Configuration, target: Path) -> None:
    with target.open('wb') as output:
        _pg('pg_dump', '--format=custom', '--no-owner', '--no-acl', '--dbname',
            cfg.database, stdout=output, timeout=3600, cfg=cfg)
    if target.stat().st_size < 16:
        raise BackupError('Database dump is empty')
    _check_dump(target, cfg=cfg)


def _check_dump(source: Path, cfg: Configuration | None = None) -> None:
    with source.open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise BackupError('Not a PostgreSQL custom-format dump (PGDMP)')
    try:
        executable = pg_binary('pg_restore', cfg.pg_bin_directory if cfg else None)
    except PlatformError as exc:
        raise BackupError(str(exc)) from exc
    _run([executable, '--list', str(source)], timeout=180)


def _artifacts(cfg: Configuration) -> list[tuple[str, Path, str]]:
    result = []
    for artifact in cfg.artifacts:
        name, path, required = artifact
        if name != 'media':
            continue
        if path.is_symlink():
            raise BackupError(f'Symlink artifact not allowed: {path}')
        if not path.exists():
            if required:
                raise BackupError(f'Required artifact not found: {path}')
            continue
        if not (path.is_file() or path.is_dir()):
            raise BackupError(f'Unsupported artifact: {path}')
        result.append((name, path, 'dir' if path.is_dir() else 'file'))
    # Guardrail: never include configuration, LDAP credentials or other legacy artifacts.
    if any(item[0] != 'media' for item in result):
        raise BackupError('Only media is permitted in version 0.3.0')
    if len(result) != 1 or result[0][2] != 'dir':
        raise BackupError('Media directory must exist and be a directory')
    return result


def _walk_regular(root: Path):
    """Do not follow symlinks, devices, sockets or external directory trees."""
    if root.is_symlink():
        raise BackupError(f'Symlinks are excluded from backups: {root}')
    if root.is_file():
        if not stat.S_ISREG(root.lstat().st_mode):
            raise BackupError(f'Not a regular file: {root}')
        yield root, PurePosixPath('.')
        return
    if not root.is_dir():
        raise BackupError(f'Not a regular file or directory: {root}')
    yield root, PurePosixPath('.')
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        for name in list(directories) + files:
            child = Path(current) / name
            if child.is_symlink():
                raise BackupError(f'Symlink inside artifact is not supported: {child}')
            mode = child.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise BackupError(f'Special file inside artifact: {child}')
            yield child, PurePosixPath(child.relative_to(root).as_posix())


def _create_archive(cfg: Configuration, path: Path, kind: str,
                    db_path: Path | None = None, purpose: str = 'manual') -> dict:
    manifest = _base_manifest(kind, cfg, purpose)
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT, compresslevel=6) as archive:
        for name, source, kind0 in _artifacts(cfg):
            manifest['artifacts'].append({'id': name, 'kind': kind0})
            for child, rel in _walk_regular(source):
                arc = 'payload/' + name + ('' if rel == PurePosixPath('.') else '/' + rel.as_posix())
                info = archive.gettarinfo(str(child), arcname=arc)
                info.pax_headers = {}  # Never include host path in extended metadata.
                if info.isfile():
                    manifest['files'][arc] = {'size': info.size, 'sha256': _sha256(child)}
                    with child.open('rb') as data:
                        archive.addfile(info, data)
                else:
                    archive.addfile(info)
        if db_path is not None:
            info = archive.gettarinfo(str(db_path), arcname='database/netbox.dump')
            manifest['files']['database/netbox.dump'] = {
                'size': info.size, 'sha256': _sha256(db_path)}
            with db_path.open('rb') as data:
                archive.addfile(info, data)
        payload = _json_bytes(manifest)
        info = tarfile.TarInfo('manifest.json')
        info.size, info.mode = len(payload), 0o600
        archive.addfile(info, io.BytesIO(payload))
    return manifest


def _read_archive(path: Path, expected: str) -> tuple[dict, tarfile.TarFile]:
    try:
        archive = tarfile.open(path, 'r:gz')
        info = archive.getmember('manifest.json')
        if not info.isfile() or info.size > MAX_MANIFEST_BYTES:
            raise BackupError('Invalid manifest size or type')
        reader = archive.extractfile(info)
        if reader is None:
            raise BackupError('Missing manifest data')
        manifest = json.loads(reader.read(MAX_MANIFEST_BYTES + 1))
        if (not isinstance(manifest, dict) or manifest.get('tool') != 'netbox-ops'
                or manifest.get('format') != FORMAT or manifest.get('kind') != expected):
            raise BackupError(f'Archive is not a compatible netbox-ops {expected} backup')
        if not isinstance(manifest.get('files'), dict) or not isinstance(manifest.get('artifacts'), list):
            raise BackupError('Invalid manifest contents')
        return manifest, archive
    except (OSError, tarfile.TarError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f'Invalid backup archive: {exc}') from exc


def _read_sidecar_metadata(path: Path) -> dict | None:
    """Read metadata without hashing multi-GiB DB dump in every TUI redraw."""
    source = Path(str(path) + '.json')
    if not source.is_file() or source.is_symlink():
        return None
    if source.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError('Oversized DB sidecar')
    payload = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('kind') != 'db':
        raise BackupError('Malformed DB sidecar')
    return payload


def _read_archive_metadata(path: Path, kind: str) -> dict:
    """Lightweight metadata lookup; integrity status is NOT inferred here."""
    expected = 'files' if kind == 'legacy' else kind
    manifest, handle = _read_archive(path, expected)
    handle.close()
    return manifest


def _validate_archive(path: Path, kind: str, cfg: Configuration, extract_dir: Path | None = None) -> dict:
    """Verify archive content and hashes, without using tar.extractall()."""
    manifest, archive = _read_archive(path, kind)
    with archive:
        expected: dict = manifest['files']
        configured = {name: (p, mandatory) for name, p, mandatory in cfg.artifacts if name == 'media'}
        if any(not isinstance(spec, dict) or spec.get('id') not in configured
               or spec.get('kind') not in ('file', 'dir')
               for spec in manifest['artifacts']):
            raise BackupError('Archive contains an unknown artifact')
        declared = {spec['id']: spec for spec in manifest['artifacts']}
        if len(declared) != len(manifest['artifacts']):
            raise BackupError('Duplicate artifact in manifest')
        if set(declared) != {'media'}:
            raise BackupError('Only media-only archives are accepted. Legacy config backups cannot be restored.')
        total_bytes = 0
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise BackupError('Too many archive members')
        paths = set()
        roots_seen = set()
        for member in members:
            name = member.name
            bits = PurePosixPath(name).parts
            if (not bits or name in paths or name.startswith('/') or '\\' in name
                    or any(bit in ('', '.', '..') for bit in bits)):
                raise BackupError(f'Unsafe or duplicate archive path: {name}')
            paths.add(name)
            if name == 'manifest.json':
                continue
            if not (member.isfile() or member.isdir()):
                raise BackupError(f'Special archive member is not allowed: {name}')
            if bits[0] == 'database':
                if kind not in COMBINED_KINDS or name != 'database/netbox.dump' or not member.isfile():
                    raise BackupError(f'Unexpected database entry: {name}')
            elif bits[0] == 'payload' and len(bits) >= 2 and bits[1] in declared:
                aid = bits[1]
                spec = declared[aid]
                if spec['kind'] == 'file' and (len(bits) != 2 or not member.isfile()):
                    raise BackupError(f'Unexpected member for file artifact: {name}')
                if spec['kind'] == 'dir' and len(bits) == 2 and not member.isdir():
                    raise BackupError(f'Invalid artifact directory: {name}')
                if len(bits) == 2:
                    roots_seen.add(aid)
            else:
                raise BackupError(f'Unexpected archive entry: {name}')
            if member.isfile():
                record = expected.get(name)
                if (not isinstance(record, dict) or record.get('size') != member.size
                        or not isinstance(record.get('sha256'), str)):
                    raise BackupError(f'Missing or invalid checksum: {name}')
                total_bytes += member.size
                if total_bytes > MAX_DATA_BYTES:
                    raise BackupError('Archive exceeds maximum uncompressed size')
                f = archive.extractfile(member)
                if f is None:
                    raise BackupError(f'Cannot read archive member: {name}')
                digest = hashlib.sha256()
                target = None
                if extract_dir is not None:
                    target = extract_dir.joinpath(*bits)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target = target.open('wb')
                with contextlib.ExitStack() as stack:
                    if target is not None:
                        stack.enter_context(target)
                    while chunk := f.read(1024 * 1024):
                        digest.update(chunk)
                        if target is not None:
                            target.write(chunk)
                if digest.hexdigest() != record['sha256']:
                    raise BackupError(f'Checksum mismatch: {name}')
            elif extract_dir is not None:
                extract_dir.joinpath(*bits).mkdir(parents=True, exist_ok=True)
        if set(expected) != {m.name for m in members if m.isfile() and m.name != 'manifest.json'}:
            raise BackupError('Manifest does not match archive contents')
        if roots_seen != set(declared):
            raise BackupError('Archive has missing artifact root entries')
        if kind in COMBINED_KINDS and 'database/netbox.dump' not in expected:
            raise BackupError('Missing database dump in combined backup')
        if kind == 'media' and any(k.startswith('database/') for k in expected):
            raise BackupError('Media backup unexpectedly contains a database')
        if set(declared) != {'media'}:
            raise BackupError('Only media-only archives are accepted. Legacy config backups cannot be restored.')
        if extract_dir is not None:
            # Restore mode and ownership from archive, only for extracted temporary files.
            for member in reversed(members):
                if member.name == 'manifest.json':
                    continue
                target = extract_dir.joinpath(*PurePosixPath(member.name).parts)
                os.chmod(target, member.mode & 0o777 or 0o600, follow_symlinks=False)
                # Never accept foreign archive uid/gid; copy keeps local identity on replacement.
    return manifest


def _verify_legacy_archive(path: Path) -> dict:
    """Read-only integrity check for old configuration-containing files archives.

    This deliberately NEVER extracts or restores legacy configuration files.
    Integrity means archive structure and declared file hashes match; it does
    not mean the backup is compatible with the current NetBox installation.
    """
    manifest, archive = _read_archive(path, 'files')
    with archive:
        declared = manifest.get('artifacts', [])
        if (not declared or not all(isinstance(a, dict) and
                                    isinstance(a.get('id'), str) and
                                    re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,63}', a['id']) and
                                    a.get('kind') in ('file', 'dir') for a in declared)):
            raise BackupError('Invalid legacy artifact manifest')
        ids = {a['id'] for a in declared}
        if len(ids) != len(declared):
            raise BackupError('Duplicate legacy artifacts')
        expected = manifest['files']
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise BackupError('Too many archive members')
        paths: set[str] = set()
        root_seen: set[str] = set()
        total_bytes = 0
        for member in members:
            name = member.name
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '\\' in name or name in paths or
                    any(part in ('', '.', '..') for part in parts)):
                raise BackupError(f'Unsafe or duplicate archive path: {name}')
            paths.add(name)
            if name == 'manifest.json':
                continue
            if (not (member.isfile() or member.isdir()) or len(parts) < 2 or
                    parts[0] != 'payload' or parts[1] not in ids):
                raise BackupError(f'Unexpected legacy archive entry: {name}')
            if len(parts) == 2:
                root_seen.add(parts[1])
            if not member.isfile():
                continue
            record = expected.get(name)
            if (not isinstance(record, dict) or record.get('size') != member.size or
                    not isinstance(record.get('sha256'), str)):
                raise BackupError(f'Missing legacy file checksum: {name}')
            total_bytes += member.size
            if total_bytes > MAX_DATA_BYTES:
                raise BackupError('Legacy archive exceeds size limit')
            stream = archive.extractfile(member)
            if stream is None:
                raise BackupError(f'Unreadable legacy file: {name}')
            digest = hashlib.sha256()
            with stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != record['sha256']:
                raise BackupError(f'Legacy checksum mismatch: {name}')
        if (set(expected) != {m.name for m in members if m.isfile() and m.name != 'manifest.json'}
                or root_seen != ids):
            raise BackupError('Legacy manifest does not match the archive')
    return manifest


def _require_root() -> None:
    if os.geteuid() != 0:
        raise BackupError('Root privileges required: sudo netbox-ops ...')


def _version_guard(manifest: dict | None, *, accept_unknown: bool,
                   allow_mismatch: bool) -> None:
    existing = _version()
    # Older netbox-ops used `manage.py --version` (Django, not NetBox).
    # Treat such metadata as unknown instead of trusting that wrong label.
    source = (manifest or {}).get('netbox_version_source')
    backup_version = ((manifest or {}).get('netbox_version') or '') if source == 'release.yaml' else ''
    if not existing or not backup_version:
        if not accept_unknown:
            raise BackupError('NetBox version unknown. Specify --accept-unknown-version after verification.')
    elif existing != backup_version and not allow_mismatch:
        raise BackupError(f'NetBox version mismatch: installed={existing}, backup={backup_version}. '
                          'Use --allow-version-mismatch only after verifying compatibility.')


def _preflight_db(cfg: Configuration, source: Path) -> None:
    if not DB_ID.fullmatch(cfg.database) or not DB_ID.fullmatch(cfg.database_owner):
        raise BackupError('Unsafe database or database role identifier')
    _check_dump(source, cfg=cfg)
    exists = _sql(f"SELECT 1 FROM pg_database WHERE datname='{cfg.database}'", cfg=cfg)
    if exists != '1':
        raise BackupError(f'Current database does not exist: {cfg.database}')
    role = _sql(f"SELECT 1 FROM pg_roles WHERE rolname='{cfg.database_owner}'", cfg=cfg)
    if role != '1':
        raise BackupError(f'Database role does not exist: {cfg.database_owner}')


def _sidecar_manifest(path: Path) -> dict | None:
    sidecar = Path(str(path) + '.json')
    if not sidecar.is_file():
        return None
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('kind') != 'db' or data.get('tool') != 'netbox-ops'
            or data.get('format') != FORMAT or data.get('sha256') != _sha256(path)):
        raise BackupError('Database backup metadata or SHA256 mismatch')
    return data


def _stage_database_options(cfg: Configuration) -> list[str]:
    """Read the live database's encoding and locale, not template0 defaults."""
    if not DB_ID.fullmatch(cfg.database):
        raise BackupError('Unsafe database identifier')
    # to_jsonb avoids referencing columns absent from older PostgreSQL versions.
    details = _sql(
        "SELECT json_build_object("
        "'encoding', pg_encoding_to_char(d.encoding), "
        "'collate', d.datcollate, 'ctype', d.datctype, "
        "'provider', to_jsonb(d)->>'datlocprovider', "
        "'icu_locale', COALESCE(to_jsonb(d)->>'daticulocale', "
        "to_jsonb(d)->>'datlocale')) "
        f"FROM pg_database AS d WHERE d.datname = '{cfg.database}'",
        cfg=cfg,
    )
    try:
        settings = json.loads(details)
    except (ValueError, TypeError) as exc:
        raise BackupError('Cannot read active database locale settings') from exc
    if not isinstance(settings, dict):
        raise BackupError('Cannot read active database locale settings')
    encoding = settings.get('encoding')
    collate = settings.get('collate')
    ctype = settings.get('ctype')
    provider = settings.get('provider') or 'c'  # PostgreSQL before version 15.
    if (not isinstance(encoding, str) or not re.fullmatch(r'[A-Z0-9_]+', encoding)
            or not all(isinstance(v, str) and v and chr(0) not in v
                       for v in (collate, ctype))):
        raise BackupError('Invalid active database encoding or locale')
    args = ['--encoding', encoding, '--lc-collate', collate, '--lc-ctype', ctype]
    if provider == 'i':
        icu_locale = settings.get('icu_locale')
        if not isinstance(icu_locale, str) or not icu_locale or chr(0) in icu_locale:
            raise BackupError('Invalid active database ICU locale')
        args.extend(['--locale-provider', 'icu', '--icu-locale', icu_locale])
    elif provider == 'b':
        builtin_locale = settings.get('icu_locale')
        if not isinstance(builtin_locale, str) or not builtin_locale or chr(0) in builtin_locale:
            raise BackupError('Invalid active database builtin locale')
        args.extend(['--locale-provider', 'builtin', '--builtin-locale', builtin_locale])
    elif provider != 'c':
        raise BackupError('Unsupported active database locale provider')
    return args


def _stage_db(cfg: Configuration, dump: Path) -> str:
    options = _stage_database_options(cfg)
    stage = 'nbo_stage_' + uuid.uuid4().hex[:16]
    _pg('createdb', '--owner', cfg.database_owner, '--template', 'template0',
        *options, stage, timeout=120, cfg=cfg)
    try:
        with dump.open('rb') as src:
            _pg('pg_restore', '--no-owner', '--no-acl', '--role', cfg.database_owner,
                '--exit-on-error', '--single-transaction', '--dbname', stage,
                stdin=src, timeout=7200, cfg=cfg)
        if _sql("SELECT to_regclass('public.django_migrations') IS NOT NULL", stage, cfg=cfg) != 't':
            raise BackupError('Restored database has no django_migrations table')
    except Exception:
        _drop_db(stage, cfg=cfg)
        raise
    return stage


def _drop_db(name: str, cfg: Configuration | None = None) -> None:
    if not DB_ID.fullmatch(name):
        raise BackupError('Unsafe database identifier')
    _pg('dropdb', '--if-exists', '--force', name, timeout=90, cfg=cfg)


def _rename_db(current: str, new: str, cfg: Configuration | None = None) -> None:
    if not DB_ID.fullmatch(current) or not DB_ID.fullmatch(new):
        raise BackupError('Unsafe database identifier')
    _sql(f'ALTER DATABASE "{current}" RENAME TO "{new}"', cfg=cfg)


def _terminate(name: str, cfg: Configuration | None = None) -> None:
    if not DB_ID.fullmatch(name):
        raise BackupError('Unsafe database identifier')
    _sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
         f"WHERE datname = '{name}' AND pid <> pg_backend_pid()", cfg=cfg)


def _django_check(cfg: Configuration, staged_name: str) -> None:
    try:
        check_staged_migrations(cfg, staged_name)
    except RuntimeErrorSafe as exc:
        raise BackupError(str(exc)) from exc


def _pending_recovery() -> bool:
    try:
        return upgrade_recovery_pending()
    except ServiceError as exc:
        raise BackupError(str(exc)) from None


def _services_stop_for_restore(systemd: SystemdServices) -> list[str]:
    pending = _pending_recovery()
    states = systemd.status(['netbox', 'netbox-rq'])
    if pending:
        # Recovery restores and quiesced backups remain available, but only
        # with both writers already stopped. Never restart them implicitly.
        if (len(states) != 2 or {s.name for s in states} != {'netbox', 'netbox-rq'}
                or any(s.loaded != 'loaded' or s.active != 'inactive' for s in states)):
            raise BackupError('NetBox upgrade recovery is pending; stop both writers first')
        return []
    active = [s.name for s in states if s.active == 'active']
    stopped = []
    try:
        for name in reversed(active):
            systemd.stop([name])
            stopped.append(name)
    except Exception:
        for name in reversed(stopped):
            try:
                _services_restart_after(systemd, [name])
            except Exception:
                pass
        raise
    return active


def _services_restart_after(systemd: SystemdServices, active: list[str]) -> None:
    try:
        require_service_action_allowed('start', active)
    except ServiceError as exc:
        raise BackupError(str(exc)) from None
    for name in active:
        systemd.start([name])


def _replace_artifacts(cfg: Configuration, source_stage: Path) -> Callable[[], None]:
    """Install validated staged files via same-filesystem renames; return rollback callback.

    Directory ownership existing on target is kept, and snapshot is kept in
    secure parent staging directories until caller accepts the transaction.
    """
    records = []
    configured = {name: path for name, path, _ in cfg.artifacts if name == 'media'}
    for node in sorted((source_stage / 'payload').iterdir()):
        name = node.name
        if name not in configured:
            raise BackupError(f'Artifact not configured: {name}')
        target = configured[name]
        if target.is_symlink() or target.parent.is_symlink() or not target.parent.is_dir():
            raise BackupError(f'Unsafe/missing target parent: {target}')
        staging_dir = Path(tempfile.mkdtemp(prefix='.netbox-ops-restore-', dir=target.parent))
        previous = staging_dir / 'previous'
        prepared = staging_dir / 'prepared'
        try:
            if node.is_dir():
                shutil.copytree(node, prepared, symlinks=False, copy_function=shutil.copy2)
            else:
                shutil.copy2(node, prepared)
            if target.exists():
                oldinfo = target.stat()
                if node.is_dir() != target.is_dir():
                    raise BackupError(f'Artifact type mismatch: {target}')
                # Preserve local owners and permissions for existing paths,
                # including NetBox media's writable ownership (not tar UIDs).
                for current, dirs, files in os.walk(prepared) if prepared.is_dir() else []:
                    for name0 in dirs + files:
                        staged_child = Path(current) / name0
                        rel = staged_child.relative_to(prepared)
                        old_child = target / rel
                        if old_child.is_symlink():
                            raise BackupError(f'Existing artifact contains symlink: {old_child}')
                        if old_child.exists():
                            info = old_child.stat()
                        else:
                            parent_old = old_child.parent
                            info = parent_old.stat() if parent_old.exists() else oldinfo
                        os.chown(staged_child, info.st_uid, info.st_gid)
                        if old_child.exists():
                            os.chmod(staged_child, stat.S_IMODE(old_child.stat().st_mode))
                        else:
                            os.chmod(staged_child, stat.S_IMODE(staged_child.stat().st_mode) & ~0o022)
                os.chown(prepared, oldinfo.st_uid, oldinfo.st_gid)
                os.chmod(prepared, stat.S_IMODE(oldinfo.st_mode))
            else:
                # A new path receives secure default ownership. Review manually.
                os.chmod(prepared, 0o700 if prepared.is_dir() else 0o600)
            records.append((target, staging_dir, previous, prepared))
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            for _, existing_staging, _, _ in records:
                shutil.rmtree(existing_staging, ignore_errors=True)
            raise
    changed = []
    try:
        for target, staging_dir, previous, prepared in records:
            had_original = target.exists()
            if had_original:
                os.replace(target, previous)
            try:
                os.replace(prepared, target)
            except Exception:
                if had_original:
                    os.replace(previous, target)
                raise
            changed.append((target, staging_dir, previous, had_original))
    except Exception:
        for target, staging_dir, previous, had_original in reversed(changed):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if had_original:
                os.replace(previous, target)
        for _, staging_dir, _, _ in records:
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    def finish(rollback: bool = False) -> None:
        for target, staging_dir, previous, had_original in reversed(changed):
            if rollback:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if had_original:
                    os.replace(previous, target)
            shutil.rmtree(staging_dir, ignore_errors=True)
    return finish


class BackupManager:
    def __init__(self, cfg: Configuration):
        self.cfg = cfg
        self.directory = cfg.backup_directory

    def list_backups(self, kind: str | None = None) -> list[Path]:
        if not self.directory.is_dir():
            return []
        if kind is not None and kind not in (*RESTORE_KINDS, 'legacy'):
            raise BackupError(f'Invalid backup kind: {kind}')
        suffixes = {'db': '.dump', 'media': '.media.tar.gz',
                    'combined': '.combined.tar.gz', 'full': '.full.tar.gz',
                    'legacy': '.files.tar.gz'}
        paths = (p for p in self.directory.iterdir()
                 if p.is_file() and not p.is_symlink() and
                 (p.name.endswith(suffixes[kind]) if kind else
                  any(p.name.endswith(s) for s in suffixes.values())))
        # Chronological UTC sort, newest first, independent of backup type,
        # purpose (pre_restore/manual), filename prefixes and lexicographic order.
        # For older/unrecognised names, fall back to the file modification time.
        def created_at(path: Path) -> tuple[float, float, str]:
            info = path.stat()
            created = _backup_filename_time(path)
            timestamp = created.timestamp() if created else info.st_mtime
            return (timestamp, info.st_mtime, path.name)
        return sorted(paths, key=created_at, reverse=True)

    @staticmethod
    def classify(path: Path) -> str:
        if path.name.endswith('.media.tar.gz'):
            return 'media'
        if path.name.endswith('.combined.tar.gz'):
            return 'combined'
        if path.name.endswith('.full.tar.gz'):
            return 'full'
        if path.name.endswith('.files.tar.gz'):
            return 'legacy'
        if path.name.endswith('.dump'):
            return 'db'
        raise BackupError(f'Unrecognised backup file: {path.name}')

    @staticmethod
    def backup_purpose(path: Path) -> str:
        """Fast, backwards-compatible recognition from the managed filename."""
        return 'pre_restore' if re.search(r'^netbox-(?:db|media|combined|full)-pre-restore-', path.name) else 'manual'

    def quick_list(self) -> list[BackupEntry]:
        """Filesystem-only inventory for responsive menus; no gzip/manifest reads.

        A filename never proves validity. Integrity must be checked by verify().
        """
        result = []
        for path in self.list_backups():
            try:
                info = path.stat()
                kind = self.classify(path)
            except (OSError, BackupError):
                continue
            match = re.search(r'(\d{8}T\d{6}Z)', path.name)
            stamp = match.group(1) if match else dt.datetime.fromtimestamp(
                info.st_mtime, dt.timezone.utc).isoformat()
            result.append(BackupEntry(path, kind, stamp, info.st_size, '', kind == 'legacy',
                                      self.backup_purpose(path)))
        return result

    def describe_one(self, entry: BackupEntry) -> BackupEntry:
        """Load metadata for ONE selected archive; caller caches by file identity.

        v0.3.1 archives have fast adjacent metadata sidecars. Older archives need
        a sequential gzip scan and should therefore be loaded asynchronously.
        """
        path = entry.path
        if not path.is_file() or path.is_symlink():
            raise BackupError('Backup removed or unsafe')
        kind = self.classify(path)
        try:
            if kind == 'db':
                meta = _read_sidecar_metadata(path)
            else:
                sidecar = Path(str(path) + '.json')
                if sidecar.is_file() and not sidecar.is_symlink():
                    if sidecar.stat().st_size > MAX_MANIFEST_BYTES:
                        raise BackupError('Oversized metadata')
                    meta = json.loads(sidecar.read_text(encoding='utf-8'))
                    if (not isinstance(meta, dict) or meta.get('tool') != 'netbox-ops'
                            or meta.get('format') != FORMAT or meta.get('kind') != kind):
                        raise BackupError('Invalid archive metadata')
                else:
                    meta = _read_archive_metadata(path, kind)
            meta = meta or {}
            legacy = kind == 'legacy' or (
                kind in (*COMBINED_KINDS, 'media') and
                {a.get('id') for a in meta.get('artifacts', []) if isinstance(a, dict)} != {'media'})
            return BackupEntry(path, kind, meta.get('created_utc') or entry.created,
                               path.stat().st_size,
                               (meta.get('netbox_version') or '') if meta.get('netbox_version_source')=='release.yaml' else '', legacy,
                               'pre_restore' if meta.get('purpose') == 'pre_restore' else self.backup_purpose(path))
        except (OSError, BackupError, ValueError, KeyError, json.JSONDecodeError, tarfile.TarError):
            return BackupEntry(path, kind, entry.created, path.stat().st_size, '', True, entry.purpose)

    def describe_backups(self) -> list[BackupEntry]:
        result = []
        for path in self.list_backups():
            kind = self.classify(path)
            created = ''
            version = ''
            legacy = kind == 'legacy'
            try:
                metadata = (_read_sidecar_metadata(path) if kind == 'db'
                            else _read_archive_metadata(path, kind))
                created = (metadata or {}).get('created_utc') or ''
                version = ((metadata or {}).get('netbox_version') or '') if (metadata or {}).get('netbox_version_source') == 'release.yaml' else ''
                if kind in COMBINED_KINDS and {x.get('id') for x in metadata.get('artifacts', [])} != {'media'}:
                    legacy = True
            except (OSError, BackupError, ValueError, KeyError, json.JSONDecodeError):
                legacy = True
            if not created:
                # The name supplies a useful timestamp even if the manifest is damaged.
                match = re.search(r'(\d{8}T\d{6}Z)', path.name)
                created = match.group(1) if match else dt.datetime.fromtimestamp(
                    path.stat().st_mtime, dt.timezone.utc).isoformat()
            result.append(BackupEntry(path, kind, created, path.stat().st_size, version, legacy,
                                      self.backup_purpose(path)))
        return result

    def storage_stats(self) -> tuple[int, int, int]:
        '''(bytes occupied by recognised backups, filesystem bytes free, number).'''
        files = self.list_backups()
        occupied = self._storage_bytes(files)
        base = self.directory if self.directory.exists() else self.directory.parent
        return occupied, shutil.disk_usage(base).free, len(files)

    @staticmethod
    def _storage_bytes(files: list[Path]) -> int:
        occupied = sum(p.stat().st_size for p in files)
        for p in files:
            sidecar = Path(str(p) + '.json')
            if sidecar.is_file() and not sidecar.is_symlink():
                occupied += sidecar.stat().st_size
        return occupied

    def dashboard_storage(self) -> tuple[tuple[int, int, int, int, int], BackupSummary | None]:
        """One chronological inventory and disk measurement; no archive/manifest reads."""
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            base = self.directory.parent
            files = []
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError('Backup directory is not a real directory')
            base = self.directory
            files = self.list_backups()
        occupied = self._storage_bytes(files)
        usage = shutil.disk_usage(base)
        disk = (occupied, max(0, usage.used - occupied), usage.free, usage.total, len(files))
        if not files:
            return disk, None
        path = files[0]  # Existing chronological order, across all recognized types.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise BackupError('Latest backup removed or unsafe')
        kind = self.classify(path)
        timestamp = _backup_filename_time(path)
        source = 'filename' if timestamp is not None else 'mtime'
        if timestamp is None:
            timestamp = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
        prefix = 'netbox-' + ('files' if kind == 'legacy' else kind) + '-'
        # Do not infer a manual backup from an arbitrary imported archive name.
        purpose = self.backup_purpose(path) if path.name.startswith(prefix) else 'unknown'
        return disk, BackupSummary(path, kind, timestamp, source, purpose)

    def storage_breakdown(self) -> tuple[int, int, int, int, int]:
        """(backup bytes, other disk used bytes, free bytes, total bytes, count).

        Use the filesystem containing the configured backup directory. Do not
        confuse backup usage with the filesystem's overall used space.
        """
        occupied, _, count = self.storage_stats()
        base = self.directory if self.directory.exists() else self.directory.parent
        disk = shutil.disk_usage(base)
        return occupied, max(0, disk.used - occupied), disk.free, disk.total, count

    def delete_backups(self, paths: list[Path], progress: ProgressCallback | None = None) -> tuple[int, int]:
        """Delete only explicitly selected local backups with one preflight/lock.

        A failure aborts subsequent deletions; completed deletions cannot be undone.
        """
        _require_root()
        if not paths or len(set(paths)) != len(paths):
            raise BackupError('Select distinct backups to delete')
        with ExclusiveLock():
            # Validate every target before deleting the first one.
            targets = [self._owned_backup_path(path) for path in paths]
            count = 0
            total_bytes = 0
            for path in targets:
                _progress(progress, 'delete', count, len(targets), path.name)
                freed = path.stat().st_size
                path.unlink()
                sidecar = Path(str(path) + '.json')
                if sidecar.is_file() and not sidecar.is_symlink():
                    freed += sidecar.stat().st_size
                    sidecar.unlink()
                count += 1
                total_bytes += freed
                write_audit(f'DELETE BACKUP path={path} bytes={freed}')
                _progress(progress, 'delete', count, len(targets), path.name)
            _progress(progress, 'complete', count, len(targets))
            return count, total_bytes

    def _owned_backup_path(self, source: Path) -> Path:
        _safe_directory(self.directory)
        if source.is_symlink() or source.parent.resolve() != self.directory.resolve():
            raise BackupError('Can only delete local backups directly inside the configured directory')
        path = self.directory / source.name
        if not path.is_file() or path.is_symlink():
            raise BackupError('Backup file is not a regular local file')
        self.classify(path)
        return path

    def delete_backup(self, source: Path) -> int:
        _require_root()
        with ExclusiveLock():
            path = self._owned_backup_path(source)
            released = path.stat().st_size
            path.unlink()
            sidecar = Path(str(path) + '.json')
            if sidecar.is_file() and not sidecar.is_symlink():
                released += sidecar.stat().st_size
                sidecar.unlink()
            write_audit(f'DELETE BACKUP path={path} bytes={released}')
            return released

    def backup(self, kind: str, progress: ProgressCallback | None = None) -> BackupReport:
        _require_root()
        if kind not in KINDS:
            raise BackupError(f'Invalid backup kind: {kind}')
        with ExclusiveLock():
            _safe_directory(self.directory)  # Fail preflight before stopping NetBox.
            if kind != 'combined':
                return self._backup(kind, progress)
            # A consistent DB + media/config snapshot requires quiescing writers.
            # Preserve original service states; never start a previously stopped unit.
            systemd = SystemdServices(self.cfg)
            _progress(progress, 'stop_services', 0, 6)
            active = _services_stop_for_restore(systemd)
            _progress(progress, 'stop_services', 1, 6)
            try:
                return self._backup(kind, progress)
            finally:
                _progress(progress, 'restart_services', 5, 6)
                _services_restart_after(systemd, active)
                _progress(progress, 'complete', 6, 6)

    def _backup(self, kind: str, progress: ProgressCallback | None = None,
                *, purpose: str = 'manual') -> BackupReport:
        if purpose not in ('manual', 'pre_restore'):
            raise BackupError('Invalid backup purpose')
        total = 6 if kind == 'combined' else 3
        _progress(progress, 'prepare', 1 if kind != 'combined' else 2, total)
        directory = _safe_directory(self.directory)
        label = '-pre-restore' if purpose == 'pre_restore' else ''
        name = f'netbox-{kind}{label}-{_stamp()}-{uuid.uuid4().hex[:6]}'
        suffix = '.dump' if kind == 'db' else ('.media.tar.gz' if kind == 'media' else '.combined.tar.gz')
        output = _new_file(directory, name + suffix)
        try:
            if kind == 'db':
                _progress(progress, 'dump_db', 1, 3)
                _db_dump(self.cfg, output)
                _progress(progress, 'dump_db', 2, 3)
                manifest = _base_manifest(kind, self.cfg, purpose)
                manifest['sha256'] = _sha256(output)
                sidecar = _new_file(directory, output.name + '.json')
                sidecar.write_bytes(_json_bytes(manifest))
                os.chmod(sidecar, 0o600)
            else:
                with tempfile.TemporaryDirectory(prefix='.nbo-build-', dir=directory) as temporary:
                    dump = None
                    if kind == 'combined':
                        _progress(progress, 'dump_db', 2, 6)
                        dump = Path(temporary) / 'netbox.dump'
                        _new_file(Path(temporary), 'netbox.dump')
                        _db_dump(self.cfg, dump)
                        _progress(progress, 'dump_db', 3, 6)
                    _progress(progress, 'archive_media', 3 if kind == 'combined' else 1, total)
                    archive_manifest = _create_archive(self.cfg, output, kind, dump, purpose)
                    _progress(progress, 'archive_media', 4 if kind == 'combined' else 2, total)
                _progress(progress, 'verify', 4 if kind == 'combined' else 2, total)
                _validate_archive(output, kind, self.cfg)
                # Fast display metadata: no need to decompress a multi-GiB TAR
                # merely to show its version/date. Integrity still uses verify().
                sidecar = _new_file(directory, output.name + '.json')
                sidecar.write_bytes(_json_bytes({
                    'tool': 'netbox-ops', 'format': FORMAT, 'kind': kind,
                    'created_utc': archive_manifest['created_utc'],
                    'purpose': purpose,
                    'netbox_version': archive_manifest['netbox_version'],
                    'netbox_version_source': archive_manifest['netbox_version_source'],
                    'artifacts': archive_manifest['artifacts'],
                }))
                os.chmod(sidecar, 0o600)
            os.chmod(output, 0o600)
            _progress(progress, 'complete', 5 if kind == 'combined' else 3, total)
            write_audit(f'BACKUP OK kind={kind} path={output} hash={_sha256(output)}')
            return BackupReport(output, kind, f'SHA256={_sha256(output)}')
        except Exception as exc:
            output.unlink(missing_ok=True)
            Path(str(output) + '.json').unlink(missing_ok=True)
            write_audit(f'BACKUP ERROR kind={kind} error={type(exc).__name__}')
            raise

    def verify(self, kind: str, source: Path) -> dict | None:
        source = source.resolve(strict=True)
        if not source.is_file():
            raise BackupError(f'Backup is not a regular file: {source}')
        if kind == 'db':
            _check_dump(source, cfg=self.cfg)
            return _sidecar_manifest(source)
        if kind == 'legacy':
            return _verify_legacy_archive(source)
        return _validate_archive(source, kind, self.cfg)

    def restore(self, kind: str, source: Path, *, only: str = 'all',
                accept_unknown: bool = False, allow_mismatch: bool = False,
                progress: ProgressCallback | None = None) -> BackupReport:
        _require_root()
        if kind not in RESTORE_KINDS:
            raise BackupError(f'Invalid restore kind: {kind}')
        if only not in ('db', 'media', 'all') or (kind not in COMBINED_KINDS and only != 'all'):
            raise BackupError('Partial restore is only supported for combined backups')
        restore_db = kind == 'db' or (kind in COMBINED_KINDS and only in ('all', 'db'))
        restore_media = kind == 'media' or (kind in COMBINED_KINDS and only in ('all', 'media'))
        total = 8
        _progress(progress, 'prepare', 0, total)
        with ExclusiveLock():
            pending = _pending_recovery()
            _safe_directory(self.directory)
            if source.is_symlink():
                raise BackupError('Refusing a symlinked backup source')
            source = source.resolve(strict=True)
            with tempfile.TemporaryDirectory(prefix='.nbo-restore-', dir=self.directory) as tmp:
                stage = Path(tmp)
                # Read from a private copy to avoid source changes between
                # checksum verification and restore (e.g. a dump in /tmp).
                protected = stage / ('incoming' + ('.dump' if kind == 'db' else '.tar.gz'))
                shutil.copyfile(source, protected)
                os.chmod(protected, 0o600)
                sidecar = Path(str(source) + '.json')
                if kind == 'db' and sidecar.exists():
                    if sidecar.is_symlink():
                        raise BackupError('Refusing a symlinked manifest')
                    shutil.copyfile(sidecar, Path(str(protected) + '.json'))
                _progress(progress, 'verify', 0, total)
                manifest = self.verify(kind, protected)
                _progress(progress, 'verify', 1, total)
                if restore_db:
                    _version_guard(manifest, accept_unknown=accept_unknown,
                                   allow_mismatch=allow_mismatch)
                db_source = protected
                # A combined archive must be extracted even when ONLY its database
                # is being restored. Previously full --only db skipped extraction
                # but then referenced stage/database/netbox.dump (nonexistent).
                if kind in COMBINED_KINDS or restore_media:
                    _progress(progress, 'extract_media', 1, total)
                    _validate_archive(protected, kind, self.cfg, extract_dir=stage)
                _progress(progress, 'extract_media', 2, total)
                if kind in COMBINED_KINDS:
                    db_source = stage / 'database/netbox.dump'
                if restore_db:
                    _progress(progress, 'stage_db', 2, total)
                    _preflight_db(self.cfg, db_source)
                    staged_name = _stage_db(self.cfg, db_source)
                else:
                    staged_name = None
                _progress(progress, 'stage_db', 3, total)
                previous_name = None
                old_services: list[str] = []
                old_files_undo = None
                swapped = False
                try:
                    systemd = SystemdServices(self.cfg)
                    if staged_name:
                        # The active database is still untouched and services are running.
                        _progress(progress, 'verify_application', 3, total)
                        _django_check(self.cfg, staged_name)
                    _progress(progress, 'stop_services', 3, total)
                    old_services = _services_stop_for_restore(systemd)
                    _progress(progress, 'stop_services', 4, total)
                    # Safety copies are created only after services have stopped.
                    _progress(progress, 'safety_backup', 4, total)
                    if restore_db:
                        self._backup('db', purpose='pre_restore')
                    if restore_media:
                        self._backup('media', purpose='pre_restore')
                    _progress(progress, 'safety_backup', 5, total)
                    _progress(progress, 'replace', 5, total)
                    if staged_name:
                        previous_name = 'nbo_prev_' + uuid.uuid4().hex[:16]
                        _terminate(self.cfg.database, cfg=self.cfg)
                        _rename_db(self.cfg.database, previous_name, cfg=self.cfg)
                        try:
                            _rename_db(staged_name, self.cfg.database, cfg=self.cfg)
                        except Exception:
                            _rename_db(previous_name, self.cfg.database, cfg=self.cfg)
                            previous_name = None
                            raise
                        swapped = True
                        staged_name = None
                    if restore_media:
                        old_files_undo = _replace_artifacts(self.cfg, stage)
                    _progress(progress, 'replace', 6, total)
                    _progress(progress, 'restart_services', 7, total)
                    _services_restart_after(systemd, old_services)
                    if old_files_undo:
                        old_files_undo(False)
                        old_files_undo = None
                    _progress(progress, 'complete', 8, total)
                    write_audit(f'RESTORE OK kind={kind} only={only} source={source} previous_db={previous_name or "none"}')
                    msg = f'Previous database preserved: {previous_name}' if previous_name else ''
                    if restore_db:
                        msg += '\nRedis cache and RQ data were preserved; check cache coherency before production use.'
                    if pending:
                        msg += '\nNetBox upgrade recovery remains pending; writers were not restarted.'
                    return BackupReport(source, kind, msg)
                except Exception as exc:
                    if old_files_undo:
                        try:
                            old_files_undo(True)
                        except Exception as undo_error:
                            write_audit(f'RESTORE FILES ROLLBACK ERROR {undo_error}')
                    if swapped and previous_name:
                        try:
                            for name in ('netbox-rq', 'netbox'):
                                if name in old_services:
                                    with contextlib.suppress(Exception):
                                        systemd.stop([name])
                            failed_name = 'nbo_failed_' + uuid.uuid4().hex[:16]
                            _terminate(self.cfg.database, cfg=self.cfg)
                            _rename_db(self.cfg.database, failed_name, cfg=self.cfg)
                            _rename_db(previous_name, self.cfg.database, cfg=self.cfg)
                            previous_name = None
                            write_audit(f'RESTORE DB ROLLBACK OK failed_db={failed_name}')
                        except Exception as rollback_error:
                            write_audit(f'RESTORE DB ROLLBACK FAILED error={rollback_error}')
                            raise BackupError(f'Restore failed ({exc}); automatic rollback FAILED '
                                              f'({rollback_error}). Preserve database state.') from exc
                    with contextlib.suppress(Exception):
                        _services_restart_after(systemd, old_services)
                    write_audit(f'RESTORE ERROR kind={kind} error={type(exc).__name__}')
                    raise
                finally:
                    if staged_name is not None:
                        with contextlib.suppress(Exception):
                            _drop_db(staged_name, cfg=self.cfg)
