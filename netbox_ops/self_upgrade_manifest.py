"""Read and encode the schema-v1 compatibility contract for self-upgrade.

The publisher contract is coordinated in #72. A missing or unsupported
manifest never supplies requirements and cannot authorize installation.
"""
from __future__ import annotations

import json
import re
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .self_upgrade_compatibility import ReleaseRequirements, _valid_requirements
from .self_upgrade_releases import DOWNLOAD_PREFIX, MAX_MANIFEST_BYTES, OfficialRelease, Version

_FIELDS = {
    'schema_version', 'version', 'python_min', 'python_max_exclusive',
    'linux_families', 'layouts', 'required_intermediates',
}
_PYTHON = re.compile(r'(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})')
_NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
_MAX_ITEMS = 64


class ManifestError(ValueError):
    """Compatibility metadata cannot be trusted or interpreted."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError('Duplicate compatibility manifest key')
        result[key] = value
    return result


def _python_version(value):
    match = _PYTHON.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ManifestError('Invalid Python requirement')
    return tuple(map(int, match.groups()))


def _names(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= _MAX_ITEMS
            or any(not isinstance(item, str) or not _NAME.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ManifestError('Invalid compatibility names')
    return frozenset(value)


def parse_manifest(data: bytes, expected: Version) -> ReleaseRequirements:
    """Normalize exact schema-v1 data, with no permissive defaults."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MANIFEST_BYTES:
        raise ManifestError('Invalid compatibility manifest size')
    try:
        obj = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
        if (not isinstance(obj, dict) or set(obj) != _FIELDS
                or type(obj['schema_version']) is not int or obj['schema_version'] != 1
                or obj['version'] != f'v{expected}'):
            raise ManifestError('Unsupported schema or release version')
        intermediate = obj['required_intermediates']
        if not isinstance(intermediate, list) or len(intermediate) > _MAX_ITEMS:
            raise ManifestError('Invalid intermediate release requirements')
        result = ReleaseRequirements(
            version=expected,
            python_min=_python_version(obj['python_min']),
            python_max_exclusive=(
                None if obj['python_max_exclusive'] is None
                else _python_version(obj['python_max_exclusive'])
            ),
            linux_families=_names(obj['linux_families']),
            layouts=_names(obj['layouts']),
            required_intermediates=tuple(Version.parse(tag) for tag in intermediate),
        )
        if not _valid_requirements(result, expected):
            raise ManifestError('Inconsistent compatibility requirements')
        return result
    except ManifestError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise ManifestError('Malformed compatibility manifest') from exc


def encode_manifest(requirements: ReleaseRequirements) -> bytes:
    """Encode the same strict contract used by the consumer.

    Release tooling can reuse this function instead of duplicating the schema.
    No assets are written or published here.
    """
    if not isinstance(requirements, ReleaseRequirements) or not isinstance(requirements.version, Version):
        raise ManifestError('Invalid release requirements')
    if not _valid_requirements(requirements, requirements.version):
        raise ManifestError('Inconsistent compatibility requirements')

    def python_text(version):
        return '.'.join(map(str, version))

    document = {
        'schema_version': 1,
        'version': f'v{requirements.version}',
        'python_min': python_text(requirements.python_min),
        'python_max_exclusive': (
            None if requirements.python_max_exclusive is None
            else python_text(requirements.python_max_exclusive)
        ),
        'linux_families': sorted(requirements.linux_families),
        'layouts': sorted(requirements.layouts),
        'required_intermediates': [f'v{step}' for step in requirements.required_intermediates],
    }
    encoded = (json.dumps(document, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if parse_manifest(encoded, requirements.version) != requirements:
        raise ManifestError('Compatibility metadata does not round-trip')
    return encoded


class _OfficialAssetRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urlsplit(newurl)
            allowed = (parts.scheme == 'https'
                       and parts.hostname in ('release-assets.githubusercontent.com',
                                              'objects.githubusercontent.com')
                       and parts.port in (None, 443)
                       and parts.username is None and parts.password is None)
        except ValueError:
            allowed = False
        if not allowed:
            raise ManifestError('Unexpected compatibility asset redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request, timeout):
    return build_opener(_OfficialAssetRedirect()).open(request, timeout=timeout)


def read_release_requirements(release: OfficialRelease, *, opener=_open) -> ReleaseRequirements:
    """Fetch a bounded, version-matched asset from the official GitHub release."""
    name = f'netbox-ops-{release.tag}.compatibility.json'
    asset = release.compatibility
    if (release.tag != f'v{release.version}' or asset is None or asset.name != name
            or asset.url != f'{DOWNLOAD_PREFIX}{release.tag}/{name}'
            or type(asset.size) is not int or not 0 < asset.size <= MAX_MANIFEST_BYTES):
        raise ManifestError('Official compatibility asset is missing or invalid')
    request = Request(asset.url, headers={'User-Agent': 'netbox-ops/self-upgrade'})
    try:
        with opener(request, timeout=10) as response:
            data = response.read(MAX_MANIFEST_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise ManifestError('Unable to read official compatibility metadata') from exc
    if len(data) != asset.size:
        raise ManifestError('Compatibility asset size differs from GitHub metadata')
    return parse_manifest(data, release.version)
