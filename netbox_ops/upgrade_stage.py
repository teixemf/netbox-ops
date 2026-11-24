"""Execute one pinned NetBox stage inside the caller's maintenance window."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Callable

from .upgrade_checkout import checkout_pinned_stage
from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .upgrade_execution import UpgradeExecutionError
from .upgrade_git import PinnedStage
from .upgrade_runtime import upgrade_environment
from .upgrade_services import require_quiesced_writers
from .services import SystemdServices


def resolve_base_python(
    installation: GitNetBoxInstallation, *, runner: Callable = subprocess.run,
) -> Path:
    """Select the venv's matching base Python, which survives venv removal."""
    try:
        base = runner(
            [str(installation.python), '-I', '-S', '-c',
             'import sys; print(sys._base_executable)'],
            cwd=installation.root, capture_output=True, text=True, check=False,
            timeout=15,
        )
        if base.returncode:
            raise ValueError
        raw = base.stdout.strip()
        # The upstream shell script evaluates PYTHON as shell text.
        if not re.fullmatch(r'/[A-Za-z0-9_./+-]+', raw):
            raise ValueError
        selected = Path(raw).resolve(strict=True)
        if (selected.is_relative_to(installation.root / 'venv')
                or not re.fullmatch(r'/[A-Za-z0-9_./+-]+', str(selected))):
            raise ValueError
        info = selected.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError
        for parent in selected.parents:
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError
        if not os.access(selected, os.X_OK):
            raise ValueError
        version = runner(
            [str(selected), '-I', '-S', '-c',
             'import sys; print(".".join(map(str, sys.version_info[:3])))'],
            cwd=installation.root, capture_output=True, text=True, check=False,
            timeout=15,
        )
        if version.returncode or version.stdout.strip() != installation.python_version:
            raise ValueError
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise UpgradeExecutionError('Cannot verify a matching base Python outside the NetBox venv') from None
    return selected


def run_stage(
    installation: GitNetBoxInstallation, stage: PinnedStage, *,
    systemd: SystemdServices, runner: Callable = subprocess.run,
    inspector: Callable = inspect_git_installation,
    checkout: Callable = checkout_pinned_stage,
    base_python: Callable = resolve_base_python,
    prepare_script: Callable[[GitNetBoxInstallation], dict[str, str]] | None = None,
) -> GitNetBoxInstallation:
    """Run a stage after verified backup and under the shared exclusive lock.

    Do not restart writers or roll back on failure. The caller must report
    the recovery point and retain maintenance state.
    """
    root = installation.root
    require_quiesced_writers(systemd)
    selected_python = base_python(installation, runner=runner)
    checkout(root, stage, expected_commit=installation.commit,
             runner=runner, inspector=inspector)
    live = inspector(root)
    if (live.root != root or live.commit != stage.commit
            or live.version != stage.version or live.python != installation.python
            or live.python_version != installation.python_version):
        raise UpgradeExecutionError('Stage checkout or selected Python changed')
    script = root / 'upgrade.sh'
    if script.resolve() != script or not script.is_file():
        raise UpgradeExecutionError('NetBox upgrade script is missing or unsafe')
    env = upgrade_environment(selected_python)
    if prepare_script is not None:
        require_quiesced_writers(systemd)
        env.update(prepare_script(live))
    commands = (
        ('upgrade script', ['/bin/bash', str(script)], 3600),
        ('Django system check', [str(live.python), str(root / 'netbox/manage.py'),
                                 'check'], 120),
        ('migration check', [str(live.python), str(root / 'netbox/manage.py'),
                             'migrate', '--check'], 120),
    )
    for label, command, timeout in commands:
        require_quiesced_writers(systemd)
        try:
            result = runner(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise UpgradeExecutionError(f'NetBox {label} unavailable or timed out') from None
        if result.returncode:
            raise UpgradeExecutionError(f'NetBox {label} failed')
    require_quiesced_writers(systemd)
    final = inspector(root)
    if (final.root != root or final.commit != stage.commit
            or final.version != stage.version or final.python != installation.python
            or final.python_version != installation.python_version):
        raise UpgradeExecutionError('NetBox stage changed during upgrade')
    return final
