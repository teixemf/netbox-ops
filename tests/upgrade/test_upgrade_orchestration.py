"""Exercise orchestration without touching systemd, Git or backups."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from netbox_ops import upgrade_orchestration as orchestration
from netbox_ops.upgrade_execution import UpgradeExecutionError


@pytest.fixture
def operations(monkeypatch):
    calls = Mock()
    names = (
        'validate_execution_approval', 'pin_release_stages', 'SystemdServices',
        'write_audit', 'stop_upgrade_writers', '_capture_quiesced_recovery',
        'require_quiesced_writers', 'run_stage', 'start_upgrade_writers',
    )
    for name in names:
        monkeypatch.setattr(orchestration, name, getattr(calls, name))
    monkeypatch.setattr(orchestration.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(orchestration, 'ExclusiveLock', nullcontext)
    calls.pin_release_stages.return_value = ('first', 'second')
    calls._capture_quiesced_recovery.return_value.directory = Path('/backup/pre-upgrade-test')
    return calls


def execute(calls):
    return orchestration.execute_netbox_upgrade(
        Mock(), Mock(), Mock(), rebuild_plan=calls.rebuild, health_check=calls.health,
    )


def test_success_orders_backup_stages_and_health(operations):
    result = execute(operations)
    assert result is operations._capture_quiesced_recovery.return_value
    names = [call[0] for call in operations.mock_calls]
    assert names.index('rebuild') < names.index('validate_execution_approval')
    assert names.index('pin_release_stages') < names.index('stop_upgrade_writers')
    assert names.index('stop_upgrade_writers') < names.index('_capture_quiesced_recovery')
    assert names.index('_capture_quiesced_recovery') < names.index('run_stage')
    assert operations.run_stage.call_count == 2
    assert operations.run_stage.call_args.args[0] is operations.run_stage.return_value
    assert names.index('run_stage') < names.index('start_upgrade_writers') < names.index('health')


@pytest.mark.parametrize('failure', [
    'validate_execution_approval', 'pin_release_stages', 'stop_upgrade_writers',
    '_capture_quiesced_recovery', 'run_stage',
])
def test_failure_never_starts_writers(operations, failure):
    getattr(operations, failure).side_effect = UpgradeExecutionError('failure')
    with pytest.raises(UpgradeExecutionError):
        execute(operations)
    operations.start_upgrade_writers.assert_not_called()
    operations.health.assert_not_called()
    if failure != 'run_stage':
        operations.run_stage.assert_not_called()
    else:
        assert operations.run_stage.call_count == 1


def test_health_failure_reports_recovery_without_secrets(operations):
    operations.health.side_effect = RuntimeError('secret password')
    with pytest.raises(UpgradeExecutionError, match='functional health check') as error:
        execute(operations)
    assert 'pre-upgrade-test' in str(error.value)
    assert 'secret' not in str(error.value)
    operations.start_upgrade_writers.assert_called_once()
