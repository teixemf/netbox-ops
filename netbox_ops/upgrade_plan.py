"""Shared read-only upgrade plans for Git NetBox and configured plugins.

All selected versions and compatibility checks are recalculated on every edit.
Planning never checks out Git tags, installs packages, or changes requirements.
The executor must revalidate this plan against the live installation (#83).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .plugin_compatibility import (
    CompatibilityError, DependencyPreflight, PluginCompatibility, PluginReview,
    review_plugin_upgrade,
)
from .plugin_inventory import (
    PluginInventory, configured_plugin_packages, normalize_name, resolve_plugin_package,
)
from .upgrade_discovery import (
    GitNetBoxInstallation, Prerequisite, check_prerequisites,
)

_VERSION = re.compile(r'^v?([0-9]+)\.([0-9]+)\.([0-9]+)$')
# Reviewed NetBox upgrade rule: crossing a major requires the final minor
# of the previous major. Do not predict a future major's final minor.
_LAST_MINOR_BEFORE_MAJOR: dict[int, int] = {}
_UPGRADING_DOCS = 'https://netbox.readthedocs.io/en/stable/installation/upgrading/'
_RELEASE_NOTES = 'https://netbox.readthedocs.io/en/stable/release-notes/version-{}.{}'


class UpgradePlanError(Exception):
    """Invalid, unsupported, or insufficiently verified upgrade selection."""


@dataclass(frozen=True)
class PlanSource:
    installation: GitNetBoxInstallation
    inventory: PluginInventory
    available_tags: tuple[str, ...]
    postgres_version: str | None
    redis_version: str | None
    python_by_stage: tuple[tuple[str, str], ...]
    # These results may be supplied by the isolated resolver after preflight.
    # Absence means "unverified", never "passed".
    preflights: tuple[tuple[str, str, DependencyPreflight], ...]


@dataclass(frozen=True)
class PlannedPlugin:
    package: str
    installed_version: str | None
    proposed_version: str | None
    enabled: bool
    selected: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UpgradeStage:
    kind: str  # netbox, plugins
    previous_netbox: str
    netbox_version: str
    plugin_versions: tuple[tuple[str, str], ...]
    prerequisites: tuple[Prerequisite, ...]
    plugin_checks: tuple[PluginCompatibility, ...]
    instructions: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RequirementOverlap:
    name: str
    local_pin: str
    core_pin: str
    status: str  # redundant, review, conflict, ambiguous


@dataclass(frozen=True)
class LocalRequirementDiagnostic:
    name: str
    declaration: str
    installed: str | None
    stage: str
    reason: str  # unpinned | mismatched | missing | ambiguous | overlap
    action: str  # review-pin | manual-review


@dataclass(frozen=True)
class StageDependencyReview:
    version: str
    commit: str
    core_hash: str
    exact_pins: tuple[tuple[str, str], ...]
    resolution: DependencyPreflight
    changes: tuple[tuple[str, str | None, str], ...]
    ltree: str
    overlaps: tuple[RequirementOverlap, ...] = ()
    local_diagnostics: tuple[LocalRequirementDiagnostic, ...] = ()


@dataclass(frozen=True)
class UpgradePlan:
    source: PlanSource
    current_netbox: str
    target_netbox: str
    selected_plugins: tuple[tuple[str, str | None], ...]
    available_netbox_versions: tuple[str, ...]
    plugins: tuple[PlannedPlugin, ...]
    stages: tuple[UpgradeStage, ...]
    release_notes: tuple[str, ...]
    safeguards: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    requires_strong_confirmation: bool
    fingerprint: str
    dependency_reviews: tuple[StageDependencyReview, ...] = ()

    @property
    def can_execute(self) -> bool:
        return bool(self.stages) and not self.blockers

    @property
    def status(self) -> str:
        if self.blockers:
            return 'blocked'
        if self.requires_strong_confirmation:
            return 'needs_confirmation'
        return 'ready'


def _number(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise UpgradePlanError('NetBox version must be a string')
    match = _VERSION.fullmatch(value)
    if match is None:
        raise UpgradePlanError('NetBox version must be an exact stable release (X.Y.Z)')
    return tuple(int(v) for v in match.groups())


def _release(value: str) -> str:
    number = _number(value)
    return '.'.join(str(x) for x in number)


def _verified_tags(available_tags: Sequence[str]) -> tuple[str, ...]:
    if isinstance(available_tags, (str, bytes)):
        raise UpgradePlanError('Available Git releases must be supplied as a sequence')
    releases = set()
    for item in available_tags:
        if not isinstance(item, str):
            raise UpgradePlanError('Invalid Git release tag')
        # Only standard NetBox stable tags count as verified candidates.
        if item.startswith('v') and _VERSION.fullmatch(item) and _number(item)[0] >= 4:
            releases.add(_release(item))
    return tuple(sorted(releases, key=_number))


def required_netbox_path(current: str, target: str,
                         available_tags: Sequence[str]) -> tuple[str, ...]:
    """Minimum number of intermediate checkouts required by NetBox's major rule.

    The caller supplies verified available Git release tags. Select the highest
    patch in the last minor of each crossed major; never invent a tag.
    """
    present, destination = _number(current), _number(target)
    if present[0] < 4 or destination[0] < 4:
        raise UpgradePlanError('NetBox 4.0 or newer is required')
    if destination < present:
        raise UpgradePlanError('NetBox downgrades are not supported')
    if destination == present:
        return ()
    available = _verified_tags(available_tags)
    target_release = _release(target)
    if target_release not in available:
        raise UpgradePlanError(f'Target v{target_release} is not among verified Git release tags')
    path: list[str] = []
    previous = present
    for major in range(present[0], destination[0]):
        last_minor = _LAST_MINOR_BEFORE_MAJOR.get(major)
        if last_minor is None:
            raise UpgradePlanError(f'No reviewed last-minor upgrade rule for NetBox major {major}: {_UPGRADING_DOCS}')
        if previous[1] > last_minor:
            raise UpgradePlanError(f'Current NetBox minor exceeds the reviewed boundary for major {major}')
        if previous[1] != last_minor:
            candidates = [release for release in available
                          if _number(release)[:2] == (major, last_minor)]
            if not candidates:
                raise UpgradePlanError(
                    f'A verified v{major}.{last_minor}.x intermediate Git tag is required: {_UPGRADING_DOCS}'
                )
            transition = candidates[-1]
            if _number(transition) <= previous:
                raise UpgradePlanError('Intermediate NetBox tag is not newer than the current release')
            path.append(transition)
        # The new major can be entered at any selected stable minor/patch.
        previous = (major + 1, 0, 0)
    path.append(target_release)
    return tuple(path)


def _release_notes_between(current: str, target: str) -> tuple[str, ...]:
    start, finish = _number(current), _number(target)
    if start == finish:
        return ()
    if finish[0] - start[0] > 2:
        raise UpgradePlanError('Upgrade spans too many unreviewed major releases')
    links = []
    # Keep a finite, reviewed range: the documentation for every crossed
    # minor must be read, even when no interim checkout is needed.
    known_majors = {**_LAST_MINOR_BEFORE_MAJOR, 4: 7}
    for major in range(start[0], finish[0] + 1):
        first = start[1] + 1 if major == start[0] else 0
        last = finish[1] if major == finish[0] else known_majors.get(major)
        if last is None:
            raise UpgradePlanError(f'No reviewed release-note range for NetBox {major}.x')
        for minor in range(first, last + 1):
            links.append(_RELEASE_NOTES.format(major, minor) + '/')
    # A patch-only update also needs its own minor's notes.
    if not links and finish > start:
        links.append(_RELEASE_NOTES.format(finish[0], finish[1]) + '/')
    return tuple(links)


def _configured_packages(inventory: PluginInventory) -> tuple[tuple[str, str | None], ...]:
    """Use the shared eligibility contract for configured plugins."""
    return configured_plugin_packages(inventory)


def _validated_selection(inventory: PluginInventory,
                         selected: Mapping[str, str | None]) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(selected, Mapping):
        raise UpgradePlanError('Select plugin packages and explicit stable versions')
    known = set(dict(_configured_packages(inventory)))
    chosen = {}
    for package, version in selected.items():
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', package):
            raise UpgradePlanError('Invalid plugin package name')
        name = normalize_name(package)
        if name not in known or name in chosen:
            raise UpgradePlanError('Select only reliably mapped configured plugins, without duplicates')
        if version is not None:
            if not isinstance(version, str) or not re.fullmatch(
                r'(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,4}(?:\.post\d+)?', version
            ):
                raise UpgradePlanError('Select a stable numeric plugin release or latest (None)')
        chosen[name] = version
    return tuple(sorted(chosen.items()))


def _fingerprint(plan: UpgradePlan) -> str:
    payload = {
        'git_commit': plan.source.installation.commit,
        'root': str(plan.source.installation.root),
        'origin': plan.source.installation.origin,
        'python': (str(plan.source.installation.python), plan.source.installation.python_version),
        'runtimes': plan.source.python_by_stage,
        'local_paths': plan.source.installation.local_paths,
        'enabled_plugins': plan.source.inventory.enabled,
        'installed_packages': sorted((normalize_name(p.name), p.version, p.modules)
                                     for p in plan.source.inventory.packages),
        'netbox': (plan.current_netbox, plan.target_netbox),
        'requirements': plan.source.inventory.requirements_hash,
        'tags': plan.source.available_tags,
        'selected': plan.selected_plugins,
        'plugin_versions': [(p.package, p.installed_version, p.proposed_version) for p in plan.plugins],
        'stages': [(s.kind, s.previous_netbox, s.netbox_version, s.plugin_versions,
                    [(p.name, p.required, p.installed, p.status, p.detail) for p in s.prerequisites],
                    [(p.package, p.version, p.netbox_version, p.python_version, p.status,
                      [(e.source, e.status, e.detail) for e in p.evidence]) for p in s.plugin_checks])
                   for s in plan.stages],
        'warnings': plan.warnings,
        'blockers': plan.blockers,
        'dependency_reviews': [(r.version, r.commit, r.core_hash, r.exact_pins,
                                r.resolution.status, r.resolution.checked_core, r.resolution.packages,
                                r.resolution.detail, r.changes, r.ltree,
                                [(o.name, o.local_pin, o.core_pin, o.status) for o in r.overlaps],
                                [(d.name, d.declaration, d.installed, d.stage, d.reason, d.action)
                                 for d in r.local_diagnostics])
                               for r in plan.dependency_reviews],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def build_upgrade_plan(
    installation: GitNetBoxInstallation,
    inventory: PluginInventory,
    *,
    target_netbox: str | None = None,
    selected_plugins: Mapping[str, str | None] | None = None,
    available_tags: Sequence[str] = (),
    postgres_version: str | None = None,
    redis_version: str | None = None,
    python_by_stage: Mapping[str, str] | None = None,
    preflights: Mapping[tuple[str, str], DependencyPreflight] | None = None,
    reviewer: Callable[..., PluginReview] = review_plugin_upgrade,
) -> UpgradePlan:
    """Build a fresh read-only plan from verified inventory and metadata.

    The reviewer (PyPI/doc plugin) is injected to test without any network.
    Never infer successful dependency resolution from a missing preflight.
    """
    if installation.root != inventory.root:
        raise UpgradePlanError('Plugin inventory belongs to another NetBox installation')
    source_release = _release(installation.version)
    target_release = _release(target_netbox or installation.version)
    tag_catalog = _verified_tags(available_tags)
    path = required_netbox_path(source_release, target_release, available_tags)
    requested = _validated_selection(inventory, selected_plugins or {})
    configured = _configured_packages(inventory)
    installed = {normalize_name(package.name): package.version for package in inventory.packages}
    candidates = set(dict(requested)) | {name for name, _ in configured}
    target_versions = path if path else (source_release,)
    runtime_versions = dict(python_by_stage or ())
    for key, version in runtime_versions.items():
        _number(key)
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version):
            raise UpgradePlanError('Runtime version must be numeric for every upgrade step')
    for key in runtime_versions:
        if key not in target_versions:
            raise UpgradePlanError('A Python runtime override targets an unplanned NetBox stage')
    runtimes = tuple((version, runtime_versions.get(version, installation.python_version))
                     for version in target_versions)
    accepted_preflights = tuple(sorted((normalize_name(pkg), _release(stage), value)
                                       for (pkg, stage), value in (preflights or {}).items()))
    source = PlanSource(installation, inventory, tuple('v' + r for r in tag_catalog),
                        postgres_version, redis_version, runtimes, accepted_preflights)

    notes = _release_notes_between(source_release, target_release)
    warnings = list(inventory.warnings)
    if inventory.enabled is None:
        warnings.append('Enabled PLUGINS could not be read statically; verify them manually')
    elif any(resolve_plugin_package(inventory.packages, module) is None
             for module in inventory.enabled):
        warnings.append('Some enabled plugins could not be mapped to installed distributions')
    if notes:
        warnings.append('Review all intervening NetBox release notes before approving the plan')
    if installation.warnings:
        warnings.extend(installation.warnings)
    if path:
        warnings.append('Check special migration and plugin instructions in release notes manually')
    safeguards = (
        'Create and verify PostgreSQL and media backups before any operation',
        'Protect configuration, plugin pins and previous Git commit as root-only recovery data',
        'Recheck target Git tags, requirements, prerequisites and dependencies at execution time',
        'Check NetBox services and configured plugins after each approved stage',
        'On failure, stop and provide manual recovery instructions; no automatic full rollback',
    )
    blockers: list[str] = []
    current_to_target: dict[tuple[str, str], PluginCompatibility] = {}
    plugin_changes = []
    selected_mapping = dict(requested)
    for name in sorted(candidates):
        original = installed.get(name)
        is_selected = name in selected_mapping
        is_enabled = name in dict(configured)
        proposed = original
        item_warnings = []
        if is_selected:
            chosen = selected_mapping[name]
            proposed = chosen  # Never substitute the installed version on discovery failure.
            try:
                selection_review = reviewer(name, [target_versions[-1]], [runtimes[-1][1]],
                                            version=chosen,
                                            preflights=[dict(((pkg, stage), item)
                                                             for pkg, stage, item in accepted_preflights)
                                                        .get((name, target_versions[-1]))])
            except (CompatibilityError, OSError, ValueError) as exc:
                # No exception details: upstream errors could expose secrets.
                item_warnings.append(f'{name}: plugin release review unavailable ({type(exc).__name__})')
                selection_review = None
            verified = False
            if selection_review is not None:
                if chosen is None:
                    proposed = selection_review.selected_version
                item_warnings.extend(selection_review.warnings)
                if (proposed is not None and selection_review.selected_version == proposed
                        and normalize_name(selection_review.package) == name
                        and len(selection_review.stages) == 1):
                    check = selection_review.stages[0]
                    if (normalize_name(check.package) == name and check.version == proposed
                            and check.netbox_version == target_versions[-1]
                            and check.python_version == runtimes[-1][1]):
                        current_to_target[(name, target_versions[-1])] = check
                        verified = selection_review.release_status == 'verified'
                if selection_review.release_status == 'unavailable':
                    blockers.append(f'{name}: selected plugin release is unavailable')
            if not verified:
                item_warnings.append(f'{name}: selected plugin release is not verified')
                blockers.append(f'{name}: select a verified plugin release before executing')
        else:
            proposed = original
        # When crossing NetBox versions, the installed plugin remains active
        # until the final pin change. Assess it at *every* earlier stage.
        original_stages = target_versions[:-1] if is_selected else target_versions
        if original is not None and original_stages:
            try:
                active_review = reviewer(
                    name, list(original_stages),
                    [dict(runtimes)[version] for version in original_stages],
                    version=original,
                    preflights=[dict(((pkg, stage), item) for pkg, stage, item in accepted_preflights)
                                .get((name, version)) for version in original_stages],
                )
            except (CompatibilityError, OSError, ValueError) as exc:
                item_warnings.append(f'{name}: installed plugin compatibility unavailable ({type(exc).__name__})')
            else:
                item_warnings.extend(active_review.warnings)
                for stage_version, check in zip(original_stages, active_review.stages):
                    current_to_target[(name, stage_version)] = check
        for stage_version in target_versions:
            if (name, stage_version) not in current_to_target:
                item_warnings.append(f'{name}: compatibility is unverified for NetBox {stage_version}')
        plugin_changes.append(PlannedPlugin(name, original, proposed, is_enabled,
                                             is_selected, tuple(dict.fromkeys(item_warnings))))
        warnings.extend(item_warnings)

    stages = []
    previous = source_release
    for index, version in enumerate(target_versions):
        last = index == len(target_versions) - 1
        kind = 'netbox' if previous != version else 'plugins'
        if not path and not requested:
            break  # No upgrade operation was selected.
        required = check_prerequisites(version, dict(runtimes)[version],
                                       postgres_version=postgres_version, redis_version=redis_version)
        stage_warnings = []
        stage_blockers = []
        for check in required:
            if check.status == 'blocked':
                stage_blockers.append(f'{check.name}: {check.detail} ({check.installed}, requires {check.required})')
            elif check.status == 'unknown':
                stage_warnings.append(f'{check.name}: {check.detail} ({check.required})')
        checks = []
        active_versions = []
        for plugin in plugin_changes:
            active = plugin.proposed_version if last and plugin.selected else plugin.installed_version
            if active is not None:
                active_versions.append((plugin.package, active))
            check = current_to_target.get((plugin.package, version))
            if check is None:
                stage_warnings.append(f'{plugin.package}: no verified compatibility evidence for NetBox {version}')
            else:
                checks.append(check)
                if check.status == 'blocked':
                    stage_blockers.append(f'{plugin.package}=={check.version}: confirmed conflict on NetBox {version}')
                elif check.status == 'uncertain':
                    stage_warnings.append(f'{plugin.package}=={check.version}: compatibility uncertain on NetBox {version}')
        for plugin in plugin_changes:
            if last and plugin.selected and plugin.proposed_version is None:
                stage_warnings.append(f'{plugin.package}: selected release cannot be verified')
        instructions = (
            ('Verify the pre-upgrade database/media backup and protected configuration snapshot',
             f'Checkout the approved Git tag v{version} and run the official NetBox upgrade script',
             'Validate HTTP, NetBox services and configured plugin loading')
            if kind == 'netbox' else
            ('Verify database/media backup and protected plugin requirements',
             'Apply only approved plugin pins and rerun the supported NetBox upgrade process',
             'Validate HTTP, NetBox services and configured plugin loading')
        )
        stages.append(UpgradeStage(kind, previous, version, tuple(active_versions), required,
                                   tuple(checks), instructions, tuple(dict.fromkeys(stage_warnings)),
                                   tuple(dict.fromkeys(stage_blockers))))
        warnings.extend(stage_warnings)
        blockers.extend(stage_blockers)
        previous = version

    if not stages:
        blockers.append('No NetBox or plugin changes selected')
    if source_release != target_release and not tag_catalog:
        blockers.append('No verified target Git tags are available')
    warnings = list(dict.fromkeys(warnings))
    blockers = list(dict.fromkeys(blockers))
    needs_confirmation = bool(warnings or any(
        item.status == 'unknown' for stage in stages for item in stage.prerequisites
    ) or any(check.status == 'uncertain' for stage in stages for check in stage.plugin_checks))
    draft = UpgradePlan(source, source_release, target_release, requested, tag_catalog,
                        tuple(plugin_changes), tuple(stages), notes, safeguards,
                        tuple(warnings), tuple(blockers), needs_confirmation, '')
    return UpgradePlan(source, source_release, target_release, requested, tag_catalog,
                       tuple(plugin_changes), tuple(stages), notes, safeguards,
                       tuple(warnings), tuple(blockers), needs_confirmation, _fingerprint(draft))


def edit_upgrade_plan(plan: UpgradePlan, *, target_netbox: str | None = None,
                      selected_plugins: Mapping[str, str | None] | None = None,
                      reviewer: Callable[..., PluginReview] = review_plugin_upgrade) -> UpgradePlan:
    """Rebuild all stage-level evidence instead of patching a stale plan in place."""
    source = plan.source
    destination = target_netbox if target_netbox is not None else plan.target_netbox
    stages = required_netbox_path(plan.current_netbox, destination, source.available_tags)
    relevant = set(stages or (plan.current_netbox,))
    return build_upgrade_plan(
        source.installation, source.inventory,
        target_netbox=destination,
        selected_plugins=selected_plugins if selected_plugins is not None else dict(plan.selected_plugins),
        available_tags=source.available_tags,
        postgres_version=source.postgres_version,
        redis_version=source.redis_version,
        python_by_stage={key: value for key, value in source.python_by_stage if key in relevant},
        # A changed target/plugin choice invalidates old resolver evidence. Always
        # rerun the isolated preflight before considering the edited plan approved.
        preflights={},
        reviewer=reviewer,
    )
