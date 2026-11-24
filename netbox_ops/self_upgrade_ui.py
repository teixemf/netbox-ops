"""Read-only self-upgrade commands shared by future CLI and TUI flows."""
from __future__ import annotations

import sys

from . import __version__
from .i18n import Translator
from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_catalogue import CompatibilityCatalogue
from .self_upgrade_compatibility import LocalEnvironment, UpgradeDecision
from .self_upgrade_releases import (
    OfficialRelease, ReleaseDiscoveryError, Version, discover_releases,
)

MAX_CHANGELOG_CHARS = 40_000


def release_status(release: OfficialRelease, installed: Version,
                   decision: UpgradeDecision | None = None) -> str:
    """Keep eligibility separate from execution authorization."""
    if release.version < installed:
        return 'self_upgrade_older'
    if release.version == installed:
        return 'self_upgrade_current'
    if decision is not None:
        if decision.status == 'compatible':
            return 'self_upgrade_compatible'
        if decision.status == 'incompatible':
            return 'self_upgrade_incompatible'
    return 'self_upgrade_unverified'


def detect_environment(installed: Version) -> LocalEnvironment:
    """Fail closed for source checkouts, non-root access and unknown layouts."""
    from .self_upgrade_installation import inspect_installation

    unknown = LocalEnvironment(None, None, None)
    try:
        local = inspect_installation()
    except (ReleaseArchiveError, OSError):
        return unknown
    if local.version != installed:
        return unknown
    return local.environment


def compatibility_catalogue(releases: list[OfficialRelease],
                            installed: Version) -> CompatibilityCatalogue:
    # No local inspection or manifest request is needed for legacy-only lists.
    environment = (detect_environment(installed)
                   if any(item.version > installed and item.compatibility is not None
                          for item in releases) else LocalEnvironment(None, None, None))
    return CompatibilityCatalogue(releases, installed=installed, environment=environment)


def decision_lines(decision: UpgradeDecision, t: Translator) -> list[str]:
    lines = [t('self_upgrade_reason_' + decision.reason)]
    if decision.blocked_at is not None:
        lines.append(t('self_upgrade_blocked_at', version=str(decision.blocked_at)))
    if decision.steps:
        lines.append(t('self_upgrade_path', steps=' -> '.join(f'v{step}' for step in decision.steps)))
    return lines


def safe_changelog(text: str) -> tuple[str, bool]:
    """Display plain text only; remote content must not control the terminal."""
    text = text.replace('\r\n', '\n')
    limited = len(text) > MAX_CHANGELOG_CHARS
    return ''.join(char if char.isprintable() or char == '\n' else ' '
                   for char in text[:MAX_CHANGELOG_CHARS]), limited


def catalogue_lines(releases: list[OfficialRelease], t: Translator,
                    installed: Version, *,
                    catalogue: CompatibilityCatalogue | None = None) -> list[str]:
    lines = [t('self_upgrade_installed', version=str(installed))]
    if not releases:
        lines.append(t('self_upgrade_empty'))
    for release in sorted(releases, key=lambda item: item.version, reverse=True):
        decision = catalogue.decision_for(release.version) if catalogue is not None else None
        lines.append(f'{release.tag}  {t(release_status(release, installed, decision))}')
    lines.append(t('self_upgrade_read_only'))
    return lines


def changelog_lines(release: OfficialRelease, t: Translator,
                    installed: Version, *,
                    catalogue: CompatibilityCatalogue | None = None) -> list[str]:
    notes, limited = safe_changelog(release.changelog)
    decision = catalogue.decision_for(release.version) if catalogue is not None else None
    lines = [t('self_upgrade_installed', version=str(installed)),
             f'{release.tag}  {t(release_status(release, installed, decision))}',
             *(decision_lines(decision, t) if decision is not None else []),
             t('self_upgrade_changelog'),
             *(notes.splitlines() if notes.strip() else [t('self_upgrade_no_notes')])]
    if limited:
        lines.append(t('self_upgrade_notes_truncated'))
    lines.append(t('self_upgrade_read_only'))
    return lines


def run_catalogue(action: str, t: Translator, *, target: str | None = None) -> int:
    """Fetch and display official stable releases, without configuration or root."""
    if action not in ('list', 'show'):
        return 3
    try:
        installed = Version.parse(f'v{__version__}')
        wanted = None
        if action == 'show':
            if not target:
                raise ValueError('Missing version')
            wanted = Version.parse(target if target.startswith('v') else f'v{target}')
    except ValueError:
        print(t('self_upgrade_invalid_version'), file=sys.stderr)
        return 2
    try:
        releases = discover_releases()
    except (ReleaseDiscoveryError, OSError):
        print(t('self_upgrade_discovery_failed'), file=sys.stderr)
        return 1
    if action == 'list':
        lines = catalogue_lines(releases, t, installed,
                                catalogue=compatibility_catalogue(releases, installed))
    else:
        match = next((release for release in releases if release.version == wanted), None)
        if match is None:
            print(t('self_upgrade_release_missing', version=str(wanted)), file=sys.stderr)
            return 1
        lines = changelog_lines(match, t, installed,
                                catalogue=compatibility_catalogue(releases, installed))
    for line in lines:
        print(line)
    return 0
