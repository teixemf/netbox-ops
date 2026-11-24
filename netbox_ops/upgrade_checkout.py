"""Guarded checkout primitives for a staged NetBox Git upgrade.

The caller must hold the shared lock, have a verified pre-upgrade backup,
and stop NetBox writers before selecting a stage. These functions do not run
migrations, restart services or attempt automatic rollback.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from .upgrade_discovery import _trusted_origin, inspect_git_installation
from .upgrade_execution import UpgradeExecutionError
from .upgrade_git import OFFICIAL_REMOTE, PinnedStage

_SHA = re.compile(r'[0-9a-f]{40}\Z')
_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\Z')


def _run_git(root: Path, arguments: list[str], *,
             runner: Callable = subprocess.run, timeout: int = 120) -> str:
    """Run Git without inherited Git overrides or hooks; never leak Git output."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    command = ['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
               '-C', str(root), *arguments]
    try:
        result = runner(command, cwd=root, env=env, capture_output=True,
                        text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise UpgradeExecutionError('NetBox Git command unavailable or timed out') from None
    if result.returncode:
        raise UpgradeExecutionError('NetBox Git command failed') from None
    return result.stdout.strip()


def fetch_pinned_stage(
    root: Path, stage: PinnedStage, *, runner: Callable = subprocess.run,
    inspector: Callable = inspect_git_installation,
) -> None:
    """Fetch an exact official release and check its commit before checkout.

    This modifies Git object storage / FETCH_HEAD, but not the worktree.
    It is safe to call before the maintenance window.
    """
    if not _VERSION.fullmatch(stage.version) or not _SHA.fullmatch(stage.commit):
        raise UpgradeExecutionError('Invalid pinned NetBox release')
    live = inspector(root)
    # Discovery verifies ownership, checkout structure and the official origin.
    # Do not trust a custom inspector that returns a foreign remote.
    if live.root != root or not _trusted_origin(live.origin):
        raise UpgradeExecutionError('Unsupported NetBox Git installation during fetch')
    ref = 'refs/tags/v' + stage.version
    _run_git(root, ['fetch', '--no-tags', '--', OFFICIAL_REMOTE, ref],
             runner=runner, timeout=180)
    resolved = _run_git(root, ['rev-parse', '--verify', 'FETCH_HEAD^{commit}'],
                        runner=runner)
    if resolved != stage.commit:
        raise UpgradeExecutionError('Fetched NetBox release differs from approved Git commit')


def checkout_pinned_stage(
    root: Path, stage: PinnedStage, *, expected_commit: str,
    runner: Callable = subprocess.run,
    inspector: Callable = inspect_git_installation,
) -> None:
    """Select a previously fetched, pinned release; reject drift and dirty files.

    Never use --force: unknown local changes and untracked paths must not be
    overwritten. The caller retains the recovery point on any failure.
    """
    if not _SHA.fullmatch(expected_commit) or not _SHA.fullmatch(stage.commit):
        raise UpgradeExecutionError('Invalid expected NetBox Git commit')
    if not _VERSION.fullmatch(stage.version):
        raise UpgradeExecutionError('Invalid NetBox stage version')
    live = inspector(root)
    if (live.root != root or live.commit != expected_commit
            or not _trusted_origin(live.origin)):
        raise UpgradeExecutionError('NetBox Git checkout changed before stage')
    resolved = _run_git(root, ['rev-parse', '--verify', stage.commit + '^{commit}'],
                        runner=runner)
    if resolved != stage.commit:
        raise UpgradeExecutionError('Pinned NetBox commit is not available locally')
    status = _run_git(root, ['status', '--porcelain=v1', '--untracked-files=no'],
                      runner=runner)
    if status:
        raise UpgradeExecutionError('Tracked NetBox files changed before checkout')
    _run_git(root, ['checkout', '--no-overwrite-ignore', '--detach', stage.commit], runner=runner)
    actual = _run_git(root, ['rev-parse', '--verify', 'HEAD'], runner=runner)
    if actual != stage.commit:
        raise UpgradeExecutionError('NetBox checkout did not reach the approved revision')
