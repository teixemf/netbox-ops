"""Inspect the existing archive installation before authorizing a self-upgrade."""
from __future__ import annotations

import ast
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .platform import detect_platform
from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_compatibility import LocalEnvironment
from .self_upgrade_releases import Version
from .self_upgrade_runtime import _run, _trusted_python

ACTIVE = Path('/opt/netbox-ops')
LAUNCHER = Path('/usr/local/sbin/netbox-ops')
LAUNCHER_LINK = Path('/usr/sbin/netbox-ops')
ARCHIVE_LAYOUT = 'archive'

_REQUIRED_FILES = frozenset({
    'LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
    'install.sh', 'upgrade.sh', 'netbox_ops.py',
})
_ALLOWED_TOP = _REQUIRED_FILES | {'netbox_ops', 'venv', '__pycache__'}
_PYTHON_NAME = re.compile(r'python(?:3(?:\.[0-9]+t?)?)?')


@dataclass(frozen=True)
class InstalledRelease:
    version: Version
    environment: LocalEnvironment
    base_python: Path
    identity: tuple[int, int]


def _controlled(info: os.stat_result, uid: int) -> bool:
    return info.st_uid == uid and not info.st_mode & 0o6022


def _check_parent(path: Path, uid: int) -> None:
    info = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
            or not _controlled(info, uid)):
        raise ReleaseArchiveError('Unsafe installation or launcher parent')


def _check_python_link(root: Path, path: Path, uid: int) -> None:
    target = path.resolve(strict=True)
    if path == root / 'venv/lib64':
        if path.readlink() != Path('lib') or not target.is_dir():
            raise ReleaseArchiveError('Unexpected virtual environment link')
        return
    if path.parent != root / 'venv/bin' or not _PYTHON_NAME.fullmatch(path.name):
        raise ReleaseArchiveError(f'Unexpected installation symlink: {path.relative_to(root)}')
    if not target.is_file() or not os.access(target, os.X_OK):
        raise ReleaseArchiveError('Invalid virtual environment Python link')
    if target.is_relative_to(root):
        if (target.parent != root / 'venv/bin'
                or not _PYTHON_NAME.fullmatch(target.name)
                or not _controlled(target.stat(), uid)):
            raise ReleaseArchiveError('Invalid local Python link target')
    else:
        # A normal install.sh venv uses symlinks to a system interpreter.
        # Do not allow links to user-writable paths outside the installation.
        for entry in (target, *target.parents):
            info = entry.stat()
            if info.st_uid not in (0, uid) or info.st_mode & 0o6022:
                raise ReleaseArchiveError('Python link target is not controlled')


def _raise_walk_error(error: OSError) -> None:
    raise error


def _check_tree(root: Path, uid: int, *, allow_python_links: bool) -> os.stat_result:
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or not _controlled(info, uid)
            or root.resolve(strict=True) != root):
        raise ReleaseArchiveError('Unsafe installation directory')
    top = {entry.name for entry in root.iterdir()}
    if not _REQUIRED_FILES | {'netbox_ops', 'venv'} <= top or top - _ALLOWED_TOP:
        raise ReleaseArchiveError('Unknown installation layout or local files')
    for name in _REQUIRED_FILES:
        if not (root / name).is_file() or (root / name).is_symlink():
            raise ReleaseArchiveError('Missing release file')
    for name in ('netbox_ops', 'venv'):
        if not (root / name).is_dir() or (root / name).is_symlink():
            raise ReleaseArchiveError('Missing installation directory')
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            entry = path.lstat()
            if entry.st_uid != uid or entry.st_dev != info.st_dev:
                raise ReleaseArchiveError('Unexpected installation owner or filesystem')
            if stat.S_ISLNK(entry.st_mode):
                if not allow_python_links and path != root / 'venv/lib64':
                    raise ReleaseArchiveError('Unexpected staged runtime symlink')
                _check_python_link(root, path, uid)
            elif (not (stat.S_ISREG(entry.st_mode) or stat.S_ISDIR(entry.st_mode))
                  or not _controlled(entry, uid)):
                raise ReleaseArchiveError('Unsafe installation permissions or file type')
    return info


