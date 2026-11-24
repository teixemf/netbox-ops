"""Exercise maintenance transitions without touching systemd."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_services import (
    require_quiesced_writers,
    start_upgrade_writers,
    stop_upgrade_writers,
)


def states(active):
    return [SimpleNamespace(name=name, loaded='loaded', active=active)
            for name in ('netbox', 'netbox-rq')]


class UpgradeServiceTests(unittest.TestCase):
    def setUp(self):
        self.systemd = Mock()

    def test_stop_order(self):
        self.systemd.status.side_effect = [states('active'), states('inactive')]
        stop_upgrade_writers(self.systemd)
        self.systemd.stop.assert_called_once_with(['netbox-rq', 'netbox'])
        self.systemd.start.assert_not_called()

    def test_start_order(self):
        self.systemd.status.side_effect = [states('inactive'), states('active')]
        start_upgrade_writers(self.systemd)
        self.systemd.start.assert_called_once_with(['netbox', 'netbox-rq'])

    def test_unhealthy_initial_state_blocks_stop(self):
        self.systemd.status.return_value = states('failed')
        with self.assertRaises(UpgradeExecutionError):
            stop_upgrade_writers(self.systemd)
        self.systemd.stop.assert_not_called()

    def test_partial_stop_failure_never_restarts(self):
        self.systemd.status.return_value = states('active')
        self.systemd.stop.side_effect = RuntimeError('secret')
        with self.assertRaises(UpgradeExecutionError) as caught:
            stop_upgrade_writers(self.systemd)
        self.assertNotIn('secret', str(caught.exception))
        self.systemd.start.assert_not_called()

    def test_stop_success_without_quiescence_is_failure(self):
        self.systemd.status.return_value = states('active')
        with self.assertRaises(UpgradeExecutionError):
            stop_upgrade_writers(self.systemd)
        self.systemd.start.assert_not_called()

    def test_missing_or_duplicate_writer_blocks_stage(self):
        for result in (states('inactive')[:1], states('inactive')[:1] * 2):
            with self.subTest(result=result):
                self.systemd.status.return_value = result
                with self.assertRaises(UpgradeExecutionError):
                    require_quiesced_writers(self.systemd)

    def test_unloaded_writer_blocks_start(self):
        result = states('inactive')
        result[0].loaded = 'not-found'
        self.systemd.status.return_value = result
        with self.assertRaises(UpgradeExecutionError):
            start_upgrade_writers(self.systemd)
        self.systemd.start.assert_not_called()

    def test_start_failure_does_not_retry(self):
        self.systemd.status.return_value = states('inactive')
        self.systemd.start.side_effect = RuntimeError('secret')
        with self.assertRaises(UpgradeExecutionError) as caught:
            start_upgrade_writers(self.systemd)
        self.assertNotIn('secret', str(caught.exception))
        self.systemd.start.assert_called_once()

    def test_start_success_without_health_is_failure(self):
        self.systemd.status.return_value = states('inactive')
        with self.assertRaises(UpgradeExecutionError):
            start_upgrade_writers(self.systemd)
