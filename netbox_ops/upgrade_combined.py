"""Combined upgrade stage preparation using the existing Git and plugin helpers."""
from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import re
import secrets
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Configuration
from .plugin_compatibility import (
    DependencyPreflight, PYPI_INDEX, isolated_dependency_preflight, review_plugin_upgrade,
)
from .plugin_inventory import configured_plugin_packages, inventory_plugins, normalize_name
from .plugin_upgrade import (
    PluginUpgrade, _apply_requirements, _command, prepare_plugin_upgrade, read_plugin_registry,
    additional_dependency_pins, bind_reviewed_dependencies, verify_reviewed_resolution,
)
from .restore_environment import netbox_runtime
from .services import SystemdServices
from .upgrade_checkout import _run_git, fetch_pinned_stage
from .upgrade_discovery import GitNetBoxInstallation, check_prerequisites
from .upgrade_execution import ExecutionApproval, UpgradeExecutionError
from .upgrade_diagnostics import CommandFailure, run_checked
from .upgrade_process import run_managed
from .upgrade_git import PinnedStage
from .upgrade_plan import UpgradePlan, UpgradeStage
from .upgrade_recovery import _read_config
from .upgrade_runtime import upgrade_environment, probe_ltree
from .upgrade_services import require_quiesced_writers
from .upgrade_stage import run_stage

_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')
_SERVER_PROBE = """
import json
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
from django.db import connections
from django_rq import get_connection
from django_redis import get_redis_connection
with connections['default'].cursor() as cursor:
    cursor.execute('SHOW server_version')
    pg = cursor.fetchone()[0]
print(json.dumps([pg, get_connection('default').info('server')['redis_version'],
                  get_redis_connection('default').info('server')['redis_version']]))
"""


@dataclass(frozen=True)
class CombinedStage:
    pinned: PinnedStage
    core: bytes
    pins: PluginUpgrade
    resolution: DependencyPreflight
    wheelhouse: Path


def probe_server_versions(
    cfg: Configuration, live: GitNetBoxInstallation, *,
    runner: Callable = subprocess.run, discover: Callable = netbox_runtime,
) -> tuple[str, str]:
    """Read real database and both Redis server versions as the NetBox user."""
    try:
        runtime = discover(cfg, runner=runner)
        if runtime.python != live.python or runtime.working_directory != live.root / 'netbox':
            raise ValueError
        account = (pwd.getpwuid(int(runtime.username)) if runtime.username.isdigit()
                   else pwd.getpwnam(runtime.username))
        if account.pw_uid == 0:
            raise ValueError
        response = runner(
            [str(runtime.python), '-c', _SERVER_PROBE],
            cwd=runtime.working_directory, env=runtime.environment,
            user=account.pw_uid, group=account.pw_gid,
            extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
            capture_output=True, text=True, check=False, timeout=60,
        )
        if response.returncode:
            raise ValueError
        values = json.loads(response.stdout)
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError
        parsed = []
        for value in values:
            match = _VERSION.match(value)
            if match is None:
                raise ValueError
            parsed.append(match[0])
        redis = min(parsed[1:], key=lambda version: tuple(map(int, version.split('.'))))
        return parsed[0], redis
    except Exception:
        raise UpgradeExecutionError('Cannot verify live PostgreSQL and Redis prerequisites') from None


def _check_servers(cfg: Configuration, live: GitNetBoxInstallation, planned: UpgradeStage,
                   approval: ExecutionApproval, *, probe: Callable = probe_server_versions) -> None:
    pg, redis = probe(cfg, live)
    actual = check_prerequisites(planned.netbox_version, live.python_version,
                                 postgres_version=pg, redis_version=redis)
    previous = {check.name: check for check in planned.prerequisites}
    for check in actual:
        old = previous.get(check.name)
        if check.status == 'blocked':
            raise UpgradeExecutionError('Combined stage has incompatible server prerequisites')
        if (old is None or old.required != check.required
                or (check.status == 'unknown' and
                    (old.status != 'unknown' or not approval.approved_unknowns))):
            raise UpgradeExecutionError('Combined stage has new unvalidated prerequisites')


