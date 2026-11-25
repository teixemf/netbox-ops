"""Read-only preparation and locked execution of selected plugin package upgrades."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .plugin_compatibility import PYPI_INDEX, isolated_dependency_preflight
from .plugin_inventory import inventory_plugins, normalize_name
from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .upgrade_execution import ExecutionApproval, UpgradeExecutionError
from .upgrade_process import run_managed
from .upgrade_plan import UpgradePlan
from .upgrade_recovery import _read_config
from .upgrade_runtime import upgrade_environment
from .upgrade_services import require_quiesced_writers
from .services import SystemdServices

# Only simple names, extras and exact versions are supported. Never interpret
# includes, URLs, editable installs, markers or pip options as package names.
_LINE = re.compile(
    r'([ \t]*)([A-Za-z0-9][A-Za-z0-9_.-]{0,127})'
    r'(\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?'
    r'(?:[ \t]*==[ \t]*([0-9][A-Za-z0-9.+_-]*))?([ \t]*(?:#.*)?)'
)
_STABLE = re.compile(r'(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,4}(?:\.post\d+)?')


@dataclass(frozen=True)
class PluginUpgrade:
    original_hash: str | None
    requirements: bytes
    core_pins: tuple[str, ...]
    local_pins: tuple[str, ...]
    expected: tuple[tuple[str, str], ...]
    selected: tuple[tuple[str, str], ...]
    wheelhouse: Path | None = None


def _parse(raw: bytes) -> tuple[list[str], dict[str, tuple[int, re.Match]]]:
    try:
        lines = raw.decode('utf-8').splitlines(keepends=True)
    except UnicodeError:
        raise UpgradeExecutionError('Requirements must be valid UTF-8') from None
    result = {}
    for index, line in enumerate(lines):
        body = line.rstrip('\r\n')
        if not body.strip() or body.lstrip().startswith('#'):
            continue
        match = _LINE.fullmatch(body)
        if match is None:
            raise UpgradeExecutionError('Review complex requirements before upgrading plugins')
        name = normalize_name(match[2])
        if name in result:
            raise UpgradeExecutionError('Duplicate package requirement needs manual review')
        result[name] = (index, match)
    return lines, result


def prepare_plugin_upgrade(plan: UpgradePlan, local: bytes | None,
                           core: bytes, *, stage_index: int | None = None) -> PluginUpgrade:
    """Preserve unselected requirements; prepare final pins only at the last stage."""
    inventory = plan.source.inventory
    digest = None if local is None else hashlib.sha256(local).hexdigest()
    if digest != inventory.requirements_hash or inventory.enabled is None:
        raise UpgradeExecutionError('Plugin requirements or configuration changed after planning')
    combined = stage_index is not None
    if not plan.can_execute:
        raise UpgradeExecutionError('Upgrade plan is blocked')
    if combined:
        if (not 0 <= stage_index < len(plan.stages)
                or plan.current_netbox == plan.target_netbox
                or any(s.kind != 'netbox' for s in plan.stages)):
            raise UpgradeExecutionError('Invalid combined upgrade stage')
    elif (len(plan.stages) != 1 or plan.stages[0].kind != 'plugins'
          or plan.current_netbox != plan.target_netbox):
        raise UpgradeExecutionError('Only one plugin-only stage is supported')
    installed = {}
    for package in inventory.packages:
        key = normalize_name(package.name)
        if key in installed:
            raise UpgradeExecutionError('Duplicate installed package metadata')
        installed[key] = package.version
    selection = dict(plan.selected_plugins)
    planned = {p.package: p for p in plan.plugins if p.selected}
    if (not combined and not selection) or len(selection) != len(plan.selected_plugins) or set(selection) != set(planned):
        raise UpgradeExecutionError('Invalid selected plugin packages')
    selected = {}
    for name, requested in selection.items():
        item = planned[name]
        version = item.proposed_version
        if (not isinstance(version, str) or not _STABLE.fullmatch(version)
                or installed.get(name) != item.installed_version or name not in installed
                or (requested is not None and requested != version)):
            raise UpgradeExecutionError('Selected plugin version is not verified')
        selected[name] = version
    if selected and all(installed[name] == version for name, version in selected.items()):
        raise UpgradeExecutionError('No plugin version change was approved')
    stage = plan.stages[stage_index if combined else 0]
    if combined and stage_index != len(plan.stages) - 1:
        selected = {}
    expected_stage = dict(stage.plugin_versions)
    if any(expected_stage.get(name) != version for name, version in selected.items()):
        raise UpgradeExecutionError('Selected pins differ from the approved stage')
    for check in stage.plugin_checks:
        if (check.status == 'blocked' or check.netbox_version != stage.netbox_version
                or check.python_version != plan.source.installation.python_version
                or expected_stage.get(normalize_name(check.package)) != check.version):
            raise UpgradeExecutionError('Plugin compatibility evidence does not match the approved stage')

    _, core_entries = _parse(core)
    if not core_entries or set(selection) & set(core_entries):
        raise UpgradeExecutionError('Cannot select NetBox core dependencies as plugins')
    core_pins = []
    expected = {}
    for name, (_, match) in core_entries.items():
        if not match[4] or (not combined and installed.get(name) != match[4]):
            raise UpgradeExecutionError('Installed NetBox core dependencies must match exact release pins')
        core_pins.append(f'{match[2]}{match[3] or ""}=={match[4]}')
        expected[name] = match[4]

    lines, entries = _parse(local or b'')
    if set(entries) & set(core_entries):
        raise UpgradeExecutionError('Local requirements overlap NetBox core dependencies')
    for module in inventory.enabled:
        top = module.split('.')[0]
        matches = [p for p in inventory.packages
                   if top in p.modules or (not p.modules and normalize_name(p.name) == normalize_name(top))]
        if len(matches) != 1:
            raise UpgradeExecutionError('Cannot map a configured plugin to an installed distribution')
        name = normalize_name(matches[0].name)
        if name not in selected and name not in entries:
            raise UpgradeExecutionError('Pin unselected configured plugins before upgrading')
    local_pins = []
    for name, (index, match) in entries.items():
        version = selected.get(name, match[4])
        if not version or (name not in selected and installed.get(name) != version):
            raise UpgradeExecutionError('Pin unselected local packages to their installed versions')
        if name in selected:
            ending = lines[index][len(lines[index].rstrip('\r\n')):]
            lines[index] = f'{match[1]}{match[2]}{match[3] or ""}=={version}{match[5]}{ending}'
        local_pins.append(f'{match[2]}{match[3] or ""}=={version}')
        expected[name] = version
    newline = '\r\n' if local and b'\r\n' in local else '\n'
    for name, version in selected.items():
        if name in entries:
            continue
        if lines and not lines[-1].endswith(('\n', '\r')):
            lines[-1] += newline
        pin = f'{name}=={version}'
        lines.append(pin + newline)
        local_pins.append(pin)
        expected[name] = version
    if any(expected.get(name) != version for name, version in expected_stage.items()):
        raise UpgradeExecutionError('Prepared pins differ from the approved plugin versions')
    return PluginUpgrade(digest, ''.join(lines).encode('utf-8'), tuple(core_pins),
                         tuple(local_pins), tuple(sorted(expected.items())),
                         tuple(sorted(selected.items())))


def bind_reviewed_dependencies(plan: UpgradePlan, index: int, commit: str,
                               core: bytes, pins: PluginUpgrade) -> PluginUpgrade:
    """Bind downloaded artifacts to the exact approved stage dependency report."""
    if not plan.dependency_reviews:
        return pins  # Internal callers still perform a full execution preflight.
    review = plan.dependency_reviews[index]
    if (review.version != plan.stages[index].netbox_version or review.commit != commit
            or review.core_hash != hashlib.sha256(core).hexdigest()
            or review.exact_pins != pins.expected):
        raise UpgradeExecutionError('Target requirements changed after plan approval')
    if review.resolution.status != 'passed' or not review.resolution.checked_core:
        raise UpgradeExecutionError('Refresh the plan with verified dependencies before executing')
    expected = dict(review.resolution.packages)
    if any(expected.get(name) != version for name, version in pins.expected):
        raise UpgradeExecutionError('Approved resolver report does not match exact requirements')
    return replace(pins, expected=tuple(sorted(expected.items())))


def verify_reviewed_resolution(plan: UpgradePlan, index: int, resolution) -> None:
    if plan.dependency_reviews:
        previous = plan.dependency_reviews[index].resolution
        if (resolution.status, resolution.checked_core, resolution.packages) != (
                previous.status, previous.checked_core, previous.packages):
            raise UpgradeExecutionError('Dependency resolution changed after plan approval')


def additional_dependency_pins(pins: PluginUpgrade) -> tuple[str, ...]:
    explicit = {normalize_name(pin.split('==', 1)[0].split('[', 1)[0])
                for pin in (*pins.core_pins, *pins.local_pins)}
    return tuple(f'{name}=={version}' for name, version in pins.expected if name not in explicit)


def preflight_plugin_upgrade(plan: UpgradePlan, approval: ExecutionApproval,
                             *, resolver: Callable = isolated_dependency_preflight,
                             wheelhouse: Path | None = None,
                             download: Callable | None = None) -> PluginUpgrade:
    """Resolve all proposed local pins together with the installed core pins."""
    installation = plan.source.installation
    root = installation.root
    core = _read_config(root, 'requirements.txt')
    if core is None:
        raise UpgradeExecutionError('NetBox core requirements are missing')
    prepared = prepare_plugin_upgrade(plan, _read_config(root, 'local_requirements.txt'), core)
    prepared = bind_reviewed_dependencies(plan, 0, installation.commit, core, prepared)
    if wheelhouse is not None:
        if download is None:
            raise UpgradeExecutionError('Plugin wheel preparation is unavailable')
        download(prepared, installation, wheelhouse)
        prepared = replace(prepared, wheelhouse=wheelhouse)
    options = {'wheelhouse': wheelhouse} if wheelhouse is not None else {}
    result = resolver(prepared.local_pins, prepared.core_pins,
                      installation.python_version, interpreter=installation.python, **options)
    verify_reviewed_resolution(plan, 0, result)
    if wheelhouse is not None and (result.status != 'passed' or not result.checked_core):
        raise UpgradeExecutionError('Cannot verify prepared plugin wheels before maintenance')
    if result.status == 'blocked':
        raise UpgradeExecutionError('Python dependency resolution confirmed a conflict')
    if result.status != 'passed' or not result.checked_core:
        if not approval.approved_unknowns:
            raise UpgradeExecutionError('Explicit confirmation of unresolved dependencies is required')
    return prepared


def _apply_requirements(root: Path, prepared: PluginUpgrade) -> None:
    """Publish only the approved bytes under the caller's exclusive lock."""
    path = root / 'local_requirements.txt'
    current = _read_config(root, 'local_requirements.txt')
    digest = None if current is None else hashlib.sha256(current).hexdigest()
    if digest != prepared.original_hash:
        raise UpgradeExecutionError('Plugin requirements changed before installation')
    if current is not None:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise UpgradeExecutionError('Local requirements must be root-controlled')
    fd, temporary = tempfile.mkstemp(prefix='.netbox-ops-plugin-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(prepared.requirements)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _command(command: list[str], root: Path, env: dict[str, str], label: str,
             runner: Callable, timeout: int = 600, *, output: bool = False) -> str:
    try:
        result = runner(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise UpgradeExecutionError(f'Plugin {label} unavailable or timed out') from None
    if result.returncode:
        raise UpgradeExecutionError(f'Plugin {label} failed') from None
    return result.stdout if output else ''


def run_plugin_stage(
    installation: GitNetBoxInstallation, plan: UpgradePlan, prepared: PluginUpgrade,
    base_python: Path, *, systemd: SystemdServices, runner: Callable = run_managed,
    inspector: Callable = inspect_git_installation, inventory_reader: Callable = inventory_plugins,
) -> tuple[tuple[str, str | None], ...]:
    """Install exact pins in the existing venv, then migrate and collect static files.

    The caller holds the lock, has a verified recovery point, and stopped writers.
    No NetBox checkout, system package upgrade or rollback is performed.
    """
    root = installation.root
    require_quiesced_writers(systemd)
    live = inspector(root)
    if live != installation or inventory_reader(live) != plan.source.inventory:
        raise UpgradeExecutionError('NetBox installation or plugins changed before installation')
    _apply_requirements(root, prepared)
    require_quiesced_writers(systemd)
    env = upgrade_environment(base_python)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR='1')
    python = str(installation.python)
    pip = [python, '-I', '-m', 'pip', '--isolated']
    index = (['--no-index', '--find-links', str(prepared.wheelhouse)]
             if prepared.wheelhouse is not None else ['--index-url', PYPI_INDEX])
    _command([*pip, 'install', '--no-input', '--no-cache-dir', '--disable-pip-version-check',
              '--only-binary=:all:', *index,
              *prepared.core_pins, *prepared.local_pins, *additional_dependency_pins(prepared)],
             root, env, 'installation', runner, 3600)
    require_quiesced_writers(systemd)
    _command([*pip, 'check'], root, env, 'dependency check', runner)
    final = inspector(root)
    if (final.root, final.commit, final.version, final.python, final.python_version) != (
            root, installation.commit, installation.version, installation.python, installation.python_version):
        raise UpgradeExecutionError('NetBox runtime changed during plugin installation')
    inventory = inventory_reader(final)
    actual = {normalize_name(p.name): p.version for p in inventory.packages}
    if (len(actual) != len(inventory.packages)
            or inventory.enabled != plan.source.inventory.enabled
            or inventory.requirements_hash != hashlib.sha256(prepared.requirements).hexdigest()
            or any(actual.get(name) != version for name, version in prepared.expected)):
        raise UpgradeExecutionError('Installed plugin or core versions differ from approved pins')
    manage = [python, str(root / 'netbox/manage.py')]
    for label, args in (
        ('Django system check', ['check']),
        ('migration', ['migrate', '--no-input']),
        ('static collection', ['collectstatic', '--no-input']),
        ('migration check', ['migrate', '--check']),
    ):
        require_quiesced_writers(systemd)
        _command([*manage, *args], root, env, label, runner, 1800)
    return read_plugin_registry(final, env, systemd=systemd, runner=runner)


def read_plugin_registry(
    installation: GitNetBoxInstallation, env: dict[str, str], *,
    systemd: SystemdServices, runner: Callable = run_managed,
) -> tuple[tuple[str, str | None], ...]:
    """Read the local Django registry without restarting the writers."""
    root = installation.root
    python = str(installation.python)
    # The local Django registry provides app labels and runtime versions, which
    # are not necessarily identical to distribution names and versions.
    probe = (
        "import os,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','netbox.settings'); "
        "import django; django.setup(); "
        "from netbox.plugins.utils import get_installed_plugins; "
        "print(json.dumps(get_installed_plugins()))"
    )
    require_quiesced_writers(systemd)
    try:
        data = json.loads(_command([python, '-c', probe], root / 'netbox', env,
                                   'loading check', runner, output=True))
        if (not isinstance(data, dict)
                or any(not isinstance(k, str) or (v is not None and not isinstance(v, str))
                       for k, v in data.items())):
            raise ValueError
    except (ValueError, TypeError):
        raise UpgradeExecutionError('Cannot verify the configured plugin registry') from None
    return tuple(sorted(data.items()))
