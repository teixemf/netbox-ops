"""Conservative, interactive removal of the fixed netbox-ops installation.

This standalone helper uses only the standard library (including on the
distribution's Python), and never invokes systemctl or a package manager.
"""
import fcntl
import json
import os
import re
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

# Only paths installed by the current installer are eligible. No systemd units
# or timers are installed by this release; do not guess ownership from names.
APP_ENTRIES = frozenset({
    'LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
    'install.sh', 'upgrade.sh', 'uninstall.sh', 'netbox_ops.py', 'netbox_ops',
    'venv', '__pycache__', '.git', '.github', '.gitignore', 'CONTRIBUTING.md',
    'docs', 'ruff.toml', 'scripts', 'tests',
})
BACKUP_SUFFIX = re.compile(r'\.(?:dump|sql|tar|tgz|gz|zip|bz2|xz|zst|backup|bak)(?:\.|$)', re.I)
AUDIT_NAME = re.compile(r'operations\.log(?:\.\d+(?:\.gz)?)?$')
LAUNCHER = b'#!/bin/sh\nexec /opt/netbox-ops/venv/bin/python /opt/netbox-ops/netbox_ops.py "$@"\n'
# Exact policy installed by logrotate_policy.py; customized files are retained.
POLICY = b"""/var/log/netbox-ops/operations.log {
    weekly
    rotate 4
    missingok
    notifempty
    compress
    delaycompress
    su root root
    create 0600 root root
}
"""


class UninstallError(RuntimeError):
    pass


class Layout:
    def __init__(self, root=Path('/')):
        # root is injectable only for isolated tests, never a CLI option.
        self.root = root
        self.app = root / 'opt/netbox-ops'
        self.launcher = root / 'usr/local/sbin/netbox-ops'
        self.link = root / 'usr/sbin/netbox-ops'
        self.policy = root / 'etc/logrotate.d/netbox-ops'
        self.config = root / 'etc/netbox-ops/config.json'
        self.logs = root / 'var/log/netbox-ops'
        self.data = root / 'var/lib/netbox-ops'
        self.lock = root / 'run/lock/netbox-ops.lock'


def entry(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def controlled(info):
    return info.st_uid == 0 and not info.st_mode & 0o022


def within(path, parent):
    return path == parent or parent in path.parents


def safe_parents(path, layout):
    """Reject symlinked or writable ancestors, even for absent files."""
    for parent in reversed(path.parents):
        if not within(parent, layout.root):
            continue
        info = entry(parent)
        if info is None:
            continue
        if not stat.S_ISDIR(info.st_mode) or not controlled(info):
            raise UninstallError(f'Unsafe directory: {parent}')


def mount_paths():
    """Detect bind mounts as well as mounts on a different device."""
    paths = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            field = line.split()[4]
            paths.append(Path(re.sub(r'\\([0-7]{3})',
                                     lambda m: chr(int(m.group(1), 8)), field)))
    return paths


def reject_mount(path, mounts):
    if any(within(mount, path) for mount in mounts):
        raise UninstallError(f'Mounted filesystem in removal path: {path}')


def regular(path, layout, *, private=False):
    safe_parents(path, layout)
    info = entry(path)
    if info is None:
        return None
    if (not stat.S_ISREG(info.st_mode) or not controlled(info)
            or info.st_nlink != 1 or (private and info.st_mode & 0o077)):
        raise UninstallError(f'Unexpected or unsafe file: {path}')
    return info


def configured_data(layout):
    """Protect configured backup/media locations without loading systemd."""
    protected = [layout.data, Path('/opt/netbox')]
    if regular(layout.config, layout, private=True) is None:
        return protected
    try:
        data = json.loads(layout.config.read_text(encoding='utf-8'))
        backup = data.get('backup', {})
        for key in ('directory', 'media_directory'):
            if key in backup:
                path = Path(backup[key])
                if not path.is_absolute() or '..' in path.parts or path == Path('/'):
                    raise ValueError('unsafe configured data path')
                protected.extend((path, path.resolve()))
        for artifact in backup.get('artifacts', []) or []:
            if artifact.get('id') == 'media':
                path = Path(artifact['path'])
                if not path.is_absolute() or '..' in path.parts or path == Path('/'):
                    raise ValueError('unsafe configured media path')
                protected.extend((path, path.resolve()))
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError) as exc:
        raise UninstallError('Cannot verify configured data paths; review config.json first') from exc
    return protected


