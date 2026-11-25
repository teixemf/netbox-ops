"""Select CI suites by changed files; unknown changes always run everything."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ALL = frozenset({'unit', 'postgres', 'package', 'netbox', 'self_upgrade', 'baseline'})
STANDARD = frozenset({'unit', 'postgres', 'package'})
VMS = frozenset({'netbox', 'self_upgrade', 'baseline'})
VM_WORKFLOWS = {
    '.github/workflows/vm-netbox-upgrades.yml': 'netbox',
    '.github/workflows/vm-self-upgrade.yml': 'self_upgrade',
    '.github/workflows/vm-netbox-integration.yml': 'baseline',
}
VM_FIXTURES = {
    'scripts/ci/netbox-vm-upgrade-e2e.sh': 'netbox',
    'scripts/ci/netbox-vm-self-upgrade.py': 'self_upgrade',
    'scripts/ci/netbox-vm-e2e.sh': 'baseline',
}
SHARED_VM = frozenset({
    'scripts/ci/netbox-vm-host.sh',
    'scripts/ci/netbox-vm-guest.sh',
    'scripts/ci/netbox-vm-baseline.env',
})


def file_scope(path: str) -> frozenset[str]:
    if path in {'README.md', 'CONTRIBUTING.md'} or (
        path.endswith('.md') and path.startswith(('docs/', 'tests/', '.github/'))
    ):
        return frozenset()
    if path in {
        '.github/workflows/ci-scope.yml', 'scripts/ci/test_scope.py',
        'netbox_ops/__init__.py', 'install.sh', 'config.example.json',
        'netbox_ops.py', 'netbox_ops/config.py', 'netbox_ops/platform.py',
        'netbox_ops/services.py', 'netbox_ops/netbox_version.py',
        'netbox_ops/upgrade_maintenance.py', 'netbox_ops/upgrade_recovery.py',
    }:
        return ALL
    if path in VM_WORKFLOWS:
        return frozenset({'unit', VM_WORKFLOWS[path]})
    if path in VM_FIXTURES:
        return frozenset({'unit', VM_FIXTURES[path]})
    if path in SHARED_VM:
        return VMS | {'unit'}
    if path == '.github/workflows/tests.yml':
        return STANDARD
    if path.startswith('tests/integration/'):
        return frozenset({'unit', 'postgres'})
    if path.startswith('tests/install/'):
        return frozenset({'unit', 'package'})
    if path.startswith('tests/') or path == 'ruff.toml':
        return frozenset({'unit'})
    if path in {'CHANGELOG.md', 'LICENSE', 'scripts/publish-release.py',
                '.github/workflows/build-release.yml'}:
        return frozenset({'unit', 'package'})
    if path in {'scripts/build-release.sh', 'scripts/release-candidate.py',
                'netbox_ops/release_requirements.json', 'upgrade.sh', 'uninstall.sh',
                'netbox_ops/uninstall.py'} or (
        path.startswith('netbox_ops/self_upgrade_') and path.endswith('.py')
    ):
        return frozenset({'unit', 'package', 'self_upgrade'})
    if path == 'netbox_ops/logrotate_policy.py':
        return VMS | {'unit', 'package'}
    if path in {'netbox_ops/backups.py', 'netbox_ops/restore_environment.py'}:
        return frozenset({'unit', 'postgres', 'netbox', 'baseline'})
    if path.startswith(('netbox_ops/upgrade_', 'netbox_ops/plugin_')) and path.endswith('.py'):
        return frozenset({'unit', 'netbox'})
    if path in {'netbox_ops/cli.py', 'netbox_ops/tui.py', 'netbox_ops/menu.py'}:
        return VMS | {'unit'}
    if path in {'netbox_ops/i18n.py', 'netbox_ops/journal.py',
                'netbox_ops/i18n/en.json', 'netbox_ops/i18n/pt.json'}:
        return frozenset({'unit'})
    # New or unclassified files must never silently reduce coverage.
    return ALL


def select(paths: list[str]) -> frozenset[str]:
    return frozenset().union(*(file_scope(path) for path in paths))


def changed_paths(base: str) -> list[str]:
    if not re.fullmatch(r'[0-9a-f]{40}', base) or base == '0' * 40:
        raise ValueError('Invalid comparison base')
    # Both sides of a rename must be classified. NUL separation handles any path.
    subprocess.run(['git', 'merge-base', '--is-ancestor', base, 'HEAD'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = subprocess.check_output(['git', 'diff', '--name-only', '-z',
                                   '--no-renames', base, 'HEAD', '--'])
    if raw and not raw.endswith(b'\0'):
        raise ValueError('Incomplete changed-path list')
    return [os.fsdecode(path) for path in raw.split(b'\0') if path]


def event_scope(event: str, payload: dict, ref: str) -> frozenset[str]:
    """Only development PRs and development pushes may use reduced coverage."""
    if event == 'pull_request':
        base = payload['pull_request']['base']
        if base['ref'] != 'dev':
            return ALL
        return select(changed_paths(base['sha']))
    if event == 'push' and ref == 'refs/heads/dev':
        return select(changed_paths(payload['before']))
    # Main, release workflow calls, manual runs and unknown events are full.
    return ALL


def main() -> None:
    try:
        payload = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        scope = event_scope(os.environ['GITHUB_EVENT_NAME'], payload, os.environ['GITHUB_REF'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('::warning::Cannot determine test scope; running all suites')
        scope = ALL
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        for name in sorted(ALL):
            print(f'{name}={str(name in scope).lower()}', file=output)
    print('Selected suites: ' + (', '.join(sorted(scope)) or 'lightweight checks only'))


if __name__ == '__main__':
    main()
