"""Read official netbox-ops releases without changing the local installation.

Compatibility decisions will be added once the release manifest contract is fixed.
This module intentionally never treats API metadata as proof of a safe upgrade.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = 'https://api.github.com/repos/teixemf/netbox-ops/releases'
DOWNLOAD_PREFIX = 'https://github.com/teixemf/netbox-ops/releases/download/'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 5
TAG_PATTERN = re.compile(r'^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')


class ReleaseDiscoveryError(Exception):
    """An official release listing could not be trusted or retrieved."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, tag: str) -> Version:
        if not isinstance(tag, str):
            raise ValueError('Version must be a string')
        match = TAG_PATTERN.fullmatch(tag)
        if match is None:
            raise ValueError(f'Invalid stable release tag: {tag}')
        return cls(*(int(component) for component in match.groups()))

    def __str__(self) -> str:
        return f'{self.major}.{self.minor}.{self.patch}'


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class OfficialRelease:
    version: Version
    tag: str
    changelog: str
    published_at: str
    archive: ReleaseAsset
    checksum: ReleaseAsset


def _official_asset(asset: object, name: str, tag: str) -> ReleaseAsset | None:
    if not isinstance(asset, dict) or asset.get('name') != name:
        return None
    url = asset.get('browser_download_url')
    size = asset.get('size')
    expected = f'{DOWNLOAD_PREFIX}{tag}/{name}'
    if url != expected or type(size) is not int or size <= 0:
        return None
    return ReleaseAsset(name=name, url=url, size=size)


def parse_releases(payload: object) -> list[OfficialRelease]:
    """Return stable releases with exact official archive and checksum assets.

    The result is *not* an eligibility verdict. A compatibility manifest is
    mandatory before a target can be offered for installation (#72/#75).
    """
    if not isinstance(payload, list):
        raise ReleaseDiscoveryError('Invalid GitHub releases response')
    found: dict[Version, OfficialRelease] = {}
    seen_versions: set[Version] = set()
    for item in payload:
        if not isinstance(item, dict) or item.get('draft') is not False or item.get('prerelease') is not False:
            continue
        tag = item.get('tag_name')
        try:
            version = Version.parse(tag)
        except ValueError:
            continue
        # Count stable tags before validating assets or release notes.
        if version in seen_versions:
            found.pop(version, None)
            continue
        seen_versions.add(version)
        assets = item.get('assets')
        if not isinstance(assets, list) or len(assets) > 100:
            continue
        archive_name = f'netbox-ops-{tag}.tar.gz'
        checksum_name = f'{archive_name}.sha256'
        archive = [asset for asset in assets if isinstance(asset, dict)
                   and asset.get('name') == archive_name]
        checksum = [asset for asset in assets if isinstance(asset, dict)
                    and asset.get('name') == checksum_name]
        if len(archive) != 1 or len(checksum) != 1:
            continue
        archive = [_official_asset(asset, archive_name, tag) for asset in archive]
        checksum = [_official_asset(asset, checksum_name, tag) for asset in checksum]
        valid_archives = [asset for asset in archive if asset is not None]
        valid_checksums = [asset for asset in checksum if asset is not None]
        if len(valid_archives) != 1 or len(valid_checksums) != 1:
            continue
        body = item.get('body')
        published = item.get('published_at')
        if not isinstance(body, str) or not isinstance(published, str) or not published:
            continue
        found[version] = OfficialRelease(
            version, tag, body, published, valid_archives[0], valid_checksums[0]
        )
    return sorted(found.values(), key=lambda release: release.version, reverse=True)


def _read_page(url: str, *, opener=urlopen) -> list[dict]:
    request = Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'netbox-ops/self-upgrade',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with opener(request, timeout=10) as response:
            # Bound memory even if the server advertises a smaller Content-Length.
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ReleaseDiscoveryError(f'Unable to fetch official releases: {exc}') from exc
    if len(data) > MAX_RESPONSE_BYTES:
        raise ReleaseDiscoveryError('GitHub releases response is too large')
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseDiscoveryError('Invalid GitHub releases JSON') from exc
    if not isinstance(parsed, list):
        raise ReleaseDiscoveryError('Unexpected GitHub releases response')
    return parsed


def discover_releases(*, opener=urlopen, max_pages: int = MAX_PAGES) -> list[OfficialRelease]:
    """Fetch bounded pages of releases from the fixed, official GitHub API."""
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError('Invalid release page limit')
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        results = _read_page(f'{API_URL}?per_page=100&page={page}', opener=opener)
        rows.extend(results)
        if len(results) < 100:
            break
    return parse_releases(rows)
