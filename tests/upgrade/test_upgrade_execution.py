"""Unit tests for the NetBox upgrade execution approval gate."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
)
from netbox_ops.upgrade_plan import UpgradeStage


class ExecutionApprovalTests(unittest.TestCase):
    def setUp(self):
        self.install = GitNetBoxInstallation(
            Path('/opt/netbox'), '4.4.0', 'a' * 40, 'v4.4.0',
            'HEAD', 'https://github.com/netbox-community/netbox.git',
            Path('/opt/netbox/venv/bin/python'), '3.12.0', (), (),
        )
        source = Mock(installation=self.install, available_tags=('v4.4.1',))
        stage = UpgradeStage('netbox', '4.4.0', '4.4.1', (), (), (), (), (), ())
        self.plan = Mock(
            can_execute=True, stages=(stage,), selected_plugins=(),
            source=source, fingerprint='approved',
            requires_strong_confirmation=False, current_netbox='4.4.0',
            target_netbox='4.4.1',
        )
        self.approval = ExecutionApproval('approved', 'a' * 40, '/opt/netbox')
        self.inspector = Mock(return_value=self.install)

    def check_rejected(self, plan=None, approval=None):
        with self.assertRaises(UpgradeExecutionError):
            validate_execution_approval(
                plan or self.plan, approval or self.approval,
                inspector=self.inspector, fresh_plan=self.plan,
            )

    def test_valid_approval_is_read_only(self):
        validate_execution_approval(self.plan, self.approval, inspector=self.inspector,
                                    fresh_plan=self.plan)
        self.inspector.assert_called_once_with(Path('/opt/netbox'))

    def test_wrong_fingerprint_rejected_before_inspection(self):
        self.check_rejected(approval=replace(self.approval, fingerprint='stale'))
        self.inspector.assert_not_called()

    def test_wrong_commit_rejected(self):
        self.check_rejected(approval=replace(self.approval, git_commit='b' * 40))

    def test_live_commit_changed(self):
        self.inspector.return_value = replace(self.install, commit='b' * 40)
        self.check_rejected()

    def test_live_python_changed(self):
        self.inspector.return_value = replace(self.install, python_version='3.13.0')
        self.check_rejected()

    def test_live_local_paths_changed(self):
        self.inspector.return_value = replace(self.install, local_paths=('local_requirements.txt',))
        self.check_rejected()

    def test_blocked_plan_rejected(self):
        self.plan.can_execute = False
        self.check_rejected()

    def test_unknown_requires_strong_confirmation(self):
        self.plan.requires_strong_confirmation = True
        self.check_rejected()
        validate_execution_approval(
            self.plan, replace(self.approval, approved_unknowns=True),
            inspector=self.inspector, fresh_plan=self.plan,
        )

    def test_plugin_stage_rejected(self):
        self.plan.stages = (replace(self.plan.stages[0], kind='plugins'),)
        self.check_rejected()

    def test_plugin_selection_rejected(self):
        self.plan.selected_plugins = (('plugin', '1.0'),)
        self.check_rejected()

    def test_unverified_git_tag_rejected(self):
        self.plan.source.available_tags = ()
        self.check_rejected()

    def test_stage_sequence_rejected(self):
        self.plan.stages = (replace(self.plan.stages[0], previous_netbox='4.3.9'),)
        self.check_rejected()

    def test_final_target_must_match(self):
        self.plan.target_netbox = '4.4.2'
        self.check_rejected()

    def test_fresh_plan_is_mandatory(self):
        with self.assertRaisesRegex(UpgradeExecutionError, 'Fresh preflight'):
            validate_execution_approval(self.plan, self.approval,
                                        inspector=self.inspector)
        self.inspector.assert_not_called()

    def test_blocked_fresh_plan_rejected_before_inspection(self):
        with self.assertRaisesRegex(UpgradeExecutionError, 'changed during preflight'):
            validate_execution_approval(
                self.plan, self.approval, inspector=self.inspector,
                fresh_plan=Mock(fingerprint='approved', can_execute=False),
            )
        self.inspector.assert_not_called()

    def test_fresh_plan_mismatch_rejected(self):
        with self.assertRaises(UpgradeExecutionError):
            validate_execution_approval(self.plan, self.approval,
                                        inspector=self.inspector,
                                        fresh_plan=Mock(fingerprint='changed', can_execute=True))
        self.inspector.assert_not_called()

    def test_stage_blocker_rejected(self):
        self.plan.stages = (replace(self.plan.stages[0], blockers=('missing database',)),)
        self.check_rejected()


if __name__ == '__main__':
    unittest.main()
