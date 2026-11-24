#!/usr/bin/env python3
"""Exercise the actual self-upgrade CLI and independent helper inside a CI VM.

Fixture mode builds release-shaped packages from the checkout and substitutes
only the GitHub asset transport and explicit failure injection in those packages.
Official mode uses the published packages and unmodified production transport.
Never run this controller outside a disposable NetBox CI guest.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ACTIVE = Path('/opt/netbox-ops')
LAUNCHER = Path('/usr/local/sbin/netbox-ops')
LINK = Path('/usr/sbin/netbox-ops')
HANDOFF = Path('/opt/.netbox-ops-handoff')
STATE = Path('/opt/.netbox-ops-transaction.json')
BACKUP = Path('/opt/.netbox-ops-backup')
RESULT = Path('/var/lib/netbox-ops/self-upgrade-result.json')
AUDIT = Path('/var/log/netbox-ops/operations.log')
LOCK = Path('/run/lock/netbox-ops.lock')
MARKER = Path('/run/netbox-ops-ci-e2e')
UNITS = ('netbox.service', 'netbox-rq.service', 'nginx.service',
         'redis-server.service', 'postgresql@16-main.service')
PRESERVED = (Path('/etc/netbox-ops/config.json'),
             Path('/opt/netbox/netbox/netbox/configuration.py'), LAUNCHER)
FILES = ('LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
         'install.sh', 'upgrade.sh', 'netbox_ops.py', 'netbox_ops')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


def run(args, *, expected=0, timeout=180):
    result = subprocess.run([str(arg) for arg in args], env=ENV, text=True,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode != expected:
        raise RuntimeError(f'{args[0]}: expected {expected}, got {result.returncode}\n'
                           f'{result.stdout}\n{result.stderr}')
    return result.stdout.strip()


def guard():
    if os.geteuid() != 0:
        raise RuntimeError('Root required in disposable CI guest')
    info = MARKER.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or MARKER.read_text().strip() != 'netbox-ci-e2e'):
        raise RuntimeError('Disposable CI guest marker is invalid')
    if run(['systemd-detect-virt', '--vm']) not in ('qemu', 'kvm'):
        raise RuntimeError('Expected QEMU/KVM guest')
    assert ACTIVE.is_dir() and not ACTIVE.is_symlink()
    assert not any(p.exists() or p.is_symlink() for p in (HANDOFF, STATE, BACKUP, RESULT))


def services():
    result = {}
    for unit in UNITS:
        output = run(['systemctl', 'show', unit, '-p', 'ActiveState', '-p',
                      'MainPID', '-p', 'InvocationID', '-p', 'ActiveEnterTimestampMonotonic'])
        assert 'ActiveState=active' in output.splitlines(), unit
        result[unit] = output
    run(['curl', '--fail', '--silent', '--show-error', '--output', '/dev/null',
         'http://127.0.0.1/login/'])
    return result


def preserved():
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_uid,
                     stat.S_IMODE(p.stat().st_mode)) for p in PRESERVED}


def version():
    return run([LAUNCHER, '--version']).removeprefix('netbox-ops ')


def cli(*args, expected=0):
    return run([LAUNCHER, '--lang', 'en', 'self-upgrade', *args], expected=expected)


def wait_for_helper():
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                return json.loads(RESULT.read_text())
        finally:
            os.close(fd)
        time.sleep(0.25)
    raise RuntimeError('Self-upgrade helper did not release the operation lock')


def assert_clean(expected):
    assert version() == str(expected), version()
    assert not any(p.exists() or p.is_symlink() for p in (HANDOFF, STATE, BACKUP))
    assert not list(ACTIVE.parent.glob('.netbox-ops-stage-*'))
    assert ACTIVE.stat().st_uid == 0 and stat.S_IMODE(ACTIVE.stat().st_mode) == 0o755
    assert LAUNCHER.stat().st_uid == 0 and stat.S_IMODE(LAUNCHER.stat().st_mode) == 0o755
    assert LINK.is_symlink() and LINK.readlink() == LAUNCHER
    assert RESULT.stat().st_uid == 0 and stat.S_IMODE(RESULT.stat().st_mode) == 0o600
    assert AUDIT.stat().st_uid == 0 and stat.S_IMODE(AUDIT.stat().st_mode) == 0o600


def install_archive(archive, work):
    # The caller verified the official layout/checksum before this point.
    destination = work / 'initial'
    destination.mkdir()
    with tarfile.open(archive, 'r:gz') as source:
        source.extractall(destination, filter='data')
    guard()
    # Only replace the disposable checkout, never NetBox or persistent data.
    shutil.rmtree(ACTIVE)
    shutil.copytree(destination / 'netbox-ops', ACTIVE)
    ACTIVE.chmod(0o755)
    run(['bash', ACTIVE / 'install.sh'])
    assert LINK.is_symlink() and LINK.readlink() == LAUNCHER


def append(path, text):
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n\n' + text + '\n')


def build_fixture(work, version_tag, version_object, *, requirements, asset_prefix):
    """Build a package with test-only transport, using real parser/checksum code."""
    source = work / f'source-{version_tag}' / 'netbox-ops'
    source.mkdir(parents=True)
    for name in FILES:
        path = ACTIVE / name
        if path.is_dir():
            shutil.copytree(path, source / name, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(path, source / name)
    module = source / 'netbox_ops'
    version_file = module / '__init__.py'
    updated, count = re.subn(r'^__version__ = "[^"]+"$', f'__version__ = "{version_object}"',
                             version_file.read_text(), flags=re.MULTILINE)
    assert count == 1, 'Package version fixture not found'
    version_file.write_text(updated)
    # The transport is confined to the test package, not the repository code.
    (module / '_vm_fixture.py').write_text(
        'import json\nfrom pathlib import Path\n'
        f'ROOT = Path({str(work)!r})\n'
        f'PREFIX = {asset_prefix!r}\n'
        'def rows():\n    return json.loads((ROOT / "catalogue.json").read_text())\n'
        'def mode():\n    return (ROOT / "mode").read_text().strip()\n'
        'def opener(request, timeout):\n'
        '    url = request.full_url\n'
        '    if not url.startswith(PREFIX):\n        raise ValueError("unexpected fixture URL")\n'
        '    parts = url.removeprefix(PREFIX).split("/")\n'
        '    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):\n'
        '        raise ValueError("unexpected fixture path")\n'
        '    return (ROOT / "assets" / parts[0] / parts[1]).open("rb")\n'
    )
    append(module / 'self_upgrade_releases.py', '''
def discover_releases():
    from ._vm_fixture import rows
    return parse_releases(rows())
''')
    append(module / 'self_upgrade_manifest.py', '''
_original_read = read_release_requirements
def read_release_requirements(release, *, opener=None):
    from ._vm_fixture import opener as fixture_opener
    return _original_read(release, opener=fixture_opener)
''')
    append(module / 'self_upgrade_download.py', '''
_original_download = download_release
def download_release(release, *, temp_root=Path('/tmp'), opener=None):
    from ._vm_fixture import opener as fixture_opener
    return _original_download(release, temp_root=temp_root, opener=fixture_opener)
''')
    append(module / 'self_upgrade_handoff.py', '''
_original_validate = _validate
def _validate(active, record):
    from ._vm_fixture import mode
    _original_validate(active, record)
    if mode() == 'rollback':
        raise SwapError('Injected post-install validation failure')
''')
    append(module / 'self_upgrade_helper.py', '''
_original_begin = begin_swap
def begin_swap(active, staged, backup, state):
    from ._vm_fixture import mode
    if mode() != 'crash':
        return _original_begin(active, staged, backup, state)
    from unittest.mock import patch
    original = os.rename
    def interrupted(source, destination):
        original(source, destination)
        if source == active and destination == backup:
            os._exit(86)
    with patch('netbox_ops.self_upgrade_transaction.os.rename', side_effect=interrupted):
        return _original_begin(active, staged, backup, state)
''')
    assets = work / 'assets' / version_tag
    assets.mkdir(parents=True)
    archive = assets / f'netbox-ops-{version_tag}.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(source, arcname='netbox-ops')
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
    metadata = assets / f'netbox-ops-{version_tag}.compatibility.json'
    metadata.write_bytes(requirements)
    row = {
        'tag_name': version_tag, 'draft': False, 'prerelease': False,
        'body': 'Disposable CI fixture', 'published_at': '2026-11-24T00:00:00Z',
        'assets': [{'name': item.name, 'size': item.stat().st_size,
                    'browser_download_url': f'{asset_prefix}{version_tag}/{item.name}'}
                   for item in (archive, checksum, metadata)],
    }
    return archive, checksum, row


def fixture_scenarios(work):
    sys.path.insert(0, str(ACTIVE))
    from netbox_ops import __version__
    from netbox_ops.self_upgrade_archive import verify_release_archive
    from netbox_ops.self_upgrade_compatibility import ReleaseRequirements
    from netbox_ops.self_upgrade_manifest import encode_manifest
    from netbox_ops.self_upgrade_releases import DOWNLOAD_PREFIX, Version

    old = Version.parse(f'v{__version__}')
    target = Version(old.major, old.minor, old.patch + 1)

    def requirements(v):
        return encode_manifest(ReleaseRequirements(v, (3, 12), None,
                               frozenset({'debian', 'rhel'}), frozenset({'archive'})))

    initial, checksum, first = build_fixture(
        work, f'v{old}', old, requirements=requirements(old), asset_prefix=DOWNLOAD_PREFIX)
    new, new_checksum, second = build_fixture(
        work, f'v{target}', target, requirements=requirements(target), asset_prefix=DOWNLOAD_PREFIX)
    verify_release_archive(initial, checksum.read_bytes())
    verify_release_archive(new, new_checksum.read_bytes())
    (work / 'catalogue.json').write_text(json.dumps([first, second]))
    (work / 'mode').write_text('rollback')
    install_archive(initial, work)
    assert version() == str(old)
    before_services, before_files = services(), preserved()
    offset = AUDIT.stat().st_size if AUDIT.exists() else 0
    assert f'v{target}' in cli('list')
    assert f'v{target}' in cli('plan', f'v{target}')
    assert 'cancelled' in cli('install', f'v{target}', '--confirm', 'WRONG', expected=3)
    assert version() == str(old) and not RESULT.exists()

    for mode in ('rollback', 'crash', 'success'):
        (work / 'mode').write_text(mode)
        output = cli('install', f'v{target}', '--confirm', f'UPGRADE v{target}')
        assert 'Upgrade submitted' in output, output
        result = wait_for_helper()
        if mode == 'crash':
            assert result['phase'] in ('submitted', 'applying'), result
            assert not ACTIVE.exists() and BACKUP.is_dir() and STATE.is_file()
            assert (HANDOFF / 'recover').is_file()
            run([HANDOFF / 'recover'])
            assert 'previous installation' in cli('status', expected=1)
        elif mode == 'rollback':
            assert result['phase'] == 'rolled_back', result
            assert 'previous installation' in cli('status', expected=1)
        else:
            assert result['phase'] == 'completed', result
            assert 'completed and validated' in cli('status')
        assert_clean(target if mode == 'success' else old)
        assert services() == before_services, 'A NetBox service changed or restarted'
        assert preserved() == before_files, 'Configuration or launcher changed'
        print(f'PASS: production CLI/helper fixture {mode}; services unchanged', flush=True)

    with AUDIT.open('rb') as stream:
        stream.seek(offset)
        audit = stream.read().decode()
    for message in ('SELF-UPGRADE rollback completed',
                    'SELF-UPGRADE recovery finished result=rollback',
                    'SELF-UPGRADE finished'):
        assert message in audit, message


def official_scenario(work, old_tag, target_tag):
    """Validate two actual published releases, when they exist."""
    sys.path.insert(0, str(ACTIVE))
    from netbox_ops.self_upgrade_download import download_release
    from netbox_ops.self_upgrade_releases import Version, discover_releases

    old, target = Version.parse(old_tag), Version.parse(target_tag)
    assert old < target
    releases = {item.version: item for item in discover_releases()}
    assert old in releases and target in releases, 'Official release pair is not available'
    initial = download_release(releases[old], temp_root=work)
    install_archive(initial, work)
    assert version() == str(old)
    before_services, before_files = services(), preserved()
    for _ in range(64):
        assert target_tag in cli('plan', target_tag)
        assert 'Upgrade submitted' in cli('install', target_tag, '--confirm', f'UPGRADE {target_tag}')
        result = wait_for_helper()
        assert result['phase'] == 'completed', result
        step = Version.parse(result['step'])
        assert_clean(step)
        assert services() == before_services and preserved() == before_files
        if step == target:
            assert 'completed and validated' in cli('status')
            print(f'PASS: official release upgrade {old_tag} -> {target_tag}', flush=True)
            return
        cli('status', expected=4)
    raise RuntimeError('Official upgrade exceeded intermediate step limit')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--official-from')
    parser.add_argument('--official-target')
    args = parser.parse_args()
    if bool(args.official_from) != bool(args.official_target):
        parser.error('Both official release tags are required')
    guard()
    with tempfile.TemporaryDirectory(prefix='netbox-ops-vm-release-', dir='/tmp') as name:
        work = Path(name)
        if args.official_from:
            official_scenario(work, args.official_from, args.official_target)
        else:
            fixture_scenarios(work)


if __name__ == '__main__':
    main()
