"""Resolve approved release tags against the official NetBox remote.

Read-only preflight: pin every stage to a commit before stopping services.
A local tag alone is not evidence of an official release.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from .upgrade_execution import UpgradeExecutionError
from .upgrade_plan import UpgradePlan

OFFICIAL_REMOTE = 'https://github.com/netbox-community/netbox.git'
_RELEASE = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')
_SHA = re.compile(r'[0-9a-f]{40}')


@dataclass(frozen=True)
class PinnedStage:
    version: str
    commit: str


def pin_release_stages(plan: UpgradePlan, *,
                       runner: Callable = subprocess.run) -> tuple[PinnedStage, ...]:
    """Resolve exact remote tags, peeling annotated tags to their commits."""
    versions = tuple(stage.netbox_version for stage in plan.stages)
    if (not plan.can_execute or not versions or len(set(versions)) != len(versions)
            or any(stage.kind != 'netbox' for stage in plan.stages)
            or any(not _RELEASE.fullmatch(version) for version in versions)):
        raise UpgradeExecutionError('Invalid NetBox release stages')
    refs = [f'refs/tags/v{version}' for version in versions]
    wanted = set(refs) | {ref + '^{}' for ref in refs}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    try:
        result = runner(
            ['git', '-C', '/', 'ls-remote', '--exit-code', '--', OFFICIAL_REMOTE,
             *sorted(wanted)],
            cwd='/', env=env, capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UpgradeExecutionError('Official release lookup failed or timed out') from None
    if result.returncode:
        raise UpgradeExecutionError('Official release lookup failed')
    found = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if (len(fields) != 2 or not _SHA.fullmatch(fields[0])
                or fields[1] not in wanted or fields[1] in found):
            raise UpgradeExecutionError('Invalid official release reference response')
        found[fields[1]] = fields[0]
    if any(ref not in found for ref in refs):
        raise UpgradeExecutionError('An approved release is missing from the official remote')
    return tuple(PinnedStage(version, found.get(ref + '^{}', found[ref]))
                 for version, ref in zip(versions, refs))
