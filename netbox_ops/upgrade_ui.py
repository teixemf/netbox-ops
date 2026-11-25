"""Shared upgrade controller for the CLI and the terminal interface."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from .config import Configuration
from .i18n import Translator
from .plugin_inventory import PluginInventory, inventory_plugins, normalize_name
from .upgrade_combined import probe_server_versions
from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .upgrade_execution import ExecutionApproval, UpgradeExecutionError
from .upgrade_git import OFFICIAL_REMOTE
from .upgrade_health import _validate_url
from .upgrade_orchestration import (
    execute_combined_upgrade, execute_netbox_upgrade, execute_plugin_upgrade,
)
from .upgrade_plan import UpgradePlan, _configured_packages, build_upgrade_plan
from .upgrade_preflight import review_plan_dependencies

_TAG = re.compile(r'[0-9a-f]{40}\trefs/tags/(v\d+\.\d+\.\d+)')


def official_tags(*, runner: Callable = subprocess.run) -> tuple[str, ...]:
    """Discover stable releases without fetching or modifying the checkout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    try:
        response = runner(
            ['git', '-C', '/', 'ls-remote', '--tags', '--refs', '--', OFFICIAL_REMOTE],
            cwd='/', env=env, capture_output=True, text=True, check=False, timeout=60,
        )
        if response.returncode:
            raise ValueError
        tags = {match[1] for line in response.stdout.splitlines()
                if (match := _TAG.fullmatch(line)) and int(match[1][1:].split('.')[0]) >= 4}
        if not tags:
            raise ValueError
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise UpgradeExecutionError('Official NetBox release discovery failed') from None
    return tuple(sorted(tags, key=lambda tag: tuple(map(int, tag[1:].split('.'))), reverse=True))


@dataclass(frozen=True)
class UpgradeContext:
    installation: GitNetBoxInstallation
    inventory: PluginInventory
    tags: tuple[str, ...]
    postgres: str | None
    redis: str | None
    cfg: Configuration | None = None

    @property
    def plugins(self) -> tuple[tuple[str, str | None], ...]:
        """Only known configured or explicitly declared packages are offered."""
        known = {normalize_name(p.name): p.version for p in self.inventory.packages}
        names = set(dict(_configured_packages(self.inventory)))
        names.update(normalize_name(p.name) for p in self.inventory.requirements)
        return tuple(sorted((name, known[name]) for name in names if name in known))

    @property
    def targets(self) -> tuple[str, ...]:
        current = tuple(map(int, self.installation.version.split('.')))
        return tuple(tag[1:] for tag in self.tags
                     if tuple(map(int, tag[1:].split('.'))) > current)


def discover_context(cfg: Configuration, *, releases: bool = True) -> UpgradeContext:
    installation = inspect_git_installation()
    inventory = inventory_plugins(installation)
    try:
        postgres, redis = probe_server_versions(cfg, installation)
    except UpgradeExecutionError:
        postgres = redis = None
    return UpgradeContext(installation, inventory, official_tags() if releases else (),
                          postgres, redis, cfg)


def parse_plugins(values: list[str], version: str | None = None) -> dict[str, str | None]:
    selected = {}
    if version is not None and len(values) != 1:
        raise UpgradeExecutionError('A separate version requires exactly one plugin')
    for value in values:
        name, separator, pin = value.partition('==')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            raise UpgradeExecutionError('Invalid plugin selection')
        name = normalize_name(name)
        if name in selected or (separator and (version is not None or not pin)):
            raise UpgradeExecutionError('Duplicate or invalid plugin version selection')
        selected[name] = pin if separator else version
    return selected


def make_plan(context: UpgradeContext, operation: str, target: str | None,
              selected: dict[str, str | None]) -> UpgradePlan:
    if operation not in ('netbox', 'plugins', 'stack'):
        raise UpgradeExecutionError('Unknown upgrade operation')
    if operation != 'plugins' and not target:
        raise UpgradeExecutionError('Select a target NetBox version')
    if operation != 'netbox' and not selected:
        raise UpgradeExecutionError('Select at least one plugin')
    if (operation == 'netbox' and selected) or (operation == 'plugins' and target):
        raise UpgradeExecutionError('Selection does not match the upgrade operation')
    if operation != 'plugins' and target is not None and target.removeprefix('v') == context.installation.version:
        raise UpgradeExecutionError('Select a newer NetBox release')
    if set(selected) - set(dict(context.plugins)):
        raise UpgradeExecutionError('Select only installed configured or declared plugins')
    plan = build_upgrade_plan(
        context.installation, context.inventory, target_netbox=target,
        selected_plugins=selected, available_tags=context.tags,
        postgres_version=context.postgres, redis_version=context.redis,
    )
    return review_plan_dependencies(plan, context.cfg)