def protect(path, protected):
    if any(within(item, path) or within(path, item) for item in protected):
        raise UninstallError(f'Removal overlaps protected data: {path}')


def check_app(layout, protected, mounts):
    root = layout.app
    safe_parents(root, layout)
    info = entry(root)
    if info is None:
        return False
    if not stat.S_ISDIR(info.st_mode) or not controlled(info):
        raise UninstallError(f'Unsafe installation directory: {root}')
    protect(root, protected)
    reject_mount(root, mounts)
    names = {item.name for item in root.iterdir()}
    if names - APP_ENTRIES:
        raise UninstallError('Unknown local files in installation; move them to safety first')
    if names and not names & {'netbox_ops.py', 'netbox_ops', 'install.sh', 'uninstall.sh'}:
        raise UninstallError('Cannot identify the remaining installation')
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise):
        for name in dirs + files:
            path = Path(directory) / name
            item = path.lstat()
            if item.st_uid != 0 or item.st_dev != info.st_dev:
                raise UninstallError(f'Unexpected owner or filesystem: {path}')
            relative = path.relative_to(root)
            backup_directory = (name.lower() in {'backups', 'backup'} and stat.S_ISDIR(item.st_mode)
                                and relative != Path('tests/backups'))
            if BACKUP_SUFFIX.search(name) or backup_directory:
                raise UninstallError(f'Possible backup data in installation: {path}')
            if stat.S_ISLNK(item.st_mode):
                # A venv normally contains these links. rmtree unlinks them,
                # never follows them, including a dangling system Python link.
                allowed = (relative == Path('venv/lib64') and os.readlink(path) == 'lib'
                           or relative.parent == Path('venv/bin')
                           and re.fullmatch(r'python(?:3(?:\.\d+t?)?)?|𝜋thon', name))
                if not allowed:
                    raise UninstallError(f'Unexpected symbolic link: {path}')
            elif (not controlled(item) or not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode))
                  or stat.S_ISREG(item.st_mode) and item.st_nlink != 1):
                raise UninstallError(f'Unsafe installation entry: {path}')
    return True


def _raise(error):
    raise error


def check_pending(layout):
    for name in ('.netbox-ops-handoff', '.netbox-ops-backup', '.netbox-ops-transaction.json'):
        if entry(layout.app.parent / name) is not None:
            raise UninstallError('Self-upgrade recovery is pending; resolve it before uninstalling')


