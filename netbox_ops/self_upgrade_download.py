"""Download an official release into /tmp without touching the installation."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .self_upgrade_archive import (
    MAX_ARCHIVE_BYTES, ReleaseArchiveError, verify_release_archive,
)
from .self_upgrade_manifest import ManifestError, _open as _open_official_asset
from .self_upgrade_releases import DOWNLOAD_PREFIX, OfficialRelease

MAX_CHECKSUM_BYTES = 256
CHUNK_BYTES = 1024 * 1024


def _download(url: str, destination: Path, limit: int, *, opener) -> bytes | None:
    """Stream a bounded asset; return checksum bytes only for small assets."""
    request = Request(url, headers={'User-Agent': 'netbox-ops/self-upgrade'})
    try:
        with opener(request, timeout=30) as response:
            with destination.open('xb') as output:
                total = 0
                while chunk := response.read(min(CHUNK_BYTES, limit + 1 - total)):
                    total += len(chunk)
                    if total > limit:
                        raise ReleaseArchiveError('Release asset exceeds size limit')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except (HTTPError, URLError, TimeoutError, OSError, ManifestError) as exc:
        raise ReleaseArchiveError('Unable to download official release asset') from exc
    return destination.read_bytes() if limit == MAX_CHECKSUM_BYTES else None


def download_release(release: OfficialRelease, *, temp_root: Path = Path('/tmp'),
                     opener=_open_official_asset) -> Path:
    """Return a verified archive in a private /tmp directory.

    The caller owns the returned directory and must remove it after staging.
    No active installation paths are accessed. Compatibility must be checked
    by the caller before this function is used.
    """
    archive_name = f'netbox-ops-{release.tag}.tar.gz'
    checksum_name = f'{archive_name}.sha256'
    if (release.tag != f'v{release.version}' or release.archive.name != archive_name or release.checksum.name != checksum_name
            or release.archive.url != f'{DOWNLOAD_PREFIX}{release.tag}/{archive_name}'
            or release.checksum.url != f'{DOWNLOAD_PREFIX}{release.tag}/{checksum_name}'
            or type(release.archive.size) is not int
            or type(release.checksum.size) is not int
            or not 0 < release.archive.size <= MAX_ARCHIVE_BYTES
            or not 0 < release.checksum.size <= MAX_CHECKSUM_BYTES):
        raise ReleaseArchiveError('Invalid official release assets')
    # Never create private downloads under an untrusted writable parent.
    if (not temp_root.is_absolute() or temp_root.resolve() != temp_root
            or not temp_root.is_dir()):
        raise ReleaseArchiveError('Unsafe temporary directory')
    info = temp_root.stat()
    if info.st_mode & 0o022 and (not info.st_mode & stat.S_ISVTX or info.st_uid != 0):
        raise ReleaseArchiveError('Unsafe temporary directory permissions')
    if info.st_uid not in (0, os.geteuid()):
        raise ReleaseArchiveError('Unsafe temporary directory owner')

    if shutil.disk_usage(temp_root).free < release.archive.size + release.checksum.size + 1024 * 1024:
        raise ReleaseArchiveError('Not enough free space for release download')
    directory = Path(tempfile.mkdtemp(prefix='netbox-ops-upgrade-', dir=temp_root))
    try:
        archive = directory / archive_name
        checksum_file = directory / checksum_name
        _download(release.archive.url, archive, MAX_ARCHIVE_BYTES, opener=opener)
        if archive.stat().st_size != release.archive.size:
            raise ReleaseArchiveError('Archive size differs from GitHub metadata')
        checksum = _download(release.checksum.url, checksum_file,
                             MAX_CHECKSUM_BYTES, opener=opener)
        if checksum_file.stat().st_size != release.checksum.size:
            raise ReleaseArchiveError('Checksum size differs from GitHub metadata')
        verify_release_archive(archive, checksum or b'')
        return archive
    except BaseException:
        shutil.rmtree(directory)
        raise
