"""Mocked official release lookup tests; no network or checkout changes."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_git import OFFICIAL_REMOTE, PinnedStage, pin_release_stages


class PinReleaseTests(unittest.TestCase):
    def setUp(self):
        self.plan = SimpleNamespace(can_execute=True, stages=[
            SimpleNamespace(kind='netbox', netbox_version='4.3.9'),
            SimpleNamespace(kind='netbox', netbox_version='4.4.1'),
        ])
        self.first = 'a' * 40 + '\trefs/tags/v4.3.9\n'
        self.second = 'b' * 40 + '\trefs/tags/v4.4.1\n'
        self.peeled = 'c' * 40 + '\trefs/tags/v4.4.1^{}\n'
        self.runner = Mock(return_value=SimpleNamespace(
            returncode=0, stdout=self.second + self.peeled + self.first,
        ))

    def pin(self):
        return pin_release_stages(self.plan, runner=self.runner)

    def test_pins_in_plan_order_and_peels_annotated_tag(self):
        self.assertEqual(self.pin(), (
            PinnedStage('4.3.9', 'a' * 40), PinnedStage('4.4.1', 'c' * 40),
        ))
        args = self.runner.call_args.args[0]
        self.assertIn(OFFICIAL_REMOTE, args)
        self.assertIn('refs/tags/v4.4.1^{}', args)
        self.assertEqual(self.runner.call_args.kwargs['cwd'], '/')

    def test_missing_tag(self):
        self.runner.return_value.stdout = self.first
        with self.assertRaises(UpgradeExecutionError):
            self.pin()

    def test_duplicate_tag(self):
        self.runner.return_value.stdout += self.first
        with self.assertRaises(UpgradeExecutionError):
            self.pin()

    def test_invalid_hash(self):
        self.runner.return_value.stdout = self.first.replace('a' * 40, 'bad') + self.second
        with self.assertRaises(UpgradeExecutionError):
            self.pin()

    def test_unexpected_ref(self):
        self.runner.return_value.stdout += 'd' * 40 + '\trefs/heads/main\n'
        with self.assertRaises(UpgradeExecutionError):
            self.pin()

    def test_invalid_version_never_runs_git(self):
        self.plan.stages[0].netbox_version = '--upload-pack=bad'
        with self.assertRaises(UpgradeExecutionError):
            self.pin()
        self.runner.assert_not_called()

    def test_timeout_redacts_command_details(self):
        self.runner.side_effect = subprocess.TimeoutExpired('secret', 60)
        with self.assertRaises(UpgradeExecutionError) as raised:
            self.pin()
        self.assertNotIn('secret', str(raised.exception))

    def test_nonzero_exit(self):
        self.runner.return_value.returncode = 2
        with self.assertRaises(UpgradeExecutionError):
            self.pin()
