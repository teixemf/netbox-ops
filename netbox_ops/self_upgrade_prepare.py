"""Prepare the next step of a freshly verified official upgrade plan.

No active installation is replaced here. The helper must recheck the plan
and the installation identity under its exclusive lock before activation.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_catalogue import discover_compatibility
from .self_upgrade_download import download_release
from .self_upgrade_installation import (
    ACTIVE, InstalledRelease, inspect_installation, verify_candidate_layout,
)
from .self_upgrade_releases import OfficialRelease, ReleaseDiscoveryError, Version
from .self_upgrade_runtime import stage_installation


@dataclass(frozen=True)
class PreparedRelease:
    path: Path
    installed: InstalledRelease
    target: Version
    steps: tuple[Version, ...]
    release: OfficialRelease
    identity: tuple[int, int]


def prepare_release(
    release: OfficialRelease, *,
    installed: Version,
    approve: Callable[[OfficialRelease], None],
    python: Path | None = None,
    temp_root: Path = Path('/tmp'),
    install_parent: Path = Path('/opt'),
    expected_python: tuple[int, int] | None = None,
) -> Path:
    """Low-level preparation primitive; production callers use prepare_next_release."""
    if release.version <= installed or release.tag != f'v{release.version}':
        raise ReleaseArchiveError('Target must be a newer stable release')
    if not callable(approve):
        raise ReleaseArchiveError('Compatibility approval is required')
    approve(release)
    archive = download_release(release, temp_root=temp_root)
    staged = None
    try:
        checksum = archive.with_name(archive.name + '.sha256').read_bytes()
        kwargs = {} if expected_python is None else {'expected_python': expected_python}
        staged = stage_installation(archive, checksum, release.version, python=python,
                                    temp_root=temp_root, install_parent=install_parent, **kwargs)
        return staged
    finally:
        # The private download directory belongs to this operation.
        try:
            shutil.rmtree(archive.parent)
        except BaseException:
            if staged is not None:
                shutil.rmtree(staged)
            raise


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ReleaseArchiveError('Self-upgrade preparation requires root privileges')


def prepare_next_release(
    target: Version, *,
    expected_installed: InstalledRelease,
    expected_steps: tuple[Version, ...],
) -> PreparedRelease:
    """Recheck a reviewed plan and prepare exactly its next required release.

    The production entry point uses the actual installed interpreter, official
    catalogue and canonical paths. It cannot accept an arbitrary approval
    callback, a caller-provided compatibility verdict or alternate asset URL.
    A changed installation or plan requires a new review. Each subsequent
    intermediate upgrade must be re-evaluated from the newly installed version.
    """
    _require_root()
    if (not isinstance(target, Version) or not isinstance(expected_installed, InstalledRelease)
            or target <= expected_installed.version or not isinstance(expected_steps, tuple)
            or not expected_steps or expected_steps[-1] != target
            or any(not isinstance(step, Version) for step in expected_steps)
            or tuple(sorted(set(expected_steps))) != expected_steps
            or expected_steps[0] <= expected_installed.version):
        raise ReleaseArchiveError('Invalid reviewed upgrade plan')
    local = inspect_installation()
    if local != expected_installed:
        raise ReleaseArchiveError('Installation changed; review the upgrade again')
    try:
        catalogue = discover_compatibility(installed=local.version, environment=local.environment)
    except (ReleaseDiscoveryError, OSError) as exc:
        raise ReleaseArchiveError('Cannot verify official upgrade plan') from exc
    decision = catalogue.decision_for(target)
    if decision.status != 'compatible' or decision.steps != expected_steps:
        raise ReleaseArchiveError('Compatibility or upgrade path changed; review again')
    matches = [release for release in catalogue.releases if release.version == decision.steps[0]]
    if len(matches) != 1:
        raise ReleaseArchiveError('Next required release is unavailable')
    selected = matches[0]

    def approve(release: OfficialRelease) -> None:
        if release != selected:
            raise ReleaseArchiveError('Release differs from the verified plan')

    staged = prepare_release(
        selected, installed=local.version, approve=approve,
        python=local.base_python, install_parent=ACTIVE.parent,
        expected_python=local.environment.python,
    )
    try:
        verify_candidate_layout(staged, selected.version)
        if inspect_installation() != local:
            raise ReleaseArchiveError('Installation changed during preparation; review again')
        info = staged.lstat()
        return PreparedRelease(staged, local, target, decision.steps, selected,
                               (info.st_dev, info.st_ino))
    except BaseException:
        shutil.rmtree(staged)
        raise
