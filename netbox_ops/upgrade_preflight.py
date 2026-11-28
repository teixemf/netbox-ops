"""Read-only, fingerprinted dependency evidence for every editable upgrade plan."""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .config import Configuration
from .plugin_compatibility import CompatibilityError, DependencyPreflight, isolated_dependency_preflight
from .plugin_inventory import normalize_name
from .plugin_upgrade import PluginUpgrade, RequirementOverlapError, diagnose_local_requirements, prepare_plugin_upgrade
from .upgrade_checkout import _run_git
from .upgrade_combined import download_stage_wheels
from .upgrade_execution import UpgradeExecutionError
from .upgrade_diagnostics import CommandFailure
from .upgrade_git import OFFICIAL_REMOTE, PinnedStage, pin_release_stages
from .upgrade_plan import StageDependencyReview, UpgradePlan, _fingerprint
from .upgrade_recovery import _read_config
from .upgrade_runtime import LtreeEvidence, probe_ltree


def target_requirements(pinned: PinnedStage, scratch: Path, *, git: Callable = _run_git) -> bytes:
    """Fetch a shallow official tag into private scratch, not the active repo."""
    scratch.mkdir(mode=0o700)
    git(scratch, ['init', '--bare'])
    git(scratch, ['fetch', '--depth=1', '--no-tags', '--', OFFICIAL_REMOTE,
                  'refs/tags/v' + pinned.version], timeout=180)
    if git(scratch, ['rev-parse', '--verify', 'FETCH_HEAD^{commit}']) != pinned.commit:
        raise UpgradeExecutionError('Target release commit changed during dependency review')
    return git(scratch, ['show', pinned.commit + ':requirements.txt']).encode('utf-8')


def _review_resolution(pins: PluginUpgrade, live, scratch: Path, resolver, download) -> DependencyPreflight:
    """Prefer installed bare local versions; retry the actual declaration if necessary."""
    preferred = {pin.split('==', 1)[0]: pin for pin in pins.preferred_local}
    candidates = []
    if preferred:
        candidates.append(replace(pins, local_pins=tuple(preferred.get(pin, pin) for pin in pins.local_pins)))
    candidates.append(pins)
    for attempt, candidate in enumerate(candidates):
        options = {'allow_unpinned': True} if any('==' not in pin for pin in candidate.local_pins) else {}
        result = resolver(candidate.local_pins, candidate.core_pins, live.python_version,
                          interpreter=live.python, **options)
        if result.status == 'unknown':
            wheelhouse = scratch / f'wheels-{attempt}'
            try:
                download(candidate, live, wheelhouse)
            except CommandFailure as exc:
                if exc.command_id == 'wheel.ldap':
                    result = DependencyPreflight('unknown', exc.safe_message())
            except UpgradeExecutionError:
                pass
            else:
                result = resolver(candidate.local_pins, candidate.core_pins, live.python_version,
                                  interpreter=live.python, wheelhouse=wheelhouse, **options)
        if result.status == 'passed' and result.checked_core:
            return result
    return result


