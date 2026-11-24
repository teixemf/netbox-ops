#!/usr/bin/env python3
"""Exercise the actual self-upgrade CLI and independent helper inside a CI VM.

Fixture mode consumes unchanged candidate artifacts built on the CI host by
scripts/build-release.sh from disposable local commits. Only test transport
and explicit fault injection are added to those commits.
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
FIXTURE_ROOT = Path('/tmp/netbox-ops-vm-fixture')
FIXTURE_INPUT = '.ci-self-upgrade-assets'
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


def build_fixture(work, source_root, version_tag, version_object, *, steps, asset_prefix):
    """Run the official builder on a disposable local commit, never a tag."""
    source = work / f'source-{version_tag}'
    source.mkdir()
    for name in FILES:
        path = source_root / name
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
    readme = source / 'README.md'
    updated, count = re.subn(r'(\*\*Current version: )[^ ]+( —)',
                             rf'\g<1>{version_object}\g<2>', readme.read_text())
    assert count == 1, 'README version fixture not found'
    readme.write_text(updated)
    changelog = source / 'CHANGELOG.md'
    original = changelog.read_text()
    first_heading = re.search(r'^## \[', original, re.MULTILINE)
    assert first_heading
    changelog.write_text(original[:first_heading.start()]
                         + f'## [{version_object}] — Disposable CI candidate\n\n'
                         + '- Test package; never publish.\n\n'
                         + original[first_heading.start():])
    policy_file = module / 'release_requirements.json'
    policy = json.loads(policy_file.read_text())
    policy['required_intermediates'] = [f'v{step}' for step in steps]
    policy_file.write_text(json.dumps(policy, indent=2) + '\n')
    # The transport is confined to the test package, not the repository code.
    (module / '_vm_fixture.py').write_text(
        'import json\nfrom pathlib import Path\n'
        f'ROOT = Path({str(FIXTURE_ROOT)!r})\n'
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

_original_cleanup = cleanup_swap
def cleanup_swap(active, staged, backup, state):
    from ._vm_fixture import mode
    if mode() == 'cleanup_crash':
        # Validation is already durably committed; recovery must retain the new tree.
        os._exit(87)
    return _original_cleanup(active, staged, backup, state)
''')
    scripts = source / 'scripts'
    scripts.mkdir()
    for filename in ('build-release.sh', 'release-candidate.py'):
        shutil.copy2(source_root / 'scripts' / filename, scripts / filename)
    def git(*args):
        return run(['git', '-C', source, *args])
    git('init', '--quiet', '--initial-branch=fixture')
    git('add', '--all')
    git('-c', 'user.name=CI fixture', '-c', 'user.email=ci@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', f'CI candidate {version_tag}')
    commit = git('rev-parse', 'HEAD')
    assert re.fullmatch(r'[0-9a-f]{40}', commit)
    run(['bash', scripts / 'build-release.sh'])
    archive = source / 'dist' / f'netbox-ops-{version_tag}.tar.gz'
    run([sys.executable, scripts / 'release-candidate.py', 'validate', archive, version_tag])
    assets = work / 'assets' / version_tag
    assets.mkdir(parents=True)
    names = (archive.name, archive.name + '.sha256',
             f'netbox-ops-{version_tag}.compatibility.json',
             f'netbox-ops-{version_tag}.notes.md')
    for name in names:
        shutil.copy2(source / 'dist' / name, assets / name)
    row = {
        'tag_name': version_tag, 'draft': False, 'prerelease': False,
        'body': (assets / names[3]).read_text(), 'published_at': '2026-11-24T00:00:00Z',
        'assets': [{'name': name, 'size': (assets / name).stat().st_size,
                    'browser_download_url': f'{asset_prefix}{version_tag}/{name}'}
                   for name in names[:3]],
    }
    print(f'PASS: official candidate builder and validator {version_tag} ({commit})', flush=True)
    shutil.rmtree(source)
    return row


def build_candidates(output):
    """Host-only packaging; no installation, service changes, tags or publication."""
    source_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(source_root))
    from netbox_ops import __version__
    from netbox_ops.self_upgrade_releases import DOWNLOAD_PREFIX, Version

    if output != source_root / FIXTURE_INPUT or output.exists() or output.is_symlink():
        raise RuntimeError('Expected a new CI-only candidate output directory')
    output.mkdir(mode=0o700)
    base = Version.parse(f'v{__version__}')
    # Use a higher synthetic minor so the new changelog headings are unique.
    old = Version(base.major, base.minor + 1, 0)
    middle = Version(old.major, old.minor, 1)
    target = Version(old.major, old.minor, 2)
    rows = [build_fixture(output, source_root, f'v{v}', v, steps=steps,
                          asset_prefix=DOWNLOAD_PREFIX)
            for v, steps in ((old, ()), (middle, ()), (target, (middle,)))]
    (output / 'catalogue.json').write_text(json.dumps(rows))
    print('PASS: all three unpublished candidate artifacts generated', flush=True)


def fixture_scenarios(work):
    sys.path.insert(0, str(ACTIVE))
    from netbox_ops.self_upgrade_archive import verify_release_archive
    from netbox_ops.self_upgrade_manifest import parse_manifest
    from netbox_ops.self_upgrade_releases import Version

    catalogue = work / 'catalogue.json'
    first, second, third = json.loads(catalogue.read_text())
    old, middle, target = (Version.parse(row['tag_name']) for row in (first, second, third))
    archives = []
    for row in (first, second, third):
        tag = row['tag_name']
        assets = work / 'assets' / tag
        archive = assets / f'netbox-ops-{tag}.tar.gz'
        verify_release_archive(archive, archive.with_name(archive.name + '.sha256').read_bytes())
        requirements = parse_manifest(
            (assets / f'netbox-ops-{tag}.compatibility.json').read_bytes(), Version.parse(tag))
        assert requirements.required_intermediates == ((middle,) if tag == f'v{target}' else ())
        archives.append(archive)
    initial, intermediate, _ = archives
    original_digests = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (work / 'assets').rglob('*') if path.is_file()}
    print('PASS: CI-built artifacts and compatibility manifests verified in guest', flush=True)
    (work / 'mode').write_text('rollback')
    install_archive(initial, work)
    assert version() == str(old)
    before_services, before_files = services(), preserved()
    offset = AUDIT.stat().st_size if AUDIT.exists() else 0
    assert f'v{target}' in cli('list')
    assert f'v{middle} -> v{target}' in cli('plan', f'v{target}')
    assert 'cancelled' in cli('install', f'v{target}', '--confirm', 'WRONG', expected=3)
    assert version() == str(old) and not RESULT.exists()

    # A required release cannot be skipped even with the correct confirmation.
    catalogue.write_text(json.dumps([first, third]))
    cli('plan', f'v{target}', expected=3)
    cli('install', f'v{target}', '--confirm', f'UPGRADE v{target}', expected=3)
    catalogue.write_text(json.dumps([first, second, third]))
    cli('install', f'v{old}', '--confirm', f'UPGRADE v{old}', expected=3)
    assert version() == str(old) and not RESULT.exists()
    assert not any(p.exists() or p.is_symlink() for p in (HANDOFF, STATE, BACKUP))
    assert not list(ACTIVE.parent.glob('.netbox-ops-stage-*'))
    assert services() == before_services and preserved() == before_files
    print('PASS: missing mandatory intermediate and same-version upgrade blocked', flush=True)

    # Tamper with the actual package while keeping its advertised length.
    package_bytes = intermediate.read_bytes()
    intermediate.write_bytes(bytes([package_bytes[0] ^ 1]) + package_bytes[1:])
    try:
        cli('install', f'v{target}', '--confirm', f'UPGRADE v{target}', expected=1)
        assert version() == str(old) and not RESULT.exists()
        assert not any(p.exists() or p.is_symlink() for p in (HANDOFF, STATE, BACKUP))
        assert not list(ACTIVE.parent.glob('.netbox-ops-stage-*'))
        assert services() == before_services and preserved() == before_files
        print('PASS: checksum mismatch blocked before activation', flush=True)
    finally:
        intermediate.write_bytes(package_bytes)

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
            assert result['step'] == f'v{middle}' and result['target'] == f'v{target}'
            assert 'intermediate step' in cli('status', expected=4)
        assert_clean(middle if mode == 'success' else old)
        assert services() == before_services, 'A NetBox service changed or restarted'
        assert preserved() == before_files, 'Configuration or launcher changed'
        print(f'PASS: production CLI/helper fixture {mode}; services unchanged', flush=True)

    # Re-evaluate the original target from the newly installed intermediate.
    # Interrupt after validation is committed but before the previous tree is removed.
    (work / 'mode').write_text('cleanup_crash')
    assert f'Required upgrade path: v{target}' in cli('plan', f'v{target}')
    assert 'Upgrade submitted' in cli('install', f'v{target}', '--confirm', f'UPGRADE v{target}')
    result = wait_for_helper()
    assert result['phase'] == 'applying', result
    assert version() == str(target)
    assert BACKUP.is_dir() and HANDOFF.is_dir() and STATE.is_file()
    assert json.loads(STATE.read_text())['phase'] == 'validated'
    cli('status', expected=4)
    assert 'cleanup' in run([HANDOFF / 'recover'])
    result = json.loads(RESULT.read_text())
    assert result['phase'] == 'completed' and result['step'] == f'v{target}', result
    assert result['installed'] == f'v{middle}' and result['target'] == f'v{target}', result
    assert 'completed and validated' in cli('status')
    cli('install', f'v{old}', '--confirm', f'UPGRADE v{old}', expected=3)
    assert_clean(target)
    assert services() == before_services and preserved() == before_files
    print('PASS: mandatory intermediate path completed; validated cleanup recovered; downgrade blocked', flush=True)

    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
               for path, digest in original_digests.items()), 'Candidate artifacts changed'
    with AUDIT.open('rb') as stream:
        stream.seek(offset)
        audit = stream.read().decode()
    for message in ('SELF-UPGRADE rollback completed',
                    'SELF-UPGRADE recovery finished result=rollback',
                    'SELF-UPGRADE recovery finished result=cleanup',
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
    parser.add_argument('--build-candidates', type=Path)
    parser.add_argument('--official-from')
    parser.add_argument('--official-target')
    args = parser.parse_args()
    if bool(args.official_from) != bool(args.official_target):
        parser.error('Both official release tags are required')
    if args.build_candidates is not None:
        if args.official_from:
            parser.error('Candidate build and published pair are separate modes')
        build_candidates(args.build_candidates.resolve())
        return
    guard()
    if args.official_from:
        with tempfile.TemporaryDirectory(prefix='netbox-ops-vm-release-', dir='/tmp') as name:
            official_scenario(Path(name), args.official_from, args.official_target)
    else:
        FIXTURE_ROOT.mkdir(mode=0o700)
        try:
            shutil.copytree(ACTIVE / FIXTURE_INPUT, FIXTURE_ROOT, dirs_exist_ok=True)
            fixture_scenarios(FIXTURE_ROOT)
        finally:
            shutil.rmtree(FIXTURE_ROOT)


if __name__ == '__main__':
    main()
