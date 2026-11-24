"""Coordinate NetBox-only upgrades under the shared operation lock."""
from __future__ import annotations

import os
from typing import Callable

from .config import Configuration
from .services import ExclusiveLock, SystemdServices, write_audit
from .upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
)
from .upgrade_git import pin_release_stages
from .upgrade_plan import UpgradePlan
from .upgrade_recovery import UpgradeRecovery, _capture_quiesced_recovery
from .upgrade_services import (
    require_quiesced_writers, start_upgrade_writers, stop_upgrade_writers,
)
from .upgrade_stage import run_stage


def execute_netbox_upgrade(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval, *,
    rebuild_plan: Callable[[], UpgradePlan],
    health_check: Callable[[], None],
) -> UpgradeRecovery:
    """Run approved stages; health_check must raise on unhealthy NetBox.

    The caller supplies fresh preflight and functional health checks.
    Never automatically roll back or restart after a failed stage.
    """
    if os.geteuid() != 0:
        raise UpgradeExecutionError('Root privileges required for upgrade')
    with ExclusiveLock():
        fresh = rebuild_plan()
        validate_execution_approval(plan, approval, fresh_plan=fresh)
        stages = pin_release_stages(plan)
        systemd = SystemdServices(cfg)
        recovery = None
        phase = 'stop writers'
        write_audit('NetBox upgrade maintenance requested')
        try:
            stop_upgrade_writers(systemd)
            phase = 'capture recovery'
            recovery = _capture_quiesced_recovery(cfg, plan, systemd=systemd)
            write_audit(f'NetBox upgrade recovery={recovery.directory.name}')
            live = plan.source.installation
            for index, stage in enumerate(stages, 1):
                phase = f'stage {index}'
                require_quiesced_writers(systemd)
                write_audit(f'NetBox upgrade starting {phase}')
                live = run_stage(live, stage, systemd=systemd)
                write_audit(f'NetBox upgrade completed {phase}')
            phase = 'start writers'
            start_upgrade_writers(systemd)
            phase = 'functional health check'
            try:
                health_check()
            except Exception:
                # Stop both writers even if one has already become unhealthy.
                phase = 'functional health check; maintenance unverified'
                systemd.stop(['netbox-rq', 'netbox'])
                require_quiesced_writers(systemd)
                phase = 'functional health check; writers stopped'
                raise
            write_audit('NetBox upgrade completed')
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
