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
_PIN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?==\d+[A-Za-z0-9.+_-]*$')


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
    release_status: str = 'unknown'  # verified, unavailable, unknown


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


def _published_files(files: object) -> list[dict]:
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, dict)
            and not entry.get('yanked') and entry.get('packagetype') in ('bdist_wheel', 'sdist')]


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
        published = _published_files(files)
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
                             f'NetBox {target} violates declared dependency {spec}', 'blocks'))
        else:
            evidence.append(CompatibilityEvidence('PyPI Requires-Dist',
                             'NetBox dependency cannot be evaluated safely', 'unknown'))
    if not found:
        evidence.append(CompatibilityEvidence('PyPI Requires-Dist',
                                              'No explicit NetBox distribution dependency', 'warns'))
    return evidence, supported


def _range_contains(value: str | None, version: tuple[int, ...], *, upper: bool) -> bool | None:
    if not value:
        return None
    value = value.removeprefix('v')
    wildcard = re.fullmatch(r'(\d+)\.(\d+)\.[x*]', value)
    if wildcard:
        bound = int(wildcard.group(1)), int(wildcard.group(2))
        return version[:2] <= bound if upper else version[:2] >= bound
    bound = _number_version(value)
    if bound is None:
        raise ValueError('Unreviewed declared NetBox version range')
    return version <= bound if upper else version >= bound


def assess_compatibility(package: str, version: str, netbox_version: str,
                         python_version: str, metadata: dict | None,
                         *, published_files: list[dict] | None = None,
                         declaration: CompatibilityDeclaration | None = None,
                         catalog: CompatibilityDeclaration | None = None,
                         preflight: DependencyPreflight | None = None,
                         alternatives: Sequence[str] = ()) -> PluginCompatibility:
    """Block proven technical conflicts; require strong confirmation for unknowns."""
    _validate_package(package)
    if _release_key(version) is None or not _TARGET.fullmatch(netbox_version):
        raise CompatibilityError('Select an exact stable plugin and NetBox version')
    evidence: list[CompatibilityEvidence] = []
    confirmed_python = False
    confirmed_netbox = False
    info = metadata.get('info') if isinstance(metadata, dict) else None
    if not isinstance(info, dict):
        evidence.append(CompatibilityEvidence('PyPI', 'Release metadata unavailable', 'unknown'))
    else:
        actual_package = info.get('name', '')
        actual_version = info.get('version', '')
        if (not isinstance(actual_package, str) or not isinstance(actual_version, str)
                or normalize_name(actual_package) != normalize_name(package)
                or actual_version != version):
            evidence.append(CompatibilityEvidence('PyPI', 'Metadata identity does not match selection', 'unknown'))
        else:
            requirement = info.get('requires_python')
            check = _check_specifiers(requirement, python_version)
            if check is True:
                confirmed_python = True
                evidence.append(CompatibilityEvidence('PyPI Requires-Python',
                                 f'Python {python_version} satisfies {requirement}', 'supports'))
            elif check is False:
                evidence.append(CompatibilityEvidence('PyPI Requires-Python',
                                 f'Python {python_version} violates {requirement}', 'blocks'))
            else:
                evidence.append(CompatibilityEvidence('PyPI Requires-Python',
                                 'No evaluable Python version declaration', 'unknown'))
            dependency_evidence, supported = _netbox_requirements(info.get('requires_dist'),
                                                                  netbox_version.removeprefix('v'))
            evidence.extend(dependency_evidence)
            confirmed_netbox = supported
    if published_files is not None:
        possible = [_check_specifiers(f.get('requires_python'), python_version)
                    for f in published_files if isinstance(f, dict) and not f.get('yanked')]
        if possible and all(check is False for check in possible):
            evidence.append(CompatibilityEvidence('PyPI distribution files',
                                                 'All published files reject the selected Python version', 'blocks'))
        elif possible and True in possible:
            evidence.append(CompatibilityEvidence('PyPI distribution files',
                                                 'At least one published file allows the selected Python version', 'supports'))
        else:
            evidence.append(CompatibilityEvidence('PyPI distribution files',
                                                 'File-level Python compatibility could not be verified', 'unknown'))
    for label, item in (('Plugin release documentation', declaration),
                        ('NetBox Labs plugin catalog', catalog)):
        if item is None:
            continue
        if not item.source.startswith('https://'):
            evidence.append(CompatibilityEvidence(label, 'Unverified source', 'unknown'))
            continue
        actual = _number_version(netbox_version.removeprefix('v'))
        if not (item.min_netbox or item.max_netbox):
            evidence.append(CompatibilityEvidence(label, 'Ambiguous compatibility range', 'unknown'))
            continue
        try:
            lower_ok = _range_contains(item.min_netbox, actual, upper=False)
            upper_ok = _range_contains(item.max_netbox, actual, upper=True)
        except ValueError:
            evidence.append(CompatibilityEvidence(label, 'Ambiguous compatibility range', 'unknown'))
            continue
        in_range = lower_ok is not False and upper_ok is not False
        if in_range:
            evidence.append(CompatibilityEvidence(item.source, 'Target NetBox is in the declared range', 'supports'))
            if not item.advisory and label != 'NetBox Labs plugin catalog':
                confirmed_netbox = True
        elif item.enforced and not item.advisory and label != 'NetBox Labs plugin catalog':
            evidence.append(CompatibilityEvidence(item.source,
                             'Verified PluginConfig gate rejects target NetBox', 'blocks'))
        else:
            evidence.append(CompatibilityEvidence(item.source,
                             'Declared range differs from target; not proof of a technical conflict', 'warns'))
    if preflight:
        outcome = {'passed': 'supports', 'blocked': 'blocks', 'unknown': 'unknown'}.get(preflight.status, 'unknown')
        evidence.append(CompatibilityEvidence('Isolated pip resolver', preflight.detail, outcome))
    blocked = any(record.status == 'blocks' for record in evidence)
    preflight_passed = bool(preflight and preflight.status == 'passed' and preflight.checked_core)
    status = 'blocked' if blocked else ('compatible' if confirmed_python and confirmed_netbox
                                        and preflight_passed and not any(x.status == 'unknown' for x in evidence)
                                        else 'uncertain')
    candidates = tuple(x for x in dict.fromkeys(alternatives) if _release_key(x) is not None and x != version)
    return PluginCompatibility(package, version, netbox_version, python_version,
                               status, tuple(evidence), candidates)