def _read_version(root: Path) -> Version:
    path = root / 'netbox_ops/__init__.py'
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise ReleaseArchiveError('Invalid installed version file')
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        versions = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == '__version__'
                            for target in node.targets)]
        if len(versions) != 1:
            raise ValueError('Missing or ambiguous version')
        value = ast.literal_eval(versions[0])
        if not isinstance(value, str):
            raise ValueError('Invalid version')
        return Version.parse(f'v{value}')
    except (SyntaxError, ValueError, TypeError, UnicodeError) as exc:
        raise ReleaseArchiveError('Invalid installed version') from exc


def _check_launcher(root: Path, launcher: Path, link: Path, uid: int) -> None:
    _check_parent(launcher.parent, uid)
    _check_parent(link.parent, uid)
    expected = f'#!/bin/sh\nexec {root}/venv/bin/python {root}/netbox_ops.py "$@"\n'
    info = launcher.lstat()
    if (not stat.S_ISREG(info.st_mode) or not _controlled(info, uid)
            or not info.st_mode & 0o111 or info.st_size != len(expected.encode())
            or launcher.read_text(encoding='utf-8') != expected):
        raise ReleaseArchiveError('Unknown netbox-ops launcher')
    info = link.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != uid or link.readlink() != launcher:
        raise ReleaseArchiveError('Unknown netbox-ops launcher link')


def inspect_installation(
    root: Path = ACTIVE, *,
    launcher: Path = LAUNCHER,
    link: Path = LAUNCHER_LINK,
) -> InstalledRelease:
    """Accept the existing release-archive layout, never a Git checkout.

    Verify ownership and executable paths before asking the local runtime
    for its version. Unknown files are not silently removed by a future swap.
    """
    uid = 0 if root == ACTIVE else os.geteuid()
    try:
        if root.name != 'netbox-ops' or not root.is_absolute():
            raise ReleaseArchiveError('Unknown installation path')
        _check_parent(root.parent, uid)
        info = _check_tree(root, uid, allow_python_links=True)
        _check_launcher(root, launcher, link, uid)
        version = _read_version(root)
        python = root / 'venv/bin/python'
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ReleaseArchiveError('Missing installation Python')
        result = _run(
            [str(python), '-I', '-B', '-c',
             'import sys; print(sys.prefix); print(*sys.version_info[:2]); print(sys._base_executable)'],
            root, timeout=15,
        )
        prefix, python_version, base = result.splitlines()
        major, minor = map(int, python_version.split())
        if prefix != str(root / 'venv') or major != 3 or minor < 12:
            raise ReleaseArchiveError('Unsupported installation Python')
        interpreter = _trusted_python(Path(base), root.with_name('.netbox-ops-stage-check'))
        reported = _run([str(python), '-E', '-s', '-B', str(root / 'netbox_ops.py'),
                         '--version'], root, timeout=15)
        if reported != f'netbox-ops {version}':
            raise ReleaseArchiveError('Installed version does not match package metadata')
        return InstalledRelease(version, LocalEnvironment((major, minor),
                                detect_platform().family, ARCHIVE_LAYOUT),
                                interpreter, (info.st_dev, info.st_ino))
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        raise ReleaseArchiveError('Unable to verify installed release layout') from exc


def verify_candidate_layout(staged: Path, expected: Version) -> None:
    """Verify staged ownership, tree layout and declared version before execution."""
    if (not staged.is_absolute() or not staged.name.startswith('.netbox-ops-stage-')
            or staged.stat().st_mode & 0o077):
        raise ReleaseArchiveError('Expected private staging directory')
    uid = 0 if staged.parent == Path('/opt') else os.geteuid()
    _check_parent(staged.parent, uid)
    _check_tree(staged, uid, allow_python_links=False)
    if _read_version(staged) != expected:
        raise ReleaseArchiveError('Staged package version does not match approval')