def review_plan_dependencies(plan: UpgradePlan, cfg: Configuration | None, *,
                             pin: Callable = pin_release_stages, core_reader: Callable = target_requirements,
                             resolver: Callable = isolated_dependency_preflight,
                             download: Callable = download_stage_wheels,
                             ltree_probe: Callable = probe_ltree) -> UpgradePlan:
    """Resolve all core and local pins and expose changes before approval.

    Build a temporary wheelhouse if the wheel-only dry run cannot handle the
    upstream psycopg[c] source-build requirement. No live checkout, install,
    service action or requirements modification is allowed here.
    """
    if not plan.stages:
        return plan
    warnings, blockers = list(plan.warnings), list(plan.blockers)
    reviews = []
    live = plan.source.installation
    previous = {normalize_name(p.name): p.version for p in plan.source.inventory.packages}
    try:
        local = _read_config(live.root, 'local_requirements.txt')
        stages = (pin(plan) if plan.stages[0].kind == 'netbox' else
                  (PinnedStage(live.version, live.commit),))
        with tempfile.TemporaryDirectory(prefix='netbox-ops-plan-') as temp:
            for index, (stage, pinned) in enumerate(zip(plan.stages, stages, strict=True)):
                prefix = f'NetBox {stage.netbox_version}: '
                if pinned.version != stage.netbox_version:
                    raise UpgradeExecutionError('Dependency review release mismatch')
                core = (core_reader(pinned, Path(temp) / f'git-{index}') if stage.kind == 'netbox'
                        else _read_config(live.root, 'requirements.txt'))
                if core is None:
                    raise UpgradeExecutionError('NetBox core requirements are missing')
                evidence = LtreeEvidence('not-required')
                if tuple(map(int, stage.netbox_version.split('.')[:2])) >= (4, 7):
                    evidence = ltree_probe(cfg, live) if cfg is not None else LtreeEvidence('unknown')
                    if evidence.status != 'installed' and (evidence.status != 'available' or stage.kind != 'netbox'):
                        guidance = {
                            'missing': 'extension files unavailable; ask the DBA to install the matching PostgreSQL extension package',
                            'permission': 'migration role lacks extension creation privileges; ask the DBA',
                            'unusable': 'extension type or schema/search path unusable; ask the DBA',
                            'mismatch': 'database or runtime does not match the backup target',
                            'available': 'extension not installed; plugin-only operation cannot create core extensions',
                            'unknown': 'extension prerequisites unverified; inspect database connection and permissions',
                        }
                        blockers.append(prefix + 'PostgreSQL ltree: ' + guidance.get(evidence.status, 'unverified'))
                ltree = evidence.status
                ltree_fields = dict(ltree_details=evidence.details, ltree_identity=evidence.identity,
                                    ltree_admin_port=evidence.admin_port)
                # A blocked requirements stage must not hide independent
                # prerequisite checks or the remaining targets' evidence.
                diagnostics = diagnose_local_requirements(plan, local, core, index)
                if not plan.can_execute:
                    reviews.append(StageDependencyReview(
                        stage.netbox_version, pinned.commit, hashlib.sha256(core).hexdigest(), (),
                        DependencyPreflight('not-run', 'Plan has independent compatibility or prerequisite blockers'),
                        (), ltree, local_diagnostics=diagnostics, **ltree_fields,
                    ))
                    continue
                try:
                    pins = prepare_plugin_upgrade(plan, local, core,
                                                  stage_index=index if stage.kind == 'netbox' else None)
                except UpgradeExecutionError as exc:
                    blockers.append(prefix + str(exc))
                    overlaps = exc.overlaps if isinstance(exc, RequirementOverlapError) else ()
                    reviews.append(StageDependencyReview(
                        stage.netbox_version, pinned.commit, hashlib.sha256(core).hexdigest(), (),
                        DependencyPreflight('not-run', 'Requirements could not be safely prepared'),
                        (), ltree, overlaps, diagnostics, **ltree_fields,
                    ))
                    continue
                scratch = Path(temp) / f'resolver-{index}'
                scratch.mkdir(mode=0o700)
                result = _review_resolution(pins, live, scratch, resolver, download)
                if result.status == 'blocked':
                    blockers.append(prefix + 'Python dependency resolution confirmed a conflict')
                elif result.status != 'passed' or not result.checked_core:
                    warnings.append(prefix + 'Full dependency resolution is uncertain; refresh before executing')
                    # No approved full dependency set means execution cannot safely
                    # choose additional versions later, even with compatibility consent.
                    blockers.append(prefix + 'Cannot verify the complete dependency set')
                    if result.detail.startswith('Isolated python-ldap wheel build'):
                        blockers.append(prefix + result.detail)
                expected = dict(result.packages if result.status == 'passed' and result.checked_core else pins.expected)
                changes = tuple(sorted((name, previous.get(name), version)
                                       for name, version in expected.items() if previous.get(name) != version))
                reviews.append(StageDependencyReview(stage.netbox_version, pinned.commit,
                                                     hashlib.sha256(core).hexdigest(), pins.expected,
                                                     result, changes, ltree, pins.overlaps, **ltree_fields))
                previous.update(expected)
    except UpgradeExecutionError as exc:
        # These helpers raise fixed local messages; never display arbitrary
        # subprocess output, URLs, or requirements content.
        blockers.append(str(exc))
    except (CompatibilityError, OSError, ValueError, TypeError):
        blockers.append('Cannot complete read-only dependency review')
    draft = replace(plan, dependency_reviews=tuple(reviews), warnings=tuple(dict.fromkeys(warnings)),
                    blockers=tuple(dict.fromkeys(blockers)),
                    requires_strong_confirmation=bool(warnings) or plan.requires_strong_confirmation)
    return replace(draft, fingerprint=_fingerprint(draft))
