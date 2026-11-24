#!/usr/bin/env python3
"""Exercise self-upgrade transactions on a disposable NetBox CI VM only.

Use locally built release-shaped fixtures, not a published release or a
public installation command. The copied driver survives directory swaps.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ACTIVE = Path('/opt/netbox-ops')
LAUNCHER = Path('/usr/local/sbin/netbox-ops')
LINK = Path('/usr/sbin/netbox-ops')
STATE = Path('/opt/.netbox-ops-transaction.json')
BACKUP = Path('/opt/.netbox-ops-backup')
AUDIT = Path('/var/log/netbox-ops/operations.log')
MARKER = Path('/run/netbox-ops-ci-e2e')
UNITS = ('netbox.service', 'netbox-rq.service', 'nginx.service',
         'redis-server.service', 'postgresql@16-main.service')
PRESERVED = (Path('/etc/netbox-ops/config.json'),
             Path('/opt/netbox/netbox/netbox/configuration.py'), LAUNCHER)
RELEASE_FILES = ('LICENSE', 'README.md', 'CHANGELOG.md', 'config.example.json',
                 'install.sh', 'upgrade.sh', 'netbox_ops.py', 'netbox_ops')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


def run(args, *, timeout=120):
    result = subprocess.run(args, env=ENV, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr}')
    return result.stdout.strip()


def require_disposable_vm():
    if os.geteuid() != 0:
        raise RuntimeError('Root is required in the disposable CI VM')
    try:
        info = MARKER.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError('Disposable CI VM marker is missing') from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or MARKER.read_text().strip() != 'netbox-ci-e2e'):
        raise RuntimeError('Invalid disposable CI VM marker')
    if run(['systemd-detect-virt', '--vm'], timeout=10) not in ('qemu', 'kvm'):
        raise RuntimeError('Expected a QEMU/KVM virtual machine')


def version_output():
    return run([str(LAUNCHER), '--version'], timeout=15)


def snapshot_services():
    snapshots = {}
    for unit in UNITS:
        output = run(['systemctl', 'show', unit, '-p', 'ActiveState', '-p',
                      'MainPID', '-p', 'InvocationID', '-p',
                      'ActiveEnterTimestampMonotonic'], timeout=15)
        assert 'ActiveState=active' in output.splitlines(), unit
        snapshots[unit] = output
    run(['curl', '--fail', '--silent', '--show-error', '--output', '/dev/null',
         'http://127.0.0.1/login/'], timeout=20)
    return snapshots


def preserved_files():
    return {path: (hashlib.sha256(path.read_bytes()).hexdigest(),
                   path.stat().st_uid, stat.S_IMODE(path.stat().st_mode))
            for path in PRESERVED}


def require_driver():
    driver = Path(__file__).resolve().parent
    if (driver.parent != Path('/tmp')
            or not driver.name.startswith('netbox-ops-vm-self-upgrade-')
            or driver.stat().st_uid != 0 or driver.stat().st_mode & 0o077):
        raise RuntimeError('Expected private copied test driver')
    sys.path.insert(0, str(driver))
    return driver


class InjectedValidationFailure(Exception):
    pass


def helper(stage_name, old_tag, new_tag, mode, fd):
    require_driver()
    from netbox_ops.self_upgrade_helper import run_approved_swap
    from netbox_ops.self_upgrade_releases import Version
    from netbox_ops.self_upgrade_runtime import validate_installation_runtime

    if (not re.fullmatch(r'\.netbox-ops-stage-[A-Za-z0-9_-]+', stage_name)
            or mode not in ('success', 'rollback', 'crash')):
        raise RuntimeError('Invalid fixture stage or mode')
    staged = ACTIVE.with_name(stage_name)
    old, new = Version.parse(old_tag), Version.parse(new_tag)
    assert new > old

    def preflight(current, candidate):
        assert current == ACTIVE and candidate == staged
        assert version_output() == f'netbox-ops {old}'
        validate_installation_runtime(candidate, new)

    def validate(current):
        validate_installation_runtime(current, new)
        assert version_output() == f'netbox-ops {new}'
        if mode == 'rollback':
            raise InjectedValidationFailure('VM validation failure')
        if mode == 'crash':
            print('RESULT interrupted before validation commit', flush=True)
            os._exit(86)

    try:
        run_approved_swap(staged, initiator_fd=int(fd),
                          preflight=preflight, validate=validate)
    except InjectedValidationFailure:
        assert mode == 'rollback'
        print('RESULT rollback', flush=True)
    else:
        assert mode == 'success'
        print('RESULT success', flush=True)


def initiate(stage_name, old_tag, new_tag, mode):
    driver = require_driver()
    from netbox_ops.self_upgrade_helper import open_initiator_pidfd

    descriptor = open_initiator_pidfd()
    try:
        # Inherit stdout/stderr so the controller can observe the child result.
        subprocess.Popen(
            [sys.executable, '-I', '-B', str(Path(__file__).resolve()), 'helper',
             stage_name, old_tag, new_tag, mode, str(descriptor)],
            cwd=driver, env=ENV, stdin=subprocess.DEVNULL, pass_fds=(descriptor,),
            start_new_session=True,
        )
    finally:
        os.close(descriptor)


def recover():
    require_driver()
    from netbox_ops.self_upgrade_helper import recover_interrupted_upgrade

    print(f'RESULT recovery {recover_interrupted_upgrade()}', flush=True)


def controller():
    assert ACTIVE.is_dir() and not ACTIVE.is_symlink()
    assert not STATE.exists() and not STATE.is_symlink()
    assert not BACKUP.exists() and not BACKUP.is_symlink()
    assert LINK.is_symlink() and LINK.readlink() == LAUNCHER
    assert LAUNCHER.stat().st_uid == 0 and stat.S_IMODE(LAUNCHER.stat().st_mode) == 0o755
    before_services = snapshot_services()
    before_files = preserved_files()
    audit_offset = AUDIT.stat().st_size if AUDIT.exists() else 0

    with tempfile.TemporaryDirectory(prefix='netbox-ops-vm-self-upgrade-', dir='/tmp') as name:
        work = Path(name)
        driver = work / 'scenario.py'
        shutil.copy2(Path(__file__), driver)
        shutil.copytree(ACTIVE / 'netbox_ops', work / 'netbox_ops',
                        ignore=shutil.ignore_patterns('__pycache__'))
        sys.path.insert(0, str(work))
        from netbox_ops.self_upgrade_releases import Version
        from netbox_ops.self_upgrade_runtime import stage_installation

        current = Version.parse('v' + version_output().removeprefix('netbox-ops '))
        target = Version(current.major, current.minor, current.patch + 1)
        fixture = work / 'netbox-ops'
        fixture.mkdir()
        for entry in RELEASE_FILES:
            source = ACTIVE / entry
            if source.is_dir():
                shutil.copytree(source, fixture / entry,
                                ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(source, fixture / entry)
        version_file = fixture / 'netbox_ops/__init__.py'
        changed, count = re.subn(r'^__version__ = "[^"]+"$',
                                 f'__version__ = "{target}"',
                                 version_file.read_text(), flags=re.MULTILINE)
        assert count == 1, 'Unable to create a versioned release fixture'
        version_file.write_text(changed)
        archive = work / f'netbox-ops-v{target}.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(fixture, arcname='netbox-ops')
        checksum = f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n'.encode()

        for mode in ('rollback', 'crash', 'success'):
            stage = stage_installation(archive, checksum, target, python=Path(sys.executable))
            assert stage.stat().st_uid == 0 and stat.S_IMODE(stage.stat().st_mode) == 0o700
            output = run([sys.executable, '-I', '-B', str(driver), 'initiate',
                          stage.name, f'v{current}', f'v{target}', mode])
            if mode == 'crash':
                assert 'RESULT interrupted before validation commit' in output, output
                assert STATE.is_file() and BACKUP.is_dir()
                assert version_output() == f'netbox-ops {target}'
                result = run([sys.executable, '-I', '-B', str(driver), 'recover'])
                assert 'RESULT recovery rollback' in result, result
            else:
                assert f'RESULT {mode}' in output, output
            assert not STATE.exists() and not BACKUP.exists()
            if mode == 'success':
                assert not stage.exists()
                assert version_output() == f'netbox-ops {target}'
                assert ACTIVE.stat().st_uid == 0
                assert not ACTIVE.stat().st_mode & 0o022
            else:
                assert version_output() == f'netbox-ops {current}'
                assert stage.is_dir() and not stage.is_symlink()
                shutil.rmtree(stage)
            assert snapshot_services() == before_services, 'A NetBox service changed or restarted'
            assert preserved_files() == before_files, 'Persistent config or launcher changed'
            assert LINK.is_symlink() and LINK.readlink() == LAUNCHER
            print(f'PASS: self-upgrade fixture {mode}; services unchanged', flush=True)

        with AUDIT.open('rb') as stream:
            stream.seek(audit_offset)
            audit = stream.read().decode()
        for expected in ('SELF-UPGRADE rollback completed',
                         'SELF-UPGRADE recovery finished result=rollback',
                         'SELF-UPGRADE finished'):
            assert expected in audit, expected
        assert AUDIT.stat().st_uid == 0 and stat.S_IMODE(AUDIT.stat().st_mode) == 0o600


def main():
    require_disposable_vm()
    args = sys.argv[1:]
    if not args:
        controller()
    elif args[0] == 'initiate' and len(args) == 5:
        initiate(*args[1:])
    elif args[0] == 'helper' and len(args) == 6:
        helper(*args[1:])
    elif args == ['recover']:
        recover()
    else:
        raise RuntimeError('Invalid VM test arguments')


if __name__ == '__main__':
    main()