def summary_lines(plan: UpgradePlan, t: Translator, *, details: bool = False) -> list[str]:
    lines = [
        t('upgrade_versions', current=plan.current_netbox, target=plan.target_netbox),
        t('upgrade_plan_status', status=t('upgrade_status_' + plan.status)),
    ]
    for plugin in plan.plugins:
        if plugin.selected:
            lines.append(f'{plugin.package}: {plugin.installed_version} -> {plugin.proposed_version or "?"}')
        elif details:
            lines.append(f'{plugin.package}: {plugin.installed_version} ({t("upgrade_unchanged")})')
    for index, stage in enumerate(plan.stages, 1):
        lines.append(t('upgrade_stage', index=index, total=len(plan.stages),
                       version=stage.netbox_version))
        if details:
            lines.extend(f'  {item.name}: {item.installed or "?"} / {item.required} '
                         f'[{item.status}]' for item in stage.prerequisites)
            for check in stage.plugin_checks:
                lines.append(f'  {check.package}=={check.version}: {check.status}')
                lines.extend(f'    {e.source}: {e.detail}' for e in check.evidence)
            lines.extend('  ' + instruction for instruction in stage.instructions)
    for review in plan.dependency_reviews:
        lines.append(t('upgrade_dependency_status', version=review.version,
                       status=t('upgrade_dependency_' + review.resolution.status)))
        if review.ltree != 'not-required':
            lines.append(t('upgrade_ltree_status', status=t('upgrade_ltree_' + review.ltree)))
        for name, before, after in review.changes:
            lines.append(f'  {name}: {before or "-"} -> {after}')
        if details:
            lines.append('  ' + review.resolution.detail)
            lines.extend(f'  {name}=={version}' for name, version in review.exact_pins)
    lines.extend(t('upgrade_warning', message=warning) for warning in plan.warnings)
    lines.extend(t('upgrade_blocker', message=blocker) for blocker in plan.blockers)
    if details:
        lines.extend(plan.release_notes)
        lines.extend(plan.safeguards)
    lines.append(t('upgrade_fingerprint', fingerprint=plan.fingerprint))
    # External metadata and error details must never inject terminal controls.
    return [''.join(c if c.isprintable() else ' ' for c in line) for line in lines]


def execute_plan(cfg: Configuration, operation: str, plan: UpgradePlan, *,
                 fingerprint: str, acknowledge_unknowns: bool, health_url: str,
                 authorization: str | None = None,
                 progress: Callable[[str, int, int], None] | None = None):
    """Never approve a rebuilt or edited plan implicitly."""
    if not plan.can_execute or fingerprint != plan.fingerprint:
        raise UpgradeExecutionError('Approved fingerprint does not match an executable plan')
    if plan.requires_strong_confirmation and not acknowledge_unknowns:
        raise UpgradeExecutionError('Explicit confirmation of uncertain compatibility is required')
    _validate_url(health_url)
    target = None if operation == 'plugins' else plan.target_netbox
    selection = dict(plan.selected_plugins)

    def rebuild() -> UpgradePlan:
        context = discover_context(cfg, releases=operation != 'plugins')
        return make_plan(context, operation, target, selection)

    executor = {'netbox': execute_netbox_upgrade, 'plugins': execute_plugin_upgrade,
                'stack': execute_combined_upgrade}[operation]
    approval = ExecutionApproval(fingerprint, plan.source.installation.commit,
                                 str(plan.source.installation.root), acknowledge_unknowns)
    return executor(cfg, plan, approval, rebuild_plan=rebuild, health_url=health_url,
                    authorization=authorization, progress=progress)


def run_cli(args, cfg: Configuration, t: Translator) -> int:
    """Read-only by default; execution requires the exact displayed fingerprint."""
    import getpass
    import sys
    try:
        context = discover_context(cfg, releases=args.action != 'plugins')
        if args.action == 'inspect':
            print(t('upgrade_versions', current=context.installation.version,
                    target=context.targets[0] if context.targets else '-'))
            print(f'Python: {context.installation.python_version}')
            print(f'PostgreSQL: {context.postgres or "?"}; Redis: {context.redis or "?"}')
            for name, version in context.plugins:
                print(f'{name}: {version}')
            for target in context.targets:
                print(target)
            return 0
        selected = parse_plugins(getattr(args, 'plugin', None) or [], getattr(args, 'version', None)
                                 if args.action == 'plugins' else None)
        if getattr(args, 'all', False) or getattr(args, 'plugins', None) == 'all':
            selected = dict.fromkeys(name for name, _ in context.plugins)
        target = (args.version if args.action == 'netbox' else
                  args.netbox_version if args.action == 'stack' else None)
        if args.action != 'plugins' and target is None:
            target = context.targets[0] if context.targets else None
        plan = make_plan(context, args.action, target, selected)
        for line in summary_lines(plan, t, details=args.details):
            print(line)
        if not args.execute:
            return 0 if plan.can_execute else 1
        if not args.approve_plan or not args.health_url:
            raise UpgradeExecutionError('Execution requires --approve-plan and --health-url')
        if not plan.can_execute or args.approve_plan != plan.fingerprint:
            raise UpgradeExecutionError('Approved fingerprint does not match an executable plan')
        if plan.requires_strong_confirmation and not args.accept_unknowns:
            raise UpgradeExecutionError('Explicit confirmation of uncertain compatibility is required')
        authorization = None
        if args.prompt_authorization:
            if not sys.stdin.isatty():
                raise UpgradeExecutionError('Authorization prompt requires an interactive terminal')
            authorization = getpass.getpass(t('upgrade_auth_prompt')) or None
        recovery = execute_plan(
            cfg, args.action, plan, fingerprint=args.approve_plan,
            acknowledge_unknowns=args.accept_unknowns, health_url=args.health_url,
            authorization=authorization,
            progress=lambda phase, done, total: print(
                t('upgrade_progress', phase=t('upgrade_phase_' + phase),
                  done=done, total=total), flush=True),
        )
        print(t('upgrade_success', recovery=recovery.directory))
        return 0
    except UpgradeExecutionError as exc:
        print(t('upgrade_error', message=str(exc)), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print(t('upgrade_cancelled'), file=sys.stderr)
    except Exception:
        print(t('upgrade_discovery_error'), file=sys.stderr)
    return 1
