"""Execute one pinned NetBox stage inside the caller's maintenance window."""
from __future__ import annotations

import subprocess
from typing import Callable

from .upgrade_checkout import checkout_pinned_stage
from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .upgrade_execution import UpgradeExecutionError
from .upgrade_git import PinnedStage
from .upgrade_services import require_quiesced_writers
from .services import SystemdServices


def run_stage(
    installation: GitNetBoxInstallation, stage: PinnedStage, *,
    systemd: SystemdServices, runner: Callable = subprocess.run,
    inspector: Callable = inspect_git_installation,
    checkout: Callable = checkout_pinned_stage,
) -> GitNetBoxInstallation:
    """Run a stage after verified backup and under the shared exclusive lock.

    Do not restart writers or roll back on failure. The caller must report
    the recovery point and retain maintenance state.
    """
    root = installation.root
    require_quiesced_writers(systemd)
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
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
           'LANG': 'C.UTF-8', 'PYTHON': str(live.python),
           'PIP_NO_INPUT': '1', 'GIT_TERMINAL_PROMPT': '0'}
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
