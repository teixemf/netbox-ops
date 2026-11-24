"""Self-upgrade catalogue, review and execution shared by CLI and TUI."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .i18n import Translator
from .self_upgrade_archive import ReleaseArchiveError
from .self_upgrade_catalogue import CompatibilityCatalogue, discover_compatibility
from .self_upgrade_compatibility import LocalEnvironment, UpgradeDecision
from .self_upgrade_handoff import discard_prepared_release, launch_prepared_release
from .self_upgrade_installation import InstalledRelease, inspect_installation
from .self_upgrade_prepare import prepare_next_release
from .self_upgrade_result import read_result
from .self_upgrade_transaction import SwapError
from .services import ServiceError
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


# Internal TUI sentinel: leave curses immediately after a successful handoff.
HANDOFF_EXIT = 10


@dataclass(frozen=True)
class Review:
    installed: InstalledRelease
    target: Version
    decision: UpgradeDecision

    @property
    def token(self) -> str:
        return f'UPGRADE v{self.target}'


def review_upgrade(target: Version) -> Review:
    local = inspect_installation()
    if local.version != Version.parse(f'v{__version__}'):
        raise ReleaseArchiveError('Running version differs from installed version')
    catalogue = discover_compatibility(installed=local.version, environment=local.environment)
    return Review(local, target, catalogue.decision_for(target))


def review_lines(review: Review, t: Translator) -> list[str]:
    lines = [t('self_upgrade_installed', version=str(review.installed.version)),
             t('self_upgrade_target', version=f'v{review.target}'),
             *decision_lines(review.decision, t)]
    if review.decision.status == 'compatible' and review.decision.steps:
        lines.append(t('self_upgrade_next_step', version=f'v{review.decision.steps[0]}'))
        if len(review.decision.steps) > 1:
            lines.append(t('self_upgrade_intermediate_notice'))
        lines.append(t('self_upgrade_install_warning'))
    return lines


def execute_review(review: Review, token: str, t: Translator,
                   progress: Callable[[str], None]) -> int:
    """Prepare and hand off only the reviewed next step; never wait for the child."""
    if review.decision.status != 'compatible' or not review.decision.steps:
        progress(t('self_upgrade_install_blocked'))
        return 3
    if token != review.token:
        progress(t('self_upgrade_cancelled'))
        return 3
    if os.geteuid() != 0:
        progress(t('self_upgrade_root_required'))
        return 3
    prepared = None
    try:
        progress(t('self_upgrade_preparing'))
        prepared = prepare_next_release(review.target, expected_installed=review.installed,
                                        expected_steps=review.decision.steps)
        progress(t('self_upgrade_launching'))
        launch_prepared_release(prepared)
    except (ReleaseArchiveError, ReleaseDiscoveryError, SwapError, ServiceError, OSError, ValueError):
        if prepared is not None:
            try:
                discard_prepared_release(prepared)
            except (ReleaseArchiveError, SwapError, ServiceError, OSError):
                progress(t('self_upgrade_cleanup_required'))
        progress(t('self_upgrade_execution_failed'))
        return 1
    return HANDOFF_EXIT


def status_lines(t: Translator) -> tuple[int, list[str]]:
    try:
        result = read_result()
    except (SwapError, OSError):
        return 1, [t('self_upgrade_status_unavailable')]
    if result is None:
        return 4, [t('self_upgrade_no_result')]
    phase = result['phase']
    lines = [t('self_upgrade_result_' + phase),
             t('self_upgrade_result_path', old=result['installed'], step=result['step'],
               target=result['target'])]
    if phase in ('submitted', 'applying', 'recovery_required'):
        lines.append(t('self_upgrade_recovery_hint'))
    if phase == 'completed' and result['step'] != result['target']:
        lines.append(t('self_upgrade_intermediate_notice'))
    if phase == 'completed':
        # A completed intermediate step is not a completed target upgrade.
        return (0 if result['step'] == result['target'] else 4), lines
    return (4 if phase in ('submitted', 'applying') else 1), lines


def run_execution(action: str, t: Translator, *, target: str | None = None,
                  confirmation: str | None = None) -> int:
    if action == 'status':
        code, lines = status_lines(t)
        for line in lines:
            print(line)
        return code
    try:
        if not target:
            raise ValueError('Missing target')
        wanted = Version.parse(target if target.startswith('v') else f'v{target}')
    except ValueError:
        print(t('self_upgrade_invalid_version'), file=sys.stderr)
        return 2
    try:
        review = review_upgrade(wanted)
    except (ReleaseArchiveError, ReleaseDiscoveryError, OSError, ValueError):
        print(t('self_upgrade_review_failed'), file=sys.stderr)
        return 1
    for line in review_lines(review, t):
        print(line)
    if review.decision.status != 'compatible':
        return 3
    if action == 'plan':
        return 0
    if action != 'install':
        return 2
    if confirmation is None:
        if not sys.stdin.isatty():
            print(t('self_upgrade_confirmation_required', token=review.token), file=sys.stderr)
            return 2
        try:
            confirmation = input(t('self_upgrade_confirmation_required', token=review.token) + ' ')
        except (EOFError, KeyboardInterrupt):
            print(t('self_upgrade_cancelled'))
            return 3
    result = execute_review(review, confirmation, t, lambda message: print(message, flush=True))
    if result == HANDOFF_EXIT:
        print(t('self_upgrade_submitted'), flush=True)
        return 0
    return result


def text_menu(t: Translator) -> int | None:
    """Fallback text interface; exit the process after a submitted handoff."""
    run_catalogue('list', t)
    print(t('self_upgrade_text_help'))
    try:
        raw = input(t('prompt')).strip().split()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw == ['status']:
        run_execution('status', t)
    elif len(raw) == 2 and raw[0] == 'show':
        run_catalogue('show', t, target=raw[1])
    elif len(raw) == 2 and raw[0] == 'plan':
        run_execution('plan', t, target=raw[1])
    elif len(raw) == 2 and raw[0] == 'install':
        # A separate sentinel is needed to distinguish handoff from a cancelled menu.
        try:
            target = Version.parse(raw[1] if raw[1].startswith('v') else f'v{raw[1]}')
            review = review_upgrade(target)
        except (ReleaseArchiveError, ReleaseDiscoveryError, OSError, ValueError):
            print(t('self_upgrade_review_failed'))
            return None
        for line in review_lines(review, t):
            print(line)
        if review.decision.status != 'compatible':
            return None
        try:
            token = input(t('self_upgrade_confirmation_required', token=review.token) + ' ')
        except (EOFError, KeyboardInterrupt):
            return None
        if execute_review(review, token, t, print) == HANDOFF_EXIT:
            print(t('self_upgrade_submitted'), flush=True)
            return 0
    return None
