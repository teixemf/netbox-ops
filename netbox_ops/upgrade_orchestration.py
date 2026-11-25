"""Coordinate approved NetBox, plugin and combined upgrades under the shared lock."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from dataclasses import replace
from typing import Callable

from .config import Configuration
from .services import ExclusiveLock, SystemdServices, write_audit
from .upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
    validate_netbox_only_pins,
)
from .upgrade_checkout import fetch_pinned_stage
from .upgrade_combined import prepare_combined_stages, recheck_combined_stage, run_combined_stage
from .upgrade_git import PinnedStage, pin_release_stages
from .upgrade_health import read_netbox_health, wait_for_netbox_health
from .plugin_inventory import inventory_plugins, normalize_name
from .plugin_upgrade import preflight_plugin_upgrade, run_plugin_stage
from .upgrade_maintenance import begin_maintenance, finish_maintenance
from .upgrade_plan import UpgradePlan
from .upgrade_recovery import UpgradeRecovery, _capture_quiesced_recovery, _private_directory
from .upgrade_runtime import validate_upgrade_runtime
from .upgrade_services import (
    require_quiesced_writers, start_upgrade_writers, stop_upgrade_writers,
)
from .upgrade_stage import resolve_base_python, run_stage
from .upgrade_process import ManagedProcessError, UpgradeInterrupted, check_cancelled, upgrade_signals


# Only fixed, reviewed backend messages may be included in a recovery diagnostic.
# Never pass through arbitrary exception text or subprocess output.
_SAFE_STAGE_ERRORS = frozenset({
    'NetBox Git command unavailable or timed out',
    'NetBox Git command failed',
    'Pinned NetBox commit is not available locally',
    'NetBox Git checkout changed before stage',
    'Tracked NetBox files changed before checkout',
    'NetBox checkout did not reach the approved revision',
    'Stage checkout or selected Python changed',
    'Cannot verify a matching base Python outside the NetBox venv',
    'NetBox upgrade script is missing or unsafe',
    'NetBox stage changed during upgrade',
    'Prepared combined wheel directory is unavailable',
    'Combined stage local requirements or configured plugins changed',
    'Checked out core requirements differ from preflight',
    'Local requirements changed after checkout',
    'Combined stage installed versions differ from approved pins',
    'Installed plugin or core versions differ from approved pins',
    'Plugin dependency check failed',
    'Cannot verify the configured plugin registry',
    *(
        f'NetBox {label} {reason}'
        for label in ('upgrade script', 'Django system check', 'migration check')
        for reason in ('failed', 'unavailable or timed out')
    ),
})


def execute_netbox_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_url: str,
    authorization: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> UpgradeRecovery:
    """Run approved NetBox-only stages under the shared operation lock."""
    return _execute_upgrade(cfg, plan, approval, rebuild_plan=rebuild_plan,
                            health_url=health_url, authorization=authorization, progress=progress,
                            operation='netbox')


def execute_plugin_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_url: str,
    authorization: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> UpgradeRecovery:
    """Upgrade only selected plugin packages, without checking out NetBox."""
    return _execute_upgrade(cfg, plan, approval, rebuild_plan=rebuild_plan,
                            health_url=health_url, authorization=authorization, progress=progress,
                            operation='plugins')


def execute_combined_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_url: str,
    authorization: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> UpgradeRecovery:
    """Execute NetBox and selected plugin stages in one maintenance window."""
    return _execute_upgrade(cfg, plan, approval, rebuild_plan=rebuild_plan,
                            health_url=health_url, authorization=authorization, progress=progress,
                            operation='combined')


@upgrade_signals()
def _execute_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_url: str, authorization: str | None, operation: str,
    progress: Callable[[str, int, int], None] | None = None,
) -> UpgradeRecovery:
    """Share approval, maintenance, backup, health and failure handling."""
    def report(phase: str, done: int = 0) -> None:
        if progress is not None:
            try:
                progress(phase, done, len(plan.stages))
            except Exception:
                pass  # A display failure must not interrupt maintenance.

    check_cancelled()
    report('preflight')
    if os.geteuid() != 0:
        raise UpgradeExecutionError('Root privileges required for upgrade')
    with ExclusiveLock(), tempfile.TemporaryDirectory(prefix='netbox-ops-upgrade-') as wheel_root:
        fresh = rebuild_plan()
        validate_execution_approval(plan, approval, fresh_plan=fresh, operation=operation)
        live = plan.source.installation
        # All executors use the currently selected interpreter.
        # An explicitly different runtime requires a separately approved workflow.
        if any(version != live.python_version for _, version in plan.source.python_by_stage):
            raise UpgradeExecutionError('Changing the selected Python runtime is not supported')
        prepared = None
        if operation == 'netbox':
            validate_netbox_only_pins(plan)
        else:
            if inventory_plugins(live) != plan.source.inventory:
                raise UpgradeExecutionError('Plugin inventory changed before dependency preflight')
            if operation == 'plugins':
                prepared = preflight_plugin_upgrade(plan, approval)
        base_python = resolve_base_python(live)
        validate_upgrade_runtime(cfg, live, base_python, plan.source.inventory.enabled)
        _private_directory(cfg.backup_directory)
        baseline = read_netbox_health(health_url, authorization=authorization)
        if baseline.version != plan.current_netbox:
            raise UpgradeExecutionError('HTTP status does not match the installed NetBox release')
        stages = (pin_release_stages(plan) if operation != 'plugins' else
                  (PinnedStage(live.version, live.commit),))
        combined = (prepare_combined_stages(cfg, plan, approval, stages, wheel_root=Path(wheel_root))
                    if operation == 'combined' else ())
        systemd = SystemdServices(cfg)
        recovery = None
        phase = 'stop writers'
        completed = 0
        check_cancelled()
        write_audit(f'NetBox {operation} upgrade maintenance requested')
        begin_maintenance()
        try:
            check_cancelled()
            report('stop')
            stop_upgrade_writers(systemd)
            check_cancelled()
            phase = 'capture recovery'
            report('backup')
            recovery = _capture_quiesced_recovery(cfg, plan, systemd=systemd, pinned_stages=stages)
            write_audit(f'NetBox upgrade recovery={recovery.directory.name}')
            check_cancelled()
            expected_health = baseline
            if operation == 'plugins':
                phase = 'plugin installation and migration'
                report('stage', completed)
                if prepared is None:
                    raise UpgradeExecutionError('Plugin preflight was not completed')
                loaded = run_plugin_stage(live, plan, prepared, base_python, systemd=systemd)
                if {name for name, _ in loaded} != {name for name, _ in baseline.plugins}:
                    raise UpgradeExecutionError('Configured plugin registry changed during installation')
                expected_health = replace(baseline, plugins=loaded)
                completed = 1
                report('validated', completed)
                write_audit('NetBox plugin stage completed')
            for index, (stage, planned) in enumerate(zip(stages, plan.stages, strict=True), 1):
                if operation == 'plugins':
                    break
                check_cancelled()
                report('stage', completed)
                if operation == 'combined':
                    phase = f'stage {index} preflight'
                    require_quiesced_writers(systemd)
                    validate_upgrade_runtime(cfg, live, base_python, plan.source.inventory.enabled)
                    recheck_combined_stage(
                        cfg, live, planned, combined[index - 1], approval,
                        approved_warnings=plan.warnings,
                    )
                phase = f'stage {index} fetch'
                require_quiesced_writers(systemd)
                write_audit(f'NetBox upgrade starting stage={index} version={stage.version} commit={stage.commit}')
                fetch_pinned_stage(live.root, stage)
                phase = f'stage {index} checkout and upgrade script'
                if operation == 'combined':
                    live, loaded = run_combined_stage(
                        live, plan, combined[index - 1], base_python, systemd=systemd,
                    )
                    if {name for name, _ in loaded} != {name for name, _ in baseline.plugins}:
                        raise UpgradeExecutionError('Configured plugin registry changed during combined upgrade')
                    if index < len(stages) and loaded != baseline.plugins:
                        raise UpgradeExecutionError('Plugin registry changed before the final stage')
                    expected_health = replace(baseline, plugins=loaded)
                    completed = index
                    report('validated', completed)
                    write_audit(f'NetBox upgrade completed stage={index} version={stage.version} commit={stage.commit}')
                    continue
                live = run_stage(live, stage, systemd=systemd)
                phase = f'stage {index} plugin validation'
                actual = inventory_plugins(live)
                if actual.requirements_hash != plan.source.inventory.requirements_hash:
                    raise UpgradeExecutionError('Plugin requirements changed during the stage')
                if actual.enabled != plan.source.inventory.enabled:
                    raise UpgradeExecutionError('Configured plugins changed during the stage')
                installed = {normalize_name(p.name): p.version for p in actual.packages}
                if any(installed.get(normalize_name(name)) != version
                       for name, version in planned.plugin_versions):
                    raise UpgradeExecutionError('An approved plugin version changed during the stage')
                completed = index
                report('validated', completed)
                write_audit(f'NetBox upgrade completed stage={index} version={stage.version} commit={stage.commit}')
            check_cancelled()
            phase = 'start writers'
            report('start', completed)
            try:
                start_upgrade_writers(systemd)
                check_cancelled()
                phase = 'functional health check'
                report('health', completed)
                wait_for_netbox_health(
                    health_url, plan.target_netbox, expected_health, authorization=authorization,
                )
                check_cancelled()
            except (Exception, KeyboardInterrupt):
                # Try both writers independently: one failed unit must not
                # prevent an attempt to stop the other.
                phase += '; maintenance unverified'
                for name in ('netbox-rq', 'netbox'):
                    try:
                        systemd.stop([name])
                    except Exception:
                        pass
                require_quiesced_writers(systemd)
                phase = phase.replace('maintenance unverified', 'writers stopped')
                raise
            phase = 'finish maintenance'
            write_audit('NetBox upgrade completed')
            finish_maintenance()
            report('complete', completed)
            return recovery
        except (Exception, KeyboardInterrupt) as exc:
            # Do not include command output or arbitrary exception text.
            # A partial stop/start may require manual service inspection.
            location = recovery.directory.name if recovery else 'not verified'
            try:
                write_audit(f'NetBox upgrade failed phase={phase} completed={completed} recovery={location}')
            except Exception:
                pass
            report('failed', completed)
            last = stages[completed - 1].version if completed else 'none'
            detail = str(exc) if isinstance(exc, UpgradeExecutionError) and str(exc) in _SAFE_STAGE_ERRORS else ''
            if isinstance(exc, (UpgradeInterrupted, KeyboardInterrupt)):
                detail = 'Upgrade interruption requested'
            elif isinstance(exc, ManagedProcessError):
                detail = 'Upgrade command left active descendants; managed group stopped'
            diagnostic = f'detail: {detail}; ' if detail else ''
            raise UpgradeExecutionError(
                f'Upgrade failed during {phase}; recovery: {location}; '
                f'completed stages: {completed}/{len(stages)}; last completed release: {last}; '
                f'{diagnostic}inspect service state and recovery.json before manual recovery; no automatic rollback'
            ) from None
