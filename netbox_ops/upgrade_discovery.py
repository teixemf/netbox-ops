"""Read-only discovery and prerequisite checks for Git-based NetBox installs.

Requirement ranges are transcribed from the official NetBox upgrade guide:
https://netbox.readthedocs.io/en/stable/installation/upgrading/#version-history
Unknown future releases and unverified external services remain unknown, never OK.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .netbox_version import read_netbox_version

DEFAULT_NETBOX_ROOT = Path('/opt/netbox')
REQUIREMENTS_SOURCE = 'https://netbox.readthedocs.io/en/stable/installation/upgrading/#version-history'
_RELEASE = re.compile(r'^v?([0-9]+)\.([0-9]+)\.([0-9]+)$')
_VERSION = re.compile(r'^(\d+)\.(\d+)(?:\.(\d+))?')
_SHA = re.compile(r'^[0-9a-f]{40}$')

# NetBox minor: minimum Python, maximum Python, minimum PostgreSQL, minimum Redis.
# Keep unknown releases out of this snapshot until official requirements are reviewed.
_OFFICIAL_REQUIREMENTS = {
    (4, 0): ((3, 10), (3, 12), (12, 0), (4, 0)),
    (4, 1): ((3, 10), (3, 12), (12, 0), (4, 0)),
    (4, 2): ((3, 10), (3, 12), (13, 0), (4, 0)),
    (4, 3): ((3, 10), (3, 12), (14, 0), (5, 0)),
    (4, 4): ((3, 10), (3, 12), (14, 0), (5, 0)),
    (4, 5): ((3, 12), (3, 14), (14, 0), (5, 0)),
    (4, 6): ((3, 12), (3, 14), (14, 0), (5, 0)),
    (4, 7): ((3, 12), (3, 14), (15, 0), (6, 0)),
}

# Do not traverse or display secret local config contents.
_LOCAL_FILES = ('local_requirements.txt', 'netbox/netbox/configuration.py',
                'netbox/netbox/ldap_config.py', 'gunicorn.py')
_LOCAL_DIRECTORIES = ('netbox/media', 'netbox/scripts', 'netbox/reports')


class UpgradeDiscoveryError(Exception):
    """An installation cannot be treated as a safe, supported Git checkout."""


@dataclass(frozen=True)
class GitNetBoxInstallation:
    root: Path
    version: str
    commit: str
    tag: str | None
    reference: str
    origin: str
    python: Path
    python_version: str
    local_paths: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Prerequisite:
    name: str
    required: str
    installed: str | None
    status: str  # ok, blocked, unknown
    detail: str


def _run(args: list[str], *, cwd: Path, runner: Callable = subprocess.run) -> str:
    # Disable optional index writes and fsmonitor hooks. No shell or network.
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    try:
        result = runner(args, cwd=cwd, capture_output=True, text=True, check=False,
                        timeout=8, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeDiscoveryError(f'Cannot inspect NetBox: {args[0]} unavailable or timed out') from exc
    if result.returncode:
        # Do not echo Git stderr; remotes and errors can contain secrets.
        raise UpgradeDiscoveryError(f'NetBox inspection failed: {args[0]} command returned {result.returncode}')
    return result.stdout.strip() if args[-1] != '-z' else result.stdout


def _git(root: Path, *arguments: str, runner: Callable) -> str:
    return _run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                 '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=/dev/null',
                 '-C', str(root), *arguments],
                cwd=root, runner=runner)


def _trusted_origin(origin: str) -> bool:
    """Do not accept mirrors, GitHub lookalikes or credential-bearing URLs."""
    if origin.startswith('git@github.com:'):
        path = origin[len('git@github.com:'):]
    else:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ('https', 'ssh', 'git') or parsed.hostname != 'github.com':
            return False
        if parsed.username not in (None, 'git') or parsed.password or parsed.port:
            return False
        if parsed.query or parsed.fragment:
            return False
        path = parsed.path.lstrip('/')
    return path.removesuffix('.git') == 'netbox-community/netbox'


def _check_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts or root == Path('/'):
        raise UpgradeDiscoveryError('NetBox path must be an absolute, non-traversing directory')
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise UpgradeDiscoveryError('NetBox must be a real directory, not an archive symlink or alias')
    if (not (root / '.git').is_dir() or (root / '.git').is_symlink()
            or (root / '.git').resolve() != root / '.git'):
        raise UpgradeDiscoveryError('Only standard Git checkouts with a .git directory are supported')
    for folder in (root, root / '.git'):
        info = folder.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise UpgradeDiscoveryError('NetBox Git directories must be root-owned and not group/other writable')
    return root


def _inspect_local_paths(root: Path) -> tuple[str, ...]:
    seen = []
    for relative in (*_LOCAL_FILES, *_LOCAL_DIRECTORIES):
        path = root
        for component in Path(relative).parts:
            path = path / component
            if path.is_symlink():
                raise UpgradeDiscoveryError(f'Unsafe local symlink: {relative}')
        if not path.exists():
            continue
        info = path.lstat()
        if relative in _LOCAL_FILES and not stat.S_ISREG(info.st_mode):
            raise UpgradeDiscoveryError(f'Local configuration is not a regular file: {relative}')
        if relative in _LOCAL_DIRECTORIES and not stat.S_ISDIR(info.st_mode):
            raise UpgradeDiscoveryError(f'Local directory has an unexpected type: {relative}')
        seen.append(relative)
    return tuple(seen)


def _inspect_python(root: Path, runner: Callable) -> tuple[Path, str]:
    python = root / 'venv/bin/python'
    venv_config = root / 'venv/pyvenv.cfg'
    for directory in (root / 'venv', root / 'venv/bin'):
        if directory.is_symlink() or not directory.is_dir():
            raise UpgradeDiscoveryError('NetBox Python virtualenv directory is missing or unsafe')
        info = directory.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise UpgradeDiscoveryError('NetBox Python virtualenv directory is not root-controlled')
    if not venv_config.is_file() or venv_config.is_symlink():
        raise UpgradeDiscoveryError('NetBox Python virtual environment is missing or unsafe')
    if not python.exists() or not python.is_file():
        raise UpgradeDiscoveryError('NetBox virtualenv Python interpreter is missing')
    real_python = python.resolve()
    for candidate in (real_python, venv_config):
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise UpgradeDiscoveryError('NetBox Python runtime is not root-controlled')
    version = _run([str(python), '-I', '-S', '-c',
                    'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))'],
                   cwd=root, runner=runner)
    if not _VERSION.fullmatch(version):
        raise UpgradeDiscoveryError('Cannot verify NetBox virtualenv Python version')
    return python, version


def inspect_git_installation(root: Path = DEFAULT_NETBOX_ROOT,
                             *, runner: Callable = subprocess.run) -> GitNetBoxInstallation:
    """Inspect without changing the installation or executing application code."""
    root = _check_root(root)
    actual_root = _git(root, 'rev-parse', '--show-toplevel', runner=runner)
    if actual_root != str(root):
        raise UpgradeDiscoveryError('Git top-level directory differs from the NetBox root')
    origin = _git(root, 'remote', 'get-url', 'origin', runner=runner)
    if not _trusted_origin(origin):
        raise UpgradeDiscoveryError('Git origin is not the official netbox-community/netbox repository')
    commit = _git(root, 'rev-parse', '--verify', 'HEAD', runner=runner)
    if not _SHA.fullmatch(commit):
        raise UpgradeDiscoveryError('Invalid NetBox Git commit')
    status = _git(root, 'status', '--porcelain=v1', '--untracked-files=normal', '-z', runner=runner)
    for entry in status.split('\x00'):
        if not entry:
            continue
        if not entry.startswith('?? '):
            raise UpgradeDiscoveryError('Tracked NetBox Git changes must be resolved before upgrading')
        path = entry[3:].rstrip('/')
        if not any(path == candidate or candidate.startswith(path + '/') or path.startswith(candidate + '/')
                   for candidate in (*_LOCAL_FILES, *_LOCAL_DIRECTORIES)):
            raise UpgradeDiscoveryError('Unrecognized untracked NetBox files require manual review')
    local_paths = _inspect_local_paths(root)
    version = read_netbox_version(root / 'netbox/release.yaml')
    if not _RELEASE.fullmatch(version):
        raise UpgradeDiscoveryError('Cannot identify NetBox release version')
    if int(version.removeprefix('v').split('.')[0]) < 4:
        raise UpgradeDiscoveryError('NetBox 4.0 or newer is required')
    tags = _git(root, 'tag', '--points-at', 'HEAD', '--list', 'v*', runner=runner).splitlines()
    matching_tags = [tag for tag in tags if _RELEASE.fullmatch(tag)]
    expected_tag = 'v' + version
    if matching_tags and expected_tag not in matching_tags:
        raise UpgradeDiscoveryError('Git release tag does not match release.yaml')
    tag = expected_tag if expected_tag in matching_tags else None
    # rev-parse returns HEAD for a normal detached-tag checkout.
    reference = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD', runner=runner)
    python, python_version = _inspect_python(root, runner)
    warnings = () if tag else ('HEAD is not tagged as the installed NetBox release; verify before upgrading',)
    return GitNetBoxInstallation(root, version, commit, tag, reference, origin,
                                 python, python_version, local_paths, warnings)


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    match = _VERSION.match(value)
    return tuple(int(i) for i in match.groups() if i is not None) if match else None


def check_prerequisites(target_version: str, python_version: str | None,
                        *, postgres_version: str | None = None,
                        redis_version: str | None = None) -> tuple[Prerequisite, ...]:
    """Compare verified server versions supplied by the caller, never binary/client versions."""
    match = _RELEASE.fullmatch(target_version)
    if match is None:
        raise ValueError('Target must be a specific stable NetBox release (X.Y.Z)')
    key = (int(match.group(1)), int(match.group(2)))
    if key[0] < 4:
        return (Prerequisite('NetBox release', '>= 4.0', target_version,
                             'blocked', 'NetBox versions before 4.0 are unsupported'),)
    requirements = _OFFICIAL_REQUIREMENTS.get(key)
    if requirements is None:
        return (Prerequisite('NetBox release', 'Official requirements review', target_version,
                             'unknown', f'No reviewed requirements for {target_version}: {REQUIREMENTS_SOURCE}'),)
    py_min, py_max, pg_min, redis_min = requirements

    def fmt(v: tuple[int, int]) -> str:
        return '.'.join(map(str, v))

    results = []
    for name, observed, minimum, maximum in (
        ('Python (NetBox venv)', python_version, py_min, py_max),
        ('PostgreSQL server', postgres_version, pg_min, None),
        ('Redis server', redis_version, redis_min, None),
    ):
        required = f'{fmt(minimum)}–{fmt(maximum)}' if maximum is not None else f'>= {fmt(minimum)}'
        parsed = _parse_version(observed)
        if parsed is None:
            results.append(Prerequisite(name, required, observed, 'unknown',
                                        'Version not verified against the service used by NetBox'))
            continue
        minor = (parsed[0], parsed[1] if len(parsed) > 1 else 0)
        okay = minor >= minimum and (maximum is None or minor <= maximum)
        results.append(Prerequisite(name, required, observed, 'ok' if okay else 'blocked',
                                    'Meets official version range' if okay else 'Incompatible version'))
    if key >= (4, 7):
        results.append(Prerequisite('PostgreSQL ltree/CREATE privilege', 'ltree available and CREATE privilege',
                                    None, 'unknown', 'Check extension and database permissions before upgrade'))
    return tuple(results)
