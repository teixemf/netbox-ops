"""Prepare an approved official release without changing the active tree."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_download import download_release
from .self_upgrade_releases import OfficialRelease, Version
from .self_upgrade_runtime import stage_installation


def prepare_release(
    release: OfficialRelease, *,
    installed: Version,
    approve: Callable[[OfficialRelease], None],
    python: Path | None = None,
    temp_root: Path = Path('/tmp'),
    install_parent: Path = Path('/opt'),
) -> Path:
    """Require an explicit compatibility decision before fetching any asset.

    The approval callback must reject unknown or incompatible releases and
    mandatory intermediate steps. The caller owns the returned private
    candidate and must recheck compatibility under the swap lock.
    """
    if release.version <= installed or release.tag != f'v{release.version}':
        raise ReleaseArchiveError('Target must be a newer stable release')
    if not callable(approve):
        raise ReleaseArchiveError('Compatibility approval is required')
    approve(release)
    archive = download_release(release, temp_root=temp_root)
    try:
        checksum = archive.with_name(archive.name + '.sha256').read_bytes()
        return stage_installation(archive, checksum, release.version, python=python,
                                  temp_root=temp_root, install_parent=install_parent)
    finally:
        # download_release returns a private directory created for this call.
        shutil.rmtree(archive.parent)