def assess_upgrade_stages(package: str, version: str, netbox_steps: Sequence[str],
                          python_versions: Sequence[str], metadata: dict | None,
                          *, published_files: list[dict] | None = None,
                          declaration: CompatibilityDeclaration | None = None,
                          catalog: CompatibilityDeclaration | None = None,
                          preflights: Sequence[DependencyPreflight | None] | None = None,
                          alternatives: Sequence[str] = ()) -> tuple[PluginCompatibility, ...]:
    if not netbox_steps or len(netbox_steps) != len(python_versions):
        raise CompatibilityError('Specify Python runtime for every NetBox stage')
    if preflights is not None and len(preflights) != len(netbox_steps):
        raise CompatibilityError('Specify preflight for every NetBox stage')
    return tuple(assess_compatibility(package, version, target, python_version, metadata,
                                     declaration=declaration, catalog=catalog,
                                     published_files=published_files, preflight=preflights[i] if preflights is not None else None,
                                     alternatives=alternatives)
                 for i, (target, python_version) in enumerate(zip(netbox_steps, python_versions)))


def isolated_dependency_preflight(plugin_pins: Sequence[str], core_pins: Sequence[str],
                                  python_version: str, *, runner: Callable = subprocess.run,
                                  interpreter: Path | None = None) -> DependencyPreflight:
    """Use pip's dry-run wheel-only resolver; no installation or builds.

    A caller must supply vetted target NetBox core pins and every plugin pin
    for a meaningful full conflict check; missing core pins never confirms it.
    """
    pins = tuple(plugin_pins) + tuple(core_pins)
    if not plugin_pins or not all(isinstance(p, str) and _PIN.fullmatch(p) for p in pins):
        raise CompatibilityError('Resolver accepts only exact safe package pins')
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.\d+)?', python_version)
    if match is None:
        raise CompatibilityError('Resolver requires a numeric target Python version')
    executable = str(interpreter or sys.executable)
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(('PIP_', 'PYTHON')) and name not in ('VIRTUAL_ENV',)}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR='1', PYTHONNOUSERSITE='1')
    with tempfile.TemporaryDirectory(prefix='netbox-ops-resolver-') as temp:
        report = Path(temp) / 'report.json'
        command = [executable, '-I', '-m', 'pip', '--isolated', 'install', '--dry-run',
                   '--ignore-installed', '--only-binary=:all:', '--no-input',
                   '--no-cache-dir', '--disable-pip-version-check', '--quiet',
                   '--timeout', '8', '--retries', '0', '--index-url', PYPI_INDEX,
                   '--python-version', python_version,
                   '--report', str(report), *pins]
        try:
            completed = runner(command, cwd=temp, env=env, text=True, capture_output=True,
                               timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return DependencyPreflight('unknown', 'Isolated resolver unavailable or timed out')
        if completed.returncode:
            error = (completed.stderr or '') + '\n' + (completed.stdout or '')
            if 'ResolutionImpossible' in error or 'conflicting dependencies' in error.lower():
                return DependencyPreflight('blocked', 'pip confirmed conflicting Python dependencies', bool(core_pins))
            return DependencyPreflight('unknown', 'pip could not complete a wheel-only dependency check')
        try:
            parsed = json.loads(report.read_text(encoding='utf-8'))
            if not isinstance(parsed, dict) or not isinstance(parsed.get('install'), list):
                raise ValueError('Invalid resolver report')
        except (OSError, UnicodeError, ValueError):
            return DependencyPreflight('unknown', 'No valid isolated resolver report')
        if not core_pins:
            return DependencyPreflight('unknown', 'Resolver ran without NetBox core dependency pins')
        return DependencyPreflight('passed', 'Wheel-only dependency resolution completed', checked_core=True)


def parse_compatibility_document(markdown: str, plugin_version: str, source: str,
                                 *, advisory: bool = False) -> CompatibilityDeclaration | None:
    """Extract an exact-version range from a small, conventional COMPATIBILITY.md table.

    Ambiguous tables or prose are not treated as proven version constraints.
    """
    if not source.startswith('https://') or len(markdown) > 128 * 1024:
        return None
    if _release_key(plugin_version) is None:
        raise CompatibilityError('Invalid plugin version')
    lines = markdown.splitlines()
    header_indexes = None
    matches: list[tuple[str | None, str | None]] = []
    for line in lines:
        if '|' not in line:
            continue
        columns = [item.strip() for item in line.strip().strip('|').split('|')]
        lowered = [re.sub(r'[^a-z0-9]+', ' ', item.lower()).strip() for item in columns]
        if 'plugin version' in lowered or ('version' in lowered and any('netbox' in x for x in lowered)):
            def locate(terms: tuple[str, ...]) -> int | None:
                return next((i for i, text in enumerate(lowered) if text in terms), None)
            version_at = locate(('plugin version', 'version'))
            minimum_at = locate(('minimum netbox', 'min netbox', 'netbox minimum', 'min netbox version'))
            maximum_at = locate(('maximum netbox', 'max netbox', 'netbox maximum', 'max netbox version'))
            if version_at is not None and (minimum_at is not None or maximum_at is not None):
                header_indexes = (version_at, minimum_at, maximum_at)
            continue
        if header_indexes is None:
            continue
        version_at, minimum_at, maximum_at = header_indexes
        if len(columns) <= version_at or columns[version_at].removeprefix('v') != plugin_version:
            continue
        minimum = columns[minimum_at].removeprefix('v') if minimum_at is not None and len(columns) > minimum_at else ''
        maximum = columns[maximum_at].removeprefix('v') if maximum_at is not None and len(columns) > maximum_at else ''
        if (minimum and not (_TARGET.fullmatch(minimum) or re.fullmatch(r'\d+\.\d+\.[x*]', minimum)) or
                maximum and not (_TARGET.fullmatch(maximum) or re.fullmatch(r'\d+\.\d+\.[x*]', maximum))):
            return None
        if not minimum and not maximum:
            return None
        matches.append((minimum or None, maximum or None))
    if len(matches) != 1:
        return None
    return CompatibilityDeclaration(source, *matches[0], advisory=advisory)


def _github_source_repo(metadata: dict) -> tuple[str, str] | None:
    info = metadata.get('info', {})
    if not isinstance(info, dict):
        return None
    links = info.get('project_urls')
    if not isinstance(links, dict):
        return None
    for key in ('Source', 'Repository', 'Source Code', 'Code'):
        candidate = links.get(key)
        if not isinstance(candidate, str):
            continue
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError:
            continue
        if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or
                parsed.username or parsed.password or port or parsed.query or parsed.fragment):
            continue
        parts = parsed.path.strip('/').removesuffix('.git').split('/')
        if len(parts) == 2 and all(_PACKAGE.fullmatch(x) for x in parts):
            return parts[0], parts[1]
    return None


