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
from .plugin_upgrade import RequirementOverlapError, prepare_plugin_upgrade
from .upgrade_checkout import _run_git
from .upgrade_combined import download_stage_wheels
from .upgrade_execution import UpgradeExecutionError
from .upgrade_git import OFFICIAL_REMOTE, PinnedStage, pin_release_stages
from .upgrade_plan import StageDependencyReview, UpgradePlan, _fingerprint
from .upgrade_recovery import _read_config
from .upgrade_runtime import probe_ltree


def target_requirements(pinned: PinnedStage, scratch: Path, *, git: Callable = _run_git) -> bytes:
    """Fetch a shallow official tag into private scratch, not the active repo."""
    scratch.mkdir(mode=0o700)
    git(scratch, ['init', '--bare'])
    git(scratch, ['fetch', '--depth=1', '--no-tags', '--', OFFICIAL_REMOTE,
                  'refs/tags/v' + pinned.version], timeout=180)
    if git(scratch, ['rev-parse', '--verify', 'FETCH_HEAD^{commit}']) != pinned.commit:
        raise UpgradeExecutionError('Target release commit changed during dependency review')
    return git(scratch, ['show', pinned.commit + ':requirements.txt']).encode('utf-8')


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
    if not plan.can_execute:
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
                ltree = 'not-required'
                if tuple(map(int, stage.netbox_version.split('.')[:2])) >= (4, 7):
                    ltree = ltree_probe(cfg, live) if cfg is not None else 'unknown'
                    if ltree not in ('installed', 'available'):
                        blockers.append(prefix + 'PostgreSQL ltree availability or permissions not verified')
                # A blocked requirements stage must not hide independent
                # prerequisite checks or the remaining targets' evidence.
                try:
                    pins = prepare_plugin_upgrade(plan, local, core,
                                                  stage_index=index if stage.kind == 'netbox' else None)
                except UpgradeExecutionError as exc:
                    blockers.append(prefix + str(exc))
                    overlaps = exc.overlaps if isinstance(exc, RequirementOverlapError) else ()
                    reviews.append(StageDependencyReview(
                        stage.netbox_version, pinned.commit, hashlib.sha256(core).hexdigest(), (),
                        DependencyPreflight('not-run', 'Requirements could not be safely prepared'),
                        (), ltree, overlaps,
                    ))
                    continue
                result = resolver(pins.local_pins, pins.core_pins, live.python_version,
                                  interpreter=live.python)
                if result.status == 'unknown':
                    wheelhouse = Path(temp) / f'wheels-{index}'
                    try:
                        download(pins, live, wheelhouse)
                    except UpgradeExecutionError:
                        pass  # A network/build error is uncertainty, not a proven conflict.
                    else:
                        result = resolver(pins.local_pins, pins.core_pins, live.python_version,
                                          interpreter=live.python, wheelhouse=wheelhouse)
                if result.status == 'blocked':
                    blockers.append(prefix + 'Python dependency resolution confirmed a conflict')
                elif result.status != 'passed' or not result.checked_core:
                    warnings.append(prefix + 'Full dependency resolution is uncertain; refresh before executing')
                    # No approved full dependency set means execution cannot safely
                    # choose additional versions later, even with compatibility consent.
                    blockers.append(prefix + 'Cannot verify the complete dependency set')
                expected = dict(result.packages if result.status == 'passed' and result.checked_core else pins.expected)
                changes = tuple(sorted((name, previous.get(name), version)
                                       for name, version in expected.items() if previous.get(name) != version))
                reviews.append(StageDependencyReview(stage.netbox_version, pinned.commit,
                                                     hashlib.sha256(core).hexdigest(), pins.expected,
                                                     result, changes, ltree, pins.overlaps))
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
