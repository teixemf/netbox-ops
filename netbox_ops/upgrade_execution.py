"""Fail-closed approval checks before a Git NetBox upgrade.

This module does not execute a checkout. Execution must first prove that the
approved plan still describes the live installation and its dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .upgrade_discovery import inspect_git_installation
from .upgrade_plan import UpgradePlan


class UpgradeExecutionError(Exception):
    """The approved upgrade plan is stale or unsafe to execute."""


@dataclass(frozen=True)
class ExecutionApproval:
    fingerprint: str
    git_commit: str
    root: str
    approved_unknowns: bool = False


def validate_execution_approval(
    plan: UpgradePlan,
    approval: ExecutionApproval,
    *,
    inspector: Callable = inspect_git_installation,
    fresh_plan: UpgradePlan | None = None,
) -> None:
    """Reinspect the live Git checkout without changing any files.

    A fingerprint is not a substitute for rechecking the environment. The
    caller must rebuild the plan using fresh prerequisite, resolver and plugin
    inventory evidence before invoking this function.
    """
    # A freshly rebuilt plan is mandatory for any caller that intends to
    # execute an upgrade. Approval of an earlier plan is not reusable after
    # dependencies, requirements or upstream metadata change.
    if fresh_plan is None:
        raise UpgradeExecutionError('Fresh preflight evidence is required before execution')
    if fresh_plan.fingerprint != plan.fingerprint or not fresh_plan.can_execute:
        raise UpgradeExecutionError('Upgrade plan changed during preflight')
    if not plan.can_execute:
        raise UpgradeExecutionError('Upgrade plan has blockers or no stages')
    if not plan.stages or any(stage.blockers for stage in plan.stages):
        raise UpgradeExecutionError('Upgrade plan contains blocked stages')
    if not approval.fingerprint or approval.fingerprint != plan.fingerprint:
        raise UpgradeExecutionError('Approved plan fingerprint does not match')
    original = plan.source.installation
    if approval.root != str(original.root) or approval.git_commit != original.commit:
        raise UpgradeExecutionError('Approval does not match the planned installation')
    if plan.requires_strong_confirmation and not approval.approved_unknowns:
        raise UpgradeExecutionError('Explicit confirmation of unknown requirements is required')
    if any(stage.kind != 'netbox' for stage in plan.stages):
        raise UpgradeExecutionError('This executor supports NetBox-only stages')
    if plan.selected_plugins:
        raise UpgradeExecutionError('Plugin changes require the combined executor')
    live = inspector(original.root)
    if (live.root != original.root or live.commit != original.commit
            or live.version != original.version or live.python != original.python
            or live.python_version != original.python_version
            or live.local_paths != original.local_paths):
        raise UpgradeExecutionError('NetBox installation changed after planning')
    if live.warnings != original.warnings:
        raise UpgradeExecutionError('Git installation warnings changed after planning')
    previous = plan.current_netbox
    if plan.target_netbox == previous:
        raise UpgradeExecutionError('No NetBox version change was approved')
    for stage in plan.stages:
        if stage.previous_netbox != previous or stage.netbox_version == previous:
            raise UpgradeExecutionError('NetBox stages are out of order')
        if 'v' + stage.netbox_version not in plan.source.available_tags:
            raise UpgradeExecutionError('Unverified NetBox Git release tag')
        previous = stage.netbox_version
        if any(check.status == 'blocked' for check in stage.prerequisites):
            raise UpgradeExecutionError('A stage has incompatible prerequisites')
        if any(check.status == 'blocked' for check in stage.plugin_checks):
            raise UpgradeExecutionError('A stage has incompatible plugins')
    if previous != plan.target_netbox:
        raise UpgradeExecutionError('Final NetBox stage does not match the target')
