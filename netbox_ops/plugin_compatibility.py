"""Read-only NetBox plugin compatibility evidence and optional pip preflight.

PyPI metadata is evidence, not a guarantee that a NetBox plugin loads. Unknown
or unparsed declarations never become proof of incompatibility. Catalog ranges
are advisory; a confirmed Python or package dependency conflict is blocking.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .plugin_inventory import normalize_name

PYPI_API = 'https://pypi.org/pypi'
PYPI_INDEX = 'https://pypi.org/simple'
_MAX_JSON_BYTES = 3 * 1024 * 1024
_PACKAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
# Accept only unambiguous numeric stable releases; exotic versions need review.
_STABLE = re.compile(r'^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,4}(?:\.post\d+)?$')
_TARGET = re.compile(r'^v?\d+\.\d+\.\d+$')
_SPEC = re.compile(r'\s*(<=|>=|==|!=|~=|<|>)\s*(\d+(?:\.\d+){0,4}(?:\.\*)?)\s*')
_DIST = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*(?:\(([^)]*)\)|([^;]*?))?\s*(?:;\s*(.*))?$')
_PIN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}==\d+[A-Za-z0-9.+_-]*$')


class CompatibilityError(Exception):
    """Invalid input, not a confirmed compatibility conflict."""


@dataclass(frozen=True)
class ReleaseChoice:
    version: str
    requires_python: str | None
    status: str  # ok, blocked, unknown


@dataclass(frozen=True)
class ReleaseCatalog:
    package: str
    stable: tuple[ReleaseChoice, ...]
    latest: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CompatibilityDeclaration:
    """A curated claim from a plugin release's own documentation or a catalog.

    Documents must be checked against the *selected plugin release*, not a
    mutable main-branch README that may describe newer functionality.
    """
    source: str
    min_netbox: str | None = None
    max_netbox: str | None = None
    advisory: bool = False
    enforced: bool = False  # Only verified PluginConfig runtime gates.


@dataclass(frozen=True)
class PluginReview:
    package: str
    selected_version: str | None
    releases: ReleaseCatalog | None
    stages: tuple[PluginCompatibility, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DependencyPreflight:
    status: str  # passed, blocked, unknown
    detail: str
    checked_core: bool = False


@dataclass(frozen=True)
class CompatibilityEvidence:
    source: str
    detail: str
    status: str  # supports, blocks, warns, unknown


@dataclass(frozen=True)
class PluginCompatibility:
    package: str
    version: str
    netbox_version: str
    python_version: str
    status: str  # compatible, uncertain, blocked
    evidence: tuple[CompatibilityEvidence, ...]
    alternatives: tuple[str, ...]

    @property
    def requires_strong_confirmation(self) -> bool:
        return self.status == 'uncertain'

    @property
    def can_continue(self) -> bool:
        return self.status != 'blocked'


def _validate_package(value: str) -> str:
    if not isinstance(value, str) or not _PACKAGE.fullmatch(value):
        raise CompatibilityError('Invalid Python distribution name')
    return value


def _release_key(version: str) -> tuple[tuple[int, ...], int] | None:
    if not isinstance(version, str) or not _STABLE.fullmatch(version):
        return None
    before, _, post = version.partition('.post')
    parts = [int(item) for item in before.split('.')]
    return tuple((parts + [0] * (5 - len(parts)))[:5]), int(post) if post else -1


def _number_version(version: str) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+){0,4}', version):
        return None
    values = [int(p) for p in version.split('.')]
    return tuple((values + [0] * (5 - len(values)))[:5])


def _check_specifiers(specifiers: str | None, actual: str) -> bool | None:
    """Conservative subset of PEP 440; unsupported clauses return unknown."""
    if specifiers is None or not specifiers.strip():
        return None
    actual_parts = _number_version(actual)
    if actual_parts is None:
        return None
    clauses = specifiers.split(',')
    parsed = []
    for clause in clauses:
        match = _SPEC.fullmatch(clause)
        if not match:
            return None
        op, bound = match.groups()
        if bound.endswith('.*'):
            if op not in ('==', '!='):
                return None
            prefix = tuple(int(x) for x in bound[:-2].split('.'))
            parsed.append((op, prefix, True, bound))
        else:
            parts = _number_version(bound)
            if parts is None:
                return None
            if op == '~=' and '.' not in bound:
                return None
            parsed.append((op, parts, False, bound))
    for op, bound, wildcard, raw_bound in parsed:
        if wildcard:
            matches = actual_parts[:len(bound)] == bound
            okay = matches if op == '==' else not matches
        elif op == '~=':
            lower = bound
            # ~=4.5 => >=4.5,<5 ; ~=4.5.2 => >=4.5.2,<4.6
            # The original segment count is needed to calculate the exclusive upper bound.
            segments = [int(x) for x in str(raw_bound).split('.')]
            upper = segments[:-1]
            upper[-1] += 1
            upper = tuple((upper + [0] * (5 - len(upper)))[:5])
            okay = actual_parts >= lower and actual_parts < upper
        else:
            okay = {
                '==': actual_parts == bound, '!=': actual_parts != bound,
                '<': actual_parts < bound, '>': actual_parts > bound,
                '<=': actual_parts <= bound, '>=': actual_parts >= bound,
            }[op]
        if not okay:
            return False
    return True


def fetch_pypi_json(package: str, version: str | None = None,
                    *, opener: Callable = urlopen) -> dict:
    """Fetch only bounded JSON from a fixed PyPI hostname; never fetch wheels."""
    package = _validate_package(package)
    if version is not None and not _STABLE.fullmatch(version):
        raise CompatibilityError('Invalid stable release identifier')
    endpoint = f'{PYPI_API}/{quote(package, safe="")}'
    if version is not None:
        endpoint += '/' + quote(version, safe='')
    request = Request(endpoint + '/json', headers={'Accept': 'application/json',
                                                      'User-Agent': 'netbox-ops-upgrade-check'})
    with opener(request, timeout=8) as response:
        raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise CompatibilityError('PyPI response exceeded size limit')
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise CompatibilityError('Invalid PyPI JSON metadata') from exc
    if not isinstance(result, dict):
        raise CompatibilityError('Invalid PyPI JSON object')
    return result


def discover_stable_releases(package: str, project: dict, python_version: str) -> ReleaseCatalog:
    """Use non-yanked published files; never treat a beta as the latest stable."""
    package = _validate_package(package)
    releases = project.get('releases')
    if not isinstance(releases, dict):
        raise CompatibilityError('PyPI release listing unavailable')
    output: list[ReleaseChoice] = []
    skipped = 0
    for version, files in releases.items():
        if not isinstance(version, str):
            continue
        if _release_key(version) is None:
            if not any(marker in version.lower() for marker in ('a', 'b', 'rc', 'dev')):
                skipped += 1
            continue
        if not isinstance(files, list):
            skipped += 1
            continue
        published = [entry for entry in files if isinstance(entry, dict)
                     and not entry.get('yanked') and entry.get('packagetype') in ('bdist_wheel', 'sdist')]
        if not published:
            continue
        python_checks = [_check_specifiers(entry.get('requires_python'), python_version)
                         for entry in published]
        if True in python_checks:
            status = 'ok'
        elif python_checks and all(check is False for check in python_checks):
            status = 'blocked'
        else:
            status = 'unknown'
        output.append(ReleaseChoice(version, next((entry.get('requires_python')
                                                   for entry in published if entry.get('requires_python')), None), status))
    output.sort(key=lambda release: _release_key(release.version), reverse=True)
    warnings = []
    if skipped:
        warnings.append('Some release identifiers could not be classified; verify the latest stable release')
    if not output:
        warnings.append('No published non-yanked stable PyPI release found')
    return ReleaseCatalog(package, tuple(output), output[0].version if output else None,
                          tuple(warnings))


def _netbox_requirements(requires_dist: object, target: str) -> tuple[list[CompatibilityEvidence], bool]:
    evidence: list[CompatibilityEvidence] = []
    supported = False
    if not isinstance(requires_dist, list):
        return [CompatibilityEvidence('PyPI Requires-Dist', 'No usable NetBox requirement declaration', 'unknown')], False
    found = False
    for requirement in requires_dist:
        if not isinstance(requirement, str):
            continue
        match = _DIST.fullmatch(requirement)
        if match is None or normalize_name(match.group(1)) != 'netbox':
            continue
        found = True
        spec = (match.group(2) or match.group(3) or '').strip()
        if match.group(4):
            evidence.append(CompatibilityEvidence('PyPI Requires-Dist',
                             'Conditional NetBox dependency requires marker evaluation', 'unknown'))
            continue
        check = _check_specifiers(spec, target)
        if check is True:
            supported = True
            evidence.append(CompatibilityEvidence('PyPI Requires-Dist',
                             f'NetBox {target} satisfies declared dependency {spec}', 'supports'))
        elif check is False:
            evidence.append(CompatibilityEvidence('PyPI Requires-Dist',
