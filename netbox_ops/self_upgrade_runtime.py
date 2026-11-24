"""Prepare and verify the Python runtime of a staged netbox-ops release.

Only call this after official archive verification and compatibility approval.
The current installation, system Python and NetBox services are never modified.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_releases import Version
from .self_upgrade_stage import _check_tree, _safe_parent, stage_release

_RUNTIME_MARGIN = 64 * 1024 * 1024
_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


def _run(args: list[str], cwd: Path, *, timeout: int) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, env=_ENV, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseArchiveError('Cannot execute staged Python check') from exc
    if result.returncode:
        raise ReleaseArchiveError('Staged Python check failed')
    return result.stdout.strip()


def _trusted_python(python: Path, staged: Path) -> Path:
    if not python.is_absolute():
        raise ReleaseArchiveError('Python interpreter must be an absolute path')
    try:
        resolved = python.resolve(strict=True)
    except OSError as exc:
        raise ReleaseArchiveError('Python interpreter is unavailable') from exc
    if (not resolved.is_file() or not os.access(resolved, os.X_OK)
            or resolved.is_relative_to(staged)
            or resolved.is_relative_to(staged.parent / 'netbox-ops')):
        raise ReleaseArchiveError('Python interpreter must be outside the installation')
    if staged.parent == Path('/opt'):
        for path in (resolved, *resolved.parents):
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ReleaseArchiveError('Python interpreter is not root-controlled')
    return resolved


def validate_installation_runtime(root: Path, expected: Version) -> None:
    """Check the launcher Python and installed version without loading config."""
    venv = root / 'venv'
    python = venv / 'bin/python'
    for path in (venv, venv / 'bin', python, venv / 'pyvenv.cfg', root / 'netbox_ops.py'):
        info = path.lstat()
        if (path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o6022):
            raise ReleaseArchiveError('Unsafe staged Python runtime')
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ReleaseArchiveError('Staged Python executable is unavailable')
    prefix = _run([str(python), '-I', '-B', '-c',
                   'import sys; print(sys.prefix)'], root, timeout=15)
    if prefix != str(venv):
        raise ReleaseArchiveError('Python is not using the installation virtual environment')
    version = _run([str(python), '-E', '-s', '-B', str(root / 'netbox_ops.py'),
                    '--version'], root, timeout=15)
    if version != f'netbox-ops {expected}':
        raise ReleaseArchiveError('Installed version does not match the selected release')


def prepare_staged_runtime(staged: Path, expected: Version, *,
                           python: Path | None = None,
                           expected_python: tuple[int, int] | None = None) -> None:
    """Build a relocatable launcher runtime, with no packages or service actions.

    The venv is created with copies and without pip. The existing launcher uses
    venv/bin/python directly; activation scripts are not used by netbox-ops.
    A failed preparation removes only the newly created staged venv.
    """
    _safe_parent(staged.parent)
    if (not staged.name.startswith('.netbox-ops-stage-')
            or staged.resolve() != staged or not staged.is_dir()
            or staged.stat().st_uid != os.geteuid()
            or staged.stat().st_mode & 0o077):
        raise ReleaseArchiveError('Invalid private staging directory')
    _check_tree(staged)
    venv = staged / 'venv'
    if venv.exists() or venv.is_symlink():
        raise ReleaseArchiveError('Staged virtual environment already exists')
    if shutil.disk_usage(staged).free < _RUNTIME_MARGIN:
        raise ReleaseArchiveError('Not enough free space for staged Python')
    interpreter = _trusted_python(python or Path(sys._base_executable), staged)
    version = _run([str(interpreter), '-I', '-B', '-c',
                    'import sys; print(sys.version_info.major, sys.version_info.minor)'],
                   staged, timeout=15)
    try:
        major, minor = map(int, version.split())
    except ValueError as exc:
        raise ReleaseArchiveError('Cannot determine Python version') from exc
    if major != 3 or minor < 12:
        raise ReleaseArchiveError('Python 3.12 or newer is required')
    if expected_python is not None and (major, minor) != expected_python:
        raise ReleaseArchiveError('Base Python changed since compatibility verification')
    try:
        _run([str(interpreter), '-I', '-B', '-m', 'venv', '--copies',
              '--without-pip', str(venv)], staged, timeout=120)
        # Venv can create the standard lib64 -> lib link on Linux. Never
        # execute archive-supplied runtime files; the archive rejects them.
        for path in venv.rglob('*'):
            info = path.lstat()
            if path.is_symlink():
                if path != venv / 'lib64' or os.readlink(path) != 'lib':
                    raise ReleaseArchiveError('Unexpected virtual environment link')
            elif (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                  or info.st_uid != os.geteuid() or info.st_mode & 0o6022):
                raise ReleaseArchiveError('Unsafe virtual environment entry')
        if expected_python is not None:
            actual = _run([str(venv / 'bin/python'), '-I', '-B', '-c',
                           'import sys; print(*sys.version_info[:2])'], staged, timeout=15)
            if actual != ' '.join(map(str, expected_python)):
                raise ReleaseArchiveError('Staged Python differs from verified requirements')
        validate_installation_runtime(staged, expected)
    except BaseException:
        if venv.is_dir() and not venv.is_symlink():
            shutil.rmtree(venv)
        raise


def stage_installation(archive: Path, checksum: bytes, expected: Version, *,
                       python: Path | None = None,
                       temp_root: Path = Path('/tmp'),
                       install_parent: Path = Path('/opt'),
                       expected_python: tuple[int, int] | None = None) -> Path:
    """Prepare a complete candidate after the caller approves compatibility.

    A failure removes the candidate without touching the active installation.
    The returned directory is ready for the helper's final validation and swap.
    """
    if archive.name != f'netbox-ops-v{expected}.tar.gz':
        raise ReleaseArchiveError('Archive name does not match the selected version')
    staged = stage_release(archive, checksum, temp_root=temp_root,
                           install_parent=install_parent)
    try:
        # Check the package's declared version before executing its Python code.
        from .self_upgrade_installation import _read_version

        if _read_version(staged) != expected:
            raise ReleaseArchiveError('Staged package version does not match the selected release')
        prepare_staged_runtime(staged, expected, python=python, expected_python=expected_python)
        return staged
    except BaseException:
        shutil.rmtree(staged)
        raise
