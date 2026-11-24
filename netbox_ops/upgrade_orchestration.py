"""Coordinate NetBox-only upgrades under the shared operation lock."""
from __future__ import annotations

import os
from typing import Callable

from .config import Configuration
from .services import ExclusiveLock, SystemdServices, write_audit
from .upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
    validate_netbox_only_pins,
)
from .upgrade_checkout import fetch_pinned_stage
from .upgrade_git import pin_release_stages
from .upgrade_health import read_netbox_health, wait_for_netbox_health
from .plugin_inventory import inventory_plugins, normalize_name
from .upgrade_maintenance import begin_maintenance, finish_maintenance
from .upgrade_plan import UpgradePlan
from .upgrade_recovery import UpgradeRecovery, _capture_quiesced_recovery, _private_directory
from .upgrade_runtime import validate_upgrade_runtime
from .upgrade_services import (
    require_quiesced_writers, start_upgrade_writers, stop_upgrade_writers,
)
from .upgrade_stage import resolve_base_python, run_stage


def execute_netbox_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_url: str,
    authorization: str | None = None,
) -> UpgradeRecovery:
    """Run approved stages using fresh evidence and the explicit NetBox status URL.

    Keep writers stopped across intermediate stages; validate Django and plugin
    packages at each stage, and verify live HTTP health after the final start.
    Never automatically roll back or restart after a failed stage.
    """
    if os.geteuid() != 0:
        raise UpgradeExecutionError('Root privileges required for upgrade')
    with ExclusiveLock():
        fresh = rebuild_plan()
        validate_execution_approval(plan, approval, fresh_plan=fresh)
        live = plan.source.installation
        # This NetBox-only executor uses the currently selected interpreter.
        # An explicitly different runtime requires a separately approved workflow.
        if any(version != live.python_version for _, version in plan.source.python_by_stage):
            raise UpgradeExecutionError('Changing the selected Python runtime is not supported')
        validate_netbox_only_pins(plan)
        base_python = resolve_base_python(live)
        validate_upgrade_runtime(cfg, live, base_python, plan.source.inventory.enabled)
        _private_directory(cfg.backup_directory)
        baseline = read_netbox_health(health_url, authorization=authorization)
        if baseline.version != plan.current_netbox:
            raise UpgradeExecutionError('HTTP status does not match the installed NetBox release')
        stages = pin_release_stages(plan)
        systemd = SystemdServices(cfg)
        recovery = None
        phase = 'stop writers'
        write_audit('NetBox upgrade maintenance requested')
        begin_maintenance()
        try:
            stop_upgrade_writers(systemd)
            phase = 'capture recovery'
            recovery = _capture_quiesced_recovery(cfg, plan, systemd=systemd, pinned_stages=stages)
            write_audit(f'NetBox upgrade recovery={recovery.directory.name}')
            for index, (stage, planned) in enumerate(zip(stages, plan.stages, strict=True), 1):
                phase = f'stage {index} fetch'
                require_quiesced_writers(systemd)
                write_audit(f'NetBox upgrade starting stage={index} version={stage.version} commit={stage.commit}')
                fetch_pinned_stage(live.root, stage)
                phase = f'stage {index} checkout and upgrade script'
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
                write_audit(f'NetBox upgrade completed stage={index} version={stage.version} commit={stage.commit}')
            phase = 'start writers'
            try:
                start_upgrade_writers(systemd)
                phase = 'functional health check'
                wait_for_netbox_health(
                    health_url, plan.target_netbox, baseline, authorization=authorization,
                )
            except Exception:
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
            return recovery
        except Exception:
            # Do not include command output or arbitrary exception text.
            # A partial stop/start may require manual service inspection.
            location = recovery.directory.name if recovery else 'not verified'
            try:
                write_audit(f'NetBox upgrade failed phase={phase} recovery={location}')
            except Exception:
                pass
            raise UpgradeExecutionError(
                f'Upgrade failed during {phase}; recovery: {location}; '
                'inspect service state before manual recovery'
            ) from None
