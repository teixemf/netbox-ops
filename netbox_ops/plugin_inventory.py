"""Safe, read-only NetBox plugin inventory and explicitly approved exact pins.

Never import NetBox configuration or installed packages. Dynamic plugin settings and
ambiguous requirements must be reviewed by an operator, not rewritten automatically.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Sequence

from .upgrade_discovery import GitNetBoxInstallation, inspect_git_installation
from .services import ExclusiveLock, ServiceError, upgrade_recovery_pending, write_audit

_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
_VERSION = re.compile(r'^[0-9][A-Za-z0-9.+_-]*$')
_HEAD = re.compile(r'^([ \t]*)([A-Za-z0-9][A-Za-z0-9_.-]*)(\[[A-Za-z0-9_,.-]+\])?(.*)$')
_BARE_TAIL = re.compile(r'^([ \t]*)(#[^\r\n]*)?$')
_PIN_TAIL = re.compile(r'^[ \t]*==[ \t]*([0-9][A-Za-z0-9.+_-]*)(?:[ \t]*#[^\r\n]*)?$')
_METADATA_LIMIT = 128 * 1024
_RECORD_LIMIT = 4 * 1024 * 1024
_REQUIREMENTS_LIMIT = 1024 * 1024


class PluginInventoryError(Exception):
    """Unsupported, unsafe or changed inventory source."""


@dataclass(frozen=True)
class InstalledPackage:
    name: str
    version: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class DeclaredRequirement:
    number: int
    name: str
    kind: str  # bare, pinned, ambiguous
    version: str | None


@dataclass(frozen=True)
class PinProposal:
    package: str
    version: str
    line: int | None  # append when None
    before: str
    after: str


@dataclass(frozen=True)
class PluginInventory:
    root: Path
    requirements_hash: str | None
    requirements: tuple[DeclaredRequirement, ...]
    packages: tuple[InstalledPackage, ...]
    enabled: tuple[str, ...] | None
    proposals: tuple[PinProposal, ...]
    warnings: tuple[str, ...]


def normalize_name(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def resolve_plugin_package(packages: Sequence[InstalledPackage],
                           configured: str) -> InstalledPackage | None:
    """Resolve a configured module using distribution metadata, never names alone."""
    if not all(part.isidentifier() for part in configured.split('.')):
        return None
    top = configured.split('.')[0]
    matching = [package for package in packages if top in package.modules]
    if len(matching) != 1:
        return None
    name = normalize_name(matching[0].name)
    if sum(normalize_name(package.name) == name for package in packages) != 1:
        return None
    return matching[0]


def configured_plugin_packages(inventory: PluginInventory) -> tuple[tuple[str, str], ...]:
    """Return only unambiguously mapped, installed, configured plugins."""
    mapped = {
        (normalize_name(package.name), package.version)
        for module in inventory.enabled or ()
        if (package := resolve_plugin_package(inventory.packages, module)) is not None
    }
    return tuple(sorted(mapped))


def _read_regular(path: Path, limit: int) -> bytes | None:
    if path.is_symlink():
        raise PluginInventoryError(f'Unsafe symlink: {path.name}')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise PluginInventoryError(f'Not a supported regular file: {path.name}')
    # Opening with O_NOFOLLOW also prevents a symlink introduced after lstat.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PluginInventoryError(f'Not a supported regular file: {path.name}')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise PluginInventoryError(f'File too large: {path.name}')
        return data
    finally:
        os.close(fd)


def _requirements(root: Path) -> tuple[str, str | None]:
    raw = _read_regular(root / 'local_requirements.txt', _REQUIREMENTS_LIMIT)
    if raw is None:
        return '', None
    try:
        return raw.decode('utf-8'), hashlib.sha256(raw).hexdigest()
    except UnicodeDecodeError as exc:
        raise PluginInventoryError('Requirements must be valid UTF-8') from exc


def _parse_requirements(text: str) -> tuple[DeclaredRequirement, ...]:
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', '-')):
            continue
        match = _HEAD.fullmatch(line)
        if not match:
            continue
        name = match.group(2)
        tail = match.group(4)
        if _BARE_TAIL.fullmatch(tail):
            records.append(DeclaredRequirement(number, name, 'bare', None))
        elif (pin := _PIN_TAIL.fullmatch(tail)):
            records.append(DeclaredRequirement(number, name, 'pinned', pin.group(1)))
        else:
            records.append(DeclaredRequirement(number, name, 'ambiguous', None))
    return tuple(records)


def _enabled_plugins(configuration: Path) -> tuple[str, ...] | None:
    raw = _read_regular(configuration, _REQUIREMENTS_LIMIT)
    if raw is None:
        return None
    try:
        module = ast.parse(raw.decode('utf-8'), filename='configuration.py')
    except (UnicodeDecodeError, SyntaxError):
        return None
    values = []
    found = 0
    # If assignments or in-place edits are conditional/dynamic, report unknown.
    for node in ast.walk(module):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'PLUGINS'):
            return None
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets_to_check = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                   and t.value.id == 'PLUGINS' for t in targets_to_check):
                return None
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == 'PLUGINS' for target in targets):
            continue
        found += 1
        if node not in module.body or not isinstance(node, (ast.Assign, ast.AnnAssign)):
            return None
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            return None
        if any(not isinstance(item, ast.Constant) or not isinstance(item.value, str)
               or not item.value for item in value.elts):
            return None
        values.extend(item.value for item in value.elts)
    return tuple(dict.fromkeys(values)) if found == 1 else None


def _record_modules(path: Path) -> tuple[str, ...]:
    """Use bounded wheel file metadata when top_level.txt is absent or empty."""
    try:
        raw = _read_regular(path, _RECORD_LIMIT)
        if raw is None:
            return ()
        modules = set()
        for row in csv.reader(io.StringIO(raw.decode('utf-8')), strict=True):
            if len(row) != 3:
                return ()
            parts = row[0].split('/')
            if not all(parts) or any(part in ('.', '..') for part in parts):
                continue
            if not parts[-1].endswith(('.py', '.so')):
                continue
            top = parts[0] if len(parts) > 1 else parts[0].split('.')[0]
            if top.isidentifier():
                modules.add(top)
        return tuple(sorted(modules))
    except (PluginInventoryError, OSError, UnicodeDecodeError, csv.Error):
        # Optional metadata is not evidence when missing, unsafe or malformed.
        return ()


def _installed_packages(root: Path, python_version: str) -> tuple[InstalledPackage, ...]:
    match = re.fullmatch(r'(\d+)\.(\d+)\.\d+', python_version)
    if not match:
        raise PluginInventoryError('Unverified Python virtualenv version')
    subdir = f'python{match.group(1)}.{match.group(2)}/site-packages'
    packages = []
    visited = set()
    venv = (root / 'venv').resolve()
    for base in ('lib', 'lib64'):
        directory = root / 'venv' / base / subdir
        if not directory.exists():
            continue
        resolved = directory.resolve()
        if venv not in resolved.parents or not resolved.is_dir():
            raise PluginInventoryError('Unsafe site-packages directory')
        if resolved in visited:
            continue
        visited.add(resolved)
        for dist in directory.glob('*.dist-info'):
            if dist.is_symlink() or not dist.is_dir():
                raise PluginInventoryError('Unsafe distribution metadata directory')
            raw = _read_regular(dist / 'METADATA', _METADATA_LIMIT)
            if raw is None:
                continue
            try:
                meta = Parser().parsestr(raw.decode('utf-8'), headersonly=True)
            except UnicodeDecodeError:
                continue
            name, version = meta.get('Name', ''), meta.get('Version', '')
            if not _NAME.fullmatch(name) or not _VERSION.fullmatch(version):
                continue
            top_level = _read_regular(dist / 'top_level.txt', _METADATA_LIMIT)
            modules = ()
            if top_level is not None:
                try:
                    modules = tuple(line for line in top_level.decode('utf-8').splitlines()
                                    if line.isidentifier())
                except UnicodeDecodeError:
                    pass
            if not modules:
                modules = _record_modules(dist / 'RECORD')
            packages.append(InstalledPackage(name, version, modules))
    return tuple(sorted(packages, key=lambda p: normalize_name(p.name)))


def inventory_plugins(installation: GitNetBoxInstallation) -> PluginInventory:
    """Build proposals only for reliably matched enabled plugins; never write."""
    root = installation.root
    text, digest = _requirements(root)
    declared = _parse_requirements(text)
    installed = _installed_packages(root, installation.python_version)
    enabled = _enabled_plugins(root / 'netbox/netbox/configuration.py')
    warnings = []
    proposals = []
    if enabled is None:
        warnings.append('Cannot statically determine enabled PLUGINS; review configuration.py')
    groups = {}
    for item in declared:
        groups.setdefault(normalize_name(item.name), []).append(item)
    lines = text.splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    # Includes, editable/VCS links and URL eggs can refer to the same package.
    # Never append an inferred pin to a requirements file with unknown inclusions.
    unknown_sources = any(line.lstrip().startswith(('-r ', '--requirement', '-c ', '--constraint',
                     '-e ', '--editable', 'git+', 'hg+', 'svn+', 'bzr+'))
                     for line in text.splitlines())
    for plugin in enabled or ():
        package = resolve_plugin_package(installed, plugin)
        if package is None:
            warnings.append(f'Cannot reliably map enabled plugin {plugin} to one installed package')
            continue
        key = normalize_name(package.name)
        if any(normalize_name(egg) == key for egg in
               re.findall(r'[#&]egg=([A-Za-z0-9_.-]+)', text, flags=re.IGNORECASE)):
            warnings.append(f'{package.name}: possible VCS requirement needs manual review')
            continue
        entries = groups.get(key, ())
        if len(entries) > 1:
            warnings.append(f'Duplicate requirement for {package.name}; manual review needed')
            continue
        if entries and entries[0].kind == 'pinned':
            if entries[0].version != package.version:
                warnings.append(f'{package.name}: installed version differs from exact requirement')
            continue
        if entries and entries[0].kind != 'bare':
            warnings.append(f'{package.name}: complex requirement needs manual review')
            continue
        if not _VERSION.fullmatch(package.version):
            warnings.append(f'{package.name}: installed version cannot be pinned safely')
            continue
        if not entries:
            if unknown_sources:
                warnings.append(f'{package.name}: other requirements sources need review before adding a pin')
                continue
            proposals.append(PinProposal(package.name, package.version, None, '',
                                         f'{package.name}=={package.version}{newline}'))
            continue
        entry = entries[0]
        before = lines[entry.number - 1]
        match = _HEAD.fullmatch(before.rstrip('\r\n'))
        if match is None:
            raise PluginInventoryError('Requirements changed during parsing')
        suffix = before[len(before.rstrip('\r\n')):]
        after = f'{match.group(1)}{match.group(2)}{match.group(3) or ""}=={package.version}{match.group(4)}{suffix}'
        proposals.append(PinProposal(package.name, package.version, entry.number, before, after))
    return PluginInventory(root, digest, declared, installed, enabled, tuple(proposals), tuple(warnings))


def apply_approved_pins(inventory: PluginInventory, selected: Sequence[str],
                        *, approved: bool = False) -> None:
    """Apply ONLY selected, unchanged proposals under the shared operation lock."""
    if not approved or os.geteuid() != 0:
        raise PluginInventoryError('Explicit approval and root privileges are required')
    unique = list(dict.fromkeys(normalize_name(name) for name in selected))
    if not unique or len(unique) != len(selected):
        raise PluginInventoryError('Select a nonempty set of distinct plugin packages')
    available = {normalize_name(item.package): item for item in inventory.proposals}
    if len(available) != len(inventory.proposals) or any(name not in available for name in unique):
        raise PluginInventoryError('Only unambiguous proposed pins can be approved')
    with ExclusiveLock():
        try:
            if upgrade_recovery_pending():
                raise PluginInventoryError('NetBox upgrade recovery is pending; inspect recovery before changing pins')
        except ServiceError as exc:
            raise PluginInventoryError(str(exc)) from None
        # Revalidate trust and source content after acquiring the lock.
        state = inspect_git_installation(inventory.root)
        if state.root != inventory.root:
            raise PluginInventoryError('NetBox installation changed before approval')
        current = inventory_plugins(state)
        if (current.requirements_hash != inventory.requirements_hash or
                current.enabled != inventory.enabled or
                current.packages != inventory.packages or
                current.proposals != inventory.proposals):
            raise PluginInventoryError('Plugin inventory changed since the plan was prepared')
        text, digest = _requirements(inventory.root)
        if digest != inventory.requirements_hash:
            raise PluginInventoryError('Requirements changed since the plan was prepared')
        lines = text.splitlines(keepends=True)
        chosen = [available[name] for name in unique]
        for proposal in chosen:
            if proposal.line is not None:
                if proposal.line > len(lines) or lines[proposal.line - 1] != proposal.before:
                    raise PluginInventoryError('Requirements changed since the plan was prepared')
                lines[proposal.line - 1] = proposal.after
        additions = [proposal.after for proposal in chosen if proposal.line is None]
        if additions and lines and not lines[-1].endswith(('\n', '\r')):
            lines[-1] += '\r\n' if '\r\n' in text else '\n'
        result = ''.join(lines) + ''.join(additions)
        target = inventory.root / 'local_requirements.txt'
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        if (existing is None) != (digest is None):
            raise PluginInventoryError('Requirements changed before pin replacement')
        mode, group = 0o600, 0
        if existing is not None:
            mode = stat.S_IMODE(existing.st_mode)
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != 0
                    or existing.st_nlink != 1 or mode & 0o7022):
                raise PluginInventoryError('Local requirements must be a safe root-controlled file')
            group = existing.st_gid
        # Keep audit failure from occurring *after* the replacement.
        write_audit('plugin requirements: approved exact pins: ' + ','.join(unique))
        fd, temporary = tempfile.mkstemp(prefix='.netbox-ops-pins-', dir=inventory.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchown(stream.fileno(), 0, group)
                os.fchmod(stream.fileno(), mode)
                stream.write(result.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                now = target.lstat()
            except FileNotFoundError:
                now = None
            if now != existing:
                raise PluginInventoryError('Requirements changed before pin replacement')
            os.replace(temporary, target)
            directory_fd = os.open(inventory.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