def check_processes(layout):
    """Also refuse idle CLI/TUI instances that could start another operation."""
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        running = os.fsencode(layout.app / 'netbox_ops.py') in args
        for name in ('cwd', 'exe'):
            try:
                target = Path(os.readlink(proc / name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if name == 'exe':
                running |= within(target, layout.app / 'venv')
            else:
                running |= within(target, layout.app) and (
                    b'netbox_ops.py' in args or b'netbox_ops' in args)
        if running:
            raise UninstallError(f'netbox-ops is still running (PID {proc.name}); exit it first')


@contextmanager
def operation_lock(layout):
    # /run/lock may be sticky/world-writable. Check its parent and the
    # opened inode, and retain the named lock file after closing it.
    safe_parents(layout.lock.parent, layout)
    directory = layout.lock.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0
            or directory.st_mode & 0o022 and not directory.st_mode & stat.S_ISVTX):
        raise UninstallError('Unsafe operation lock directory')
    fd = os.open(layout.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        named = layout.lock.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise UninstallError('Unsafe operation lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UninstallError('Another netbox-ops operation is running') from exc
        yield
    finally:
        os.close(fd)


def plan(layout, remove_config=False, remove_logs=False, *, mounts=None):
    """Validate all selected paths before performing any removals."""
    mounts = mount_paths() if mounts is None else mounts
    check_pending(layout)
    protected = configured_data(layout)
    app = check_app(layout, protected, mounts)
    files = []
    retained = []
    for path, expected in ((layout.launcher, LAUNCHER), (layout.policy, POLICY)):
        safe_parents(path, layout)
        info = entry(path)
        if info is None:
            continue
        if (not stat.S_ISREG(info.st_mode) or not controlled(info) or info.st_nlink != 1
                or info.st_size != len(expected) or path.read_bytes() != expected):
            retained.append(path)
        else:
            protect(path, protected)
            reject_mount(path, mounts)
            files.append(path)
    safe_parents(layout.link, layout)
    info = entry(layout.link)
    if info is not None:
        if (stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and os.readlink(layout.link) == '/usr/local/sbin/netbox-ops'
                and layout.launcher not in retained):
            protect(layout.link, protected)
            reject_mount(layout.link, mounts)
            files.insert(0, layout.link)
        else:
            retained.append(layout.link)
    if remove_config and regular(layout.config, layout, private=True) is not None:
        protect(layout.config, protected)
        reject_mount(layout.config, mounts)
        files.append(layout.config)
    if remove_logs:
        safe_parents(layout.logs, layout)
        info = entry(layout.logs)
        if info is not None:
            if not stat.S_ISDIR(info.st_mode) or not controlled(info):
                raise UninstallError(f'Unsafe log directory: {layout.logs}')
            protect(layout.logs, protected)
            reject_mount(layout.logs, mounts)
            for path in sorted(layout.logs.iterdir()):
                if AUDIT_NAME.fullmatch(path.name):
                    regular(path, layout, private=True)
                    reject_mount(path, mounts)
                    files.append(path)
                else:
                    retained.append(path)
    return app, files, retained


def describe(layout, inventory):
    app, files, retained = inventory
    print('Will remove:')
    for path in files + ([layout.app] if app else []):
        print(f'  {path}')
    if not app and not files:
        print('  Nothing (already absent).')
    print(f'Always preserved: {layout.data} (including backups)')
    for path in retained:
        print(f'Leaving unrecognized or customized entry untouched: {path}')


def uninstall(layout, ask=input):
    inventory = plan(layout)
    describe(layout, inventory)
    print(f'Configuration and audit logs are preserved by default: {layout.config.parent}, {layout.logs}')
    if ask('Type UNINSTALL to continue: ') != 'UNINSTALL':
        print('Cancelled. No files removed.')
        return
    remove_config = ask('Type REMOVE CONFIG to also remove configuration (Enter to keep): ') == 'REMOVE CONFIG'
    remove_logs = ask('Type REMOVE LOGS to also remove audit logs (Enter to keep): ') == 'REMOVE LOGS'
    selected = plan(layout, remove_config, remove_logs)
    describe(layout, selected)
    if remove_config or remove_logs:
        if ask('Type CONFIRM to apply the additional cleanup: ') != 'CONFIRM':
            print('Cancelled. No files removed.')
            return
    with operation_lock(layout):
        check_processes(layout)
        final = plan(layout, remove_config, remove_logs)
        if final != selected:
            raise UninstallError('Installation changed since preview; rerun the uninstaller')
        app, files, _ = final
        if app and not shutil.rmtree.avoids_symlink_attacks:
            raise UninstallError('Safe recursive removal is unavailable')
        for path in files:
            path.unlink()
        if app:
            shutil.rmtree(layout.app)
    print('Uninstall complete. NetBox and backups were not removed.')


def main():
    if os.geteuid() != 0:
        print('Uninstallation requires root.', file=sys.stderr)
        return 1
    if len(sys.argv) != 1 or not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Uninstallation requires an interactive terminal and takes no arguments.', file=sys.stderr)
        return 1
    try:
        uninstall(Layout())
    except (UninstallError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        print(f'Uninstall stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