def _resolve(pins: PluginUpgrade, live: GitNetBoxInstallation,
             approval: ExecutionApproval, resolver: Callable, *, wheelhouse: Path | None = None) -> DependencyPreflight:
    options = {'wheelhouse': wheelhouse} if wheelhouse is not None else {}
    result = resolver(pins.local_pins, pins.core_pins, live.python_version,
                      interpreter=live.python, **options)
    if result.status == 'blocked':
        raise UpgradeExecutionError('Combined stage has conflicting Python dependencies')
    if (result.status != 'passed' or not result.checked_core) and not approval.approved_unknowns:
        raise UpgradeExecutionError('Combined stage dependencies require explicit confirmation')
    return result


# Compile the one explicitly supported non-core source package with a
# transient, unprivileged systemd user. Return the wheel through stdout, not
# a shared writable directory or a privileged archive extraction.
_LDAP_WHEEL_BUILD = """
import subprocess, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='nbo-ldap-') as temp:
    subprocess.run([sys.executable, '-I', '-m', 'pip', '--isolated', 'wheel',
                    '--only-binary=:all:', '--no-binary=python-ldap', '--no-deps',
                    '--no-input', '--disable-pip-version-check', '--no-cache-dir',
                    '--index-url', sys.argv[2], '--timeout', '20', '--retries', '2',
                    '--wheel-dir', temp, sys.argv[1]],
                   cwd=temp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=240, check=True)
    wheel, = Path(temp).glob('*.whl')
    sys.stdout.buffer.write(wheel.name.encode('ascii') + b'\\n' + wheel.read_bytes())
"""


def _stop_ldap_builder(unit: str, *, cleanup: Callable = subprocess.run) -> None:
    """The transient systemd unit is outside the caller's process group."""
    try:
        stopped = cleanup(['systemctl', 'stop', unit], timeout=25, check=False,
                          capture_output=True, text=True)
        if stopped.returncode:
            state = cleanup(['systemctl', 'show', '--property=LoadState,ActiveState', unit],
                            timeout=15, check=False, capture_output=True, text=True)
            if 'LoadState=not-found' not in state.stdout.splitlines() and not (
                    not state.returncode and 'ActiveState=inactive' in state.stdout.splitlines()):
                raise ValueError
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise UpgradeExecutionError('Cannot verify cleanup of temporary LDAP wheel builder') from None


def _build_ldap_wheel(pin: str, live: GitNetBoxInstallation, directory: Path, *,
                      runner: Callable = run_managed) -> Path:
    match = re.fullmatch(r'python-ldap==([0-9]+(?:\\.[0-9]+){0,4}(?:\\.post[0-9]+)?)', pin)
    if match is None:
        raise UpgradeExecutionError('LDAP wheel build requires an exact verified python-ldap version')
    unit = 'netbox-ops-ldap-' + secrets.token_hex(8) + '.service'
    command = [
        'systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + unit,
        '--property=DynamicUser=yes', '--property=NoNewPrivileges=yes',
        '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
        '--property=PrivateTmp=yes', '--property=PrivateDevices=yes',
        '--property=ProtectProc=invisible', '--property=RestrictSUIDSGID=yes',
        '--property=RuntimeMaxSec=300', '--property=TimeoutStopSec=10',
        '--property=KillMode=control-group', '--property=LimitFSIZE=16777216',
        '--property=InaccessiblePaths=-/opt/netbox/netbox -/var/lib/netbox-ops -/etc/netbox-ops',
        '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/tmp',
        'LANG=C.UTF-8', 'PIP_CONFIG_FILE=/dev/null', 'PIP_NO_CACHE_DIR=1',
        str(live.python), '-I', '-c', _LDAP_WHEEL_BUILD, pin, PYPI_INDEX,
    ]
    try:
        result = run_checked('wheel.ldap', command, runner=runner, timeout=330,
                             stdin=subprocess.DEVNULL, capture_output=True)
    except BaseException:
        _stop_ldap_builder(unit)
        raise
    name, separator, raw = result.stdout.partition(b'\\n')
    try:
        filename = name.decode('ascii')
    except UnicodeDecodeError:
        raise CommandFailure('wheel.ldap', 'artifacts') from None
    if (not separator or not re.fullmatch(r'python_ldap-' + re.escape(match[1])
            + r'-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\\.whl', filename)
            or not zipfile.is_zipfile(io.BytesIO(raw))):
        raise CommandFailure('wheel.ldap', 'artifacts')
    wheel = directory / filename
    with wheel.open('xb') as stream:
        stream.write(raw)
    return wheel


