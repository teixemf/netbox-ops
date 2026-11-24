"""Service lifecycle checks for a locked NetBox upgrade.

Only NetBox writers are controlled. The caller owns the exclusive lock and
must retain recovery data if a stage fails. No automatic restart on failure.
"""
from .services import SystemdServices, order_services
from .upgrade_execution import UpgradeExecutionError

_WRITERS = ('netbox', 'netbox-rq')


def _require_state(systemd: SystemdServices, active: str) -> None:
    try:
        states = systemd.status(list(_WRITERS))
        if (len(states) != len(_WRITERS)
                or {state.name for state in states} != set(_WRITERS)
                or any(state.loaded != 'loaded' or state.active != active
                       for state in states)):
            raise UpgradeExecutionError('NetBox writers are not in the required state')
    except Exception:
        raise UpgradeExecutionError('Cannot verify NetBox writer state') from None


def stop_upgrade_writers(systemd: SystemdServices) -> None:
    """Require a healthy starting installation and stop writers in order."""
    _require_state(systemd, 'active')
    try:
        systemd.stop(order_services('stop', _WRITERS))
    except Exception:
        raise UpgradeExecutionError('Stopping NetBox writers failed; inspect service state') from None
    _require_state(systemd, 'inactive')


def require_quiesced_writers(systemd: SystemdServices) -> None:
    """Recheck both writers immediately before each mutating stage."""
    _require_state(systemd, 'inactive')


def start_upgrade_writers(systemd: SystemdServices) -> None:
    """Start writers only after successful migration and offline checks."""
    require_quiesced_writers(systemd)
    try:
        systemd.start(order_services('start', _WRITERS))
    except Exception:
        raise UpgradeExecutionError('Starting NetBox writers failed; inspect service state') from None
    _require_state(systemd, 'active')