def fetch_release_declaration(metadata: dict, version: str, *,
                              opener: Callable = urlopen) -> CompatibilityDeclaration | None:
    """Check a plugin publisher's GitHub release-tag COMPATIBILITY.md, if present.

    Uses only the repository claimed by the selected PyPI release, pinned to an
    exact version tag. Never falls back to a mutable main README.
    """
    if _release_key(version) is None:
        raise CompatibilityError('Invalid plugin version')
    repo = _github_source_repo(metadata)
    if repo is None:
        return None
    owner, name = repo
    for tag in ('v' + version, version):
        url = f'https://raw.githubusercontent.com/{owner}/{name}/{tag}/COMPATIBILITY.md'
        request = Request(url, headers={'User-Agent': 'netbox-ops-upgrade-check'})
        try:
            with opener(request, timeout=8) as response:
                data = response.read(128 * 1024 + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            continue
        if len(data) > 128 * 1024:
            continue
        try:
            doc = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if result := parse_compatibility_document(doc, version, url):
            return result
    return None


def review_plugin_upgrade(package: str, netbox_steps: Sequence[str],
                          python_versions: Sequence[str], *, version: str | None = None,
                          catalog: CompatibilityDeclaration | None = None,
                          preflights: Sequence[DependencyPreflight | None] | None = None,
                          fetcher: Callable = fetch_pypi_json,
                          docs_fetcher: Callable = fetch_release_declaration) -> PluginReview:
    """Read-only orchestration for the shared upgrade planner (#82)."""
    _validate_package(package)
    if not netbox_steps or len(netbox_steps) != len(python_versions):
        raise CompatibilityError('Specify Python runtime for each NetBox stage')
    warnings: list[str] = []
    project = None
    listing = None
    try:
        project = fetcher(package)
        listing = discover_stable_releases(package, project, python_versions[-1])
        warnings.extend(listing.warnings)
    except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
        warnings.append('Cannot retrieve the PyPI release listing; check it manually')
    selected = version or (listing.latest if listing else None)
    if selected is None:
        return PluginReview(package, None, listing, (), tuple(warnings))
    if _release_key(selected) is None:
        raise CompatibilityError('Select a stable, numeric release version')
    release_status = 'unknown'
    release_files = None
    if listing is not None:
        entries = project['releases']
        entry = entries.get(selected)
        if isinstance(entry, list):
            release_files = _published_files(entry)
            if release_files:
                release_status = 'verified'
            elif all(isinstance(item, dict) for item in entry):
                release_status = 'unavailable'
        elif selected not in entries:
            release_status = 'unavailable'
    metadata = None
    try:
        metadata = fetcher(package, selected)
    except HTTPError as exc:
        if exc.code == 404:
            release_status = 'unavailable' if release_status != 'verified' else 'unknown'
        warnings.append('Cannot retrieve selected plugin metadata; compatibility remains uncertain')
    except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
        warnings.append('Cannot retrieve selected plugin metadata; compatibility remains uncertain')
    info = metadata.get('info') if isinstance(metadata, dict) else None
    identity_matches = (isinstance(info, dict) and isinstance(info.get('name'), str)
                        and normalize_name(info['name']) == normalize_name(package)
                        and info.get('version') == selected)
    if identity_matches and isinstance(metadata.get('urls'), list):
        # An exact release response can resolve a stale project listing.
        release_files = _published_files(metadata['urls'])
        if release_files:
            release_status = 'verified'
        elif all(isinstance(item, dict) for item in metadata['urls']):
            release_status = 'unavailable'
        else:
            release_status = 'unknown'
    if release_status == 'unavailable':
        warnings.append('Selected version was not found among published non-yanked releases')
    elif release_status == 'unknown':
        warnings.append('Selected release availability could not be verified')
    declaration = None
    if identity_matches:
        try:
            declaration = docs_fetcher(metadata, selected)
        except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
            warnings.append('Cannot verify plugin release documentation')
    alternatives = [item.version for item in listing.stable if item.version != selected] if listing else []
    stages = assess_upgrade_stages(package, selected, netbox_steps, python_versions, metadata,
                                   published_files=release_files, declaration=declaration, catalog=catalog, preflights=preflights,
                                   alternatives=alternatives)
    return PluginReview(package, selected, listing, stages, tuple(warnings), release_status)