def download_stage_wheels(pins: PluginUpgrade, live: GitNetBoxInstallation,
                          directory: Path, *, runner: Callable = run_managed) -> None:
    """Prepare one wheel set for core, plugins and the upstream pip bootstrap.

    Resolve together before downtime; the official script installs core and
    plugins separately, so restrict both calls to this same resolved set.
    NetBox's psycopg[c] extra requires a locally compiled psycopg-c wheel.
    A known python-ldap version is built separately in an unprivileged unit;
    no arbitrary additional source distributions are allowed.
    """
    directory.mkdir(mode=0o700)
    env = upgrade_environment(live.python)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR='1')
    # Only the exact upstream core pin may authorize building psycopg-c.
    # Plugin requirements alone cannot enable source builds.
    build_psycopg = any(
        'c' in match.group(1).lower().split(',')
        for pin in pins.core_pins
        if (match := re.fullmatch(r'psycopg\[([a-z0-9_,]+)\]==[0-9.]+', pin, re.I))
    )
    source_options = ['--no-binary=psycopg-c'] if build_psycopg else []
    ldap_options = []
    if pins.ldap_source_pin:
        _build_ldap_wheel(pins.ldap_source_pin, live, directory, runner=runner)
        ldap_options = ['--find-links', str(directory), pins.ldap_source_pin]
    command = [
        str(live.python), '-I', '-m', 'pip', '--isolated', 'wheel',
        '--only-binary=:all:', *source_options, '--no-input', '--disable-pip-version-check',
        '--no-cache-dir', '--index-url', PYPI_INDEX, '--wheel-dir', str(directory),
        '--timeout', '20', '--retries', '2',
        'pip', 'wheel', *pins.core_pins, *pins.local_pins, *additional_dependency_pins(pins),
        *ldap_options,
    ]
    command_id = 'wheel.psycopg' if build_psycopg else 'wheel.prepare'
    run_checked(command_id, command, runner=runner, timeout=900, cwd=directory, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not any(directory.glob('*.whl')):
        raise CommandFailure(command_id, 'artifacts')


def prepare_combined_stages(
    cfg: Configuration, plan: UpgradePlan, approval: ExecutionApproval,
    stages: tuple[PinnedStage, ...], *, wheel_root: Path,
    fetch: Callable = fetch_pinned_stage, git: Callable = _run_git,
    resolver: Callable = isolated_dependency_preflight, probe: Callable = probe_server_versions,
    download: Callable = download_stage_wheels,
) -> tuple[CombinedStage, ...]:
    """Resolve every target's official core requirements before downtime."""
    live = plan.source.installation
    local = _read_config(live.root, 'local_requirements.txt')
    prepared = []
    for index, (pinned, planned) in enumerate(zip(stages, plan.stages, strict=True)):
        if pinned.version != planned.netbox_version or not re.fullmatch(r'[0-9a-f]{40}', pinned.commit):
            raise UpgradeExecutionError('Combined stage does not match the approved release')
        _check_servers(cfg, live, planned, approval, probe=probe)
        fetch(live.root, pinned)
        core = git(live.root, ['show', pinned.commit + ':requirements.txt']).encode('utf-8')
        pins = prepare_plugin_upgrade(plan, local, core, stage_index=index)
        pins = bind_reviewed_dependencies(plan, index, pinned.commit, core, pins)
        wheelhouse = wheel_root / f'stage-{index + 1}'
        if plan.dependency_reviews:
            if tuple(map(int, planned.netbox_version.split('.')[:2])) >= (4, 7):
                if probe_ltree(cfg, live) != plan.dependency_reviews[index].ltree:
                    raise UpgradeExecutionError('PostgreSQL ltree prerequisites changed after approval')
            download(pins, live, wheelhouse)
            resolved = _resolve(pins, live, approval, resolver, wheelhouse=wheelhouse)
            verify_reviewed_resolution(plan, index, resolved)
            if resolved.status != 'passed' or not resolved.checked_core:
                raise UpgradeExecutionError('Cannot verify prepared upgrade wheels before maintenance')
        else:
            # Lower-level callers retain the full preflight before wheel preparation.
            resolved = _resolve(pins, live, approval, resolver)
            download(pins, live, wheelhouse)
        prior_passed = any(
            stage_version == planned.netbox_version and item.status == 'passed' and item.checked_core
            for _, stage_version, item in plan.source.preflights
        )
        if prior_passed and (resolved.status != 'passed' or not resolved.checked_core):
            raise UpgradeExecutionError('Approved combined dependency verification became uncertain')
        prepared.append(CombinedStage(pinned, core, pins, resolved, wheelhouse))
    return tuple(prepared)


def recheck_combined_stage(
    cfg: Configuration, live: GitNetBoxInstallation, planned: UpgradeStage,
    prepared: CombinedStage, approval: ExecutionApproval, *,
    resolver: Callable = isolated_dependency_preflight, probe: Callable = probe_server_versions,
    reviewer: Callable = review_plugin_upgrade, approved_warnings: tuple[str, ...] = (),
) -> None:
    """Reject new conflicts or uncertainty immediately before each checkout."""
    _check_servers(cfg, live, planned, approval, probe=probe)
    result = _resolve(prepared.pins, live, approval, resolver, wheelhouse=prepared.wheelhouse)
    if prepared.resolution.packages and result.packages != prepared.resolution.packages:
        raise UpgradeExecutionError('Prepared stage dependency versions changed')
    if (prepared.resolution.status == 'passed' and prepared.resolution.checked_core
            and (result.status != 'passed' or not result.checked_core)):
        raise UpgradeExecutionError('Combined stage dependency verification became uncertain')
    if prepared.resolution.packages:
        # The full compatibility plan was refreshed under the lock, and these
        # exact offline artifacts cannot acquire new versions during maintenance.
        return
    previous = {normalize_name(check.package): check for check in planned.plugin_checks}
    for package, version in planned.plugin_versions:
        try:
            review = reviewer(package, [planned.netbox_version], [live.python_version],
                              version=version, preflights=[result])
        except Exception:
            raise UpgradeExecutionError('Cannot refresh combined plugin compatibility') from None
        if any(warning not in approved_warnings for warning in review.warnings):
            raise UpgradeExecutionError('Combined stage has new unvalidated plugin warnings')
        if review.selected_version != version or len(review.stages) != 1:
            raise UpgradeExecutionError('Combined plugin compatibility could not be verified')
        check = review.stages[0]
        if (normalize_name(check.package) != package or check.version != version
                or check.netbox_version != planned.netbox_version
                or check.python_version != live.python_version or check.status == 'blocked'):
            raise UpgradeExecutionError('Combined stage has incompatible plugin evidence')
        old = previous.get(package)
        if check.status == 'uncertain':
            if not approval.approved_unknowns or (old is not None and old.status != 'uncertain'):
                raise UpgradeExecutionError('Combined plugin compatibility became uncertain')
        if old is not None:
            prior = {(e.source, e.detail, e.status) for e in old.evidence}
            if any(e.status in ('warns', 'unknown') and e.source != 'Isolated pip resolver'
                   and (e.source, e.detail, e.status) not in prior for e in check.evidence):
                raise UpgradeExecutionError('Combined stage has new unvalidated plugin requirements')


def run_combined_stage(
    live: GitNetBoxInstallation, plan: UpgradePlan, prepared: CombinedStage,
    base_python: Path, *, systemd: SystemdServices,
    stage_runner: Callable = run_stage, inventory_reader: Callable = inventory_plugins,
    runner: Callable = run_managed,
) -> tuple[GitNetBoxInstallation, tuple[tuple[str, str | None], ...]]:
    """Use the official script with exact constraints and verify all approved pins."""
    root = live.root
    if not prepared.wheelhouse.is_dir() or not any(prepared.wheelhouse.glob('*.whl')):
        raise UpgradeExecutionError('Prepared combined wheel directory is unavailable')
    require_quiesced_writers(systemd)
    actual = inventory_reader(live)
    expected_before = {p.package: p.installed_version for p in plan.plugins}
    installed_before = {normalize_name(p.name): p.version for p in actual.packages}
    eligible = set(dict(configured_plugin_packages(plan.source.inventory)))
    if (len(installed_before) != len(actual.packages)
            or set(dict(configured_plugin_packages(actual))) != eligible
            or any(installed_before.get(name) != version for name, version in expected_before.items())
            or actual.requirements_hash != prepared.pins.original_hash
            or actual.enabled != plan.source.inventory.enabled):
        raise UpgradeExecutionError('Combined stage local requirements or configured plugins changed')

    with tempfile.TemporaryDirectory(prefix='netbox-ops-combined-') as temp:
        constraints = Path(temp) / 'constraints.txt'
        constraints.write_text(''.join(f'{name}=={version}\n' for name, version in prepared.pins.expected),
                               encoding='utf-8')
        constraints.chmod(0o600)

        def prepare_script(checked_out: GitNetBoxInstallation) -> dict[str, str]:
            core = _read_config(checked_out.root, 'requirements.txt')
            if core is None or core.decode('utf-8').replace('\r\n', '\n').strip().encode() != prepared.core:
                raise UpgradeExecutionError('Checked out core requirements differ from preflight')
            local = _read_config(root, 'local_requirements.txt')
            digest = None if local is None else hashlib.sha256(local).hexdigest()
            if digest != prepared.pins.original_hash:
                raise UpgradeExecutionError('Local requirements changed after checkout')
            if prepared.pins.requirements != (local or b''):
                _apply_requirements(root, prepared.pins)
            return {'PIP_CONSTRAINT': str(constraints), 'PIP_ONLY_BINARY': ':all:',
                    'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
                    'PIP_FIND_LINKS': str(prepared.wheelhouse), 'PIP_NO_CACHE_DIR': '1'}

        final = stage_runner(live, prepared.pinned, systemd=systemd, prepare_script=prepare_script)
    require_quiesced_writers(systemd)
    env = upgrade_environment(base_python)
    _command([str(final.python), '-I', '-m', 'pip', '--isolated', 'check'],
             root, env, 'dependency check', runner)
    inventory = inventory_reader(final)
    expected_hash = (None if prepared.pins.original_hash is None and not prepared.pins.requirements
                     else hashlib.sha256(prepared.pins.requirements).hexdigest())
    installed = {normalize_name(p.name): p.version for p in inventory.packages}
    if (len(installed) != len(inventory.packages)
            or set(dict(configured_plugin_packages(inventory))) != eligible
            or inventory.requirements_hash != expected_hash
            or inventory.enabled != plan.source.inventory.enabled
            or any(installed.get(name) != version for name, version in prepared.pins.expected)):
        raise UpgradeExecutionError('Combined stage installed versions differ from approved pins')
    return final, read_plugin_registry(final, env, systemd=systemd, runner=runner)
