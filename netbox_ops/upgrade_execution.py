"""Fail-closed approval checks before a Git NetBox upgrade.

This module does not execute a checkout. Execution must first prove that the
approved plan still describes the live installation and its dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .plugin_inventory import normalize_name
from .upgrade_discovery import inspect_git_installation
from .upgrade_plan import UpgradePlan, required_netbox_path


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
    operation: str = 'netbox',
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
    for version in (plan.current_netbox, plan.target_netbox, plan.source.installation.version,
                    *(stage.netbox_version for stage in plan.stages)):
        if not isinstance(version, str) or not version.split('.')[0].isdigit() or int(version.split('.')[0]) < 4:
            raise UpgradeExecutionError('NetBox 4.0 or newer is required')
    original = plan.source.installation
    if approval.root != str(original.root) or approval.git_commit != original.commit:
        raise UpgradeExecutionError('Approval does not match the planned installation')
    if plan.requires_strong_confirmation and not approval.approved_unknowns:
        raise UpgradeExecutionError('Explicit confirmation of unknown requirements is required')
    if operation == 'netbox':
        if any(stage.kind != 'netbox' for stage in plan.stages):
            raise UpgradeExecutionError('This executor supports NetBox-only stages')
        if plan.selected_plugins:
            raise UpgradeExecutionError('Plugin changes require the combined executor')
    elif operation == 'plugins':
        if (len(plan.stages) != 1 or plan.stages[0].kind != 'plugins'
                or not plan.selected_plugins or plan.current_netbox != plan.target_netbox
                or plan.stages[0].netbox_version != plan.current_netbox
                or plan.stages[0].previous_netbox != plan.current_netbox):
            raise UpgradeExecutionError('A plugin-only plan must preserve the NetBox version')
        if fresh_plan.source.inventory != plan.source.inventory:
            raise UpgradeExecutionError('Plugin inventory changed during preflight')
    elif operation == 'combined':
        if (not plan.selected_plugins or plan.current_netbox == plan.target_netbox
                or any(stage.kind != 'netbox' for stage in plan.stages)):
            raise UpgradeExecutionError('Combined plans require NetBox stages and selected plugins')
        try:
            required = required_netbox_path(plan.current_netbox, plan.target_netbox,
                                            plan.source.available_tags)
        except Exception:
            raise UpgradeExecutionError('Cannot verify required combined upgrade path') from None
        if tuple(stage.netbox_version for stage in plan.stages) != required:
            raise UpgradeExecutionError('Combined plan omits or changes a required release stage')
        if fresh_plan.source.inventory != plan.source.inventory:
            raise UpgradeExecutionError('Plugin inventory changed during preflight')
    else:
        raise UpgradeExecutionError('Unsupported upgrade operation')
    live = inspector(original.root)
    if (live.root != original.root or live.commit != original.commit
            or live.version != original.version or live.python != original.python
            or live.python_version != original.python_version
            or live.local_paths != original.local_paths):
        raise UpgradeExecutionError('NetBox installation changed after planning')
    if live.warnings != original.warnings:
        raise UpgradeExecutionError('Git installation warnings changed after planning')
    previous = plan.current_netbox
    if operation == 'netbox' and plan.target_netbox == previous:
        raise UpgradeExecutionError('No NetBox version change was approved')
    for stage in plan.stages:
        if stage.previous_netbox != previous or (operation != 'plugins' and stage.netbox_version == previous):
            raise UpgradeExecutionError('NetBox stages are out of order')
        if operation != 'plugins' and 'v' + stage.netbox_version not in plan.source.available_tags:
            raise UpgradeExecutionError('Unverified NetBox Git release tag')
        previous = stage.netbox_version
        if any(check.status == 'blocked' for check in stage.prerequisites):
            raise UpgradeExecutionError('A stage has incompatible prerequisites')
        if any(check.status == 'blocked' for check in stage.plugin_checks):
            raise UpgradeExecutionError('A stage has incompatible plugins')
    if previous != plan.target_netbox:
        raise UpgradeExecutionError('Final NetBox stage does not match the target')


def validate_netbox_only_pins(plan: UpgradePlan) -> None:
    """Do not let the upstream venv rebuild silently update configured plugins."""
    inventory = plan.source.inventory
    if inventory.enabled is None:
        raise UpgradeExecutionError('Configured plugins must be identifiable before upgrading')
    for module in inventory.enabled:
        top_level = module.split('.')[0]
        matches = [p for p in inventory.packages
                   if top_level in p.modules or
                   (not p.modules and normalize_name(top_level) == normalize_name(p.name))]
        if len(matches) != 1:
            raise UpgradeExecutionError('A configured plugin cannot be mapped to an installed package')
        package = matches[0]
        name = normalize_name(package.name)
        requirements = [item for item in inventory.requirements
                        if normalize_name(item.name) == name]
        if (len(requirements) != 1 or requirements[0].kind != 'pinned'
                or requirements[0].version != package.version
                or any(dict(stage.plugin_versions).get(name) != package.version
                       for stage in plan.stages)):
            raise UpgradeExecutionError('Pin configured plugins to their installed versions before upgrading')
