"""Use a real disposable Git checkout, never an installed NetBox instance."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from netbox_ops.upgrade_discovery import (
    UpgradeDiscoveryError, check_prerequisites, inspect_git_installation,
)


class GitInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'netbox'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.org')
        self.git('remote', 'add', 'origin', 'https://github.com/netbox-community/netbox.git')
        (self.root / '.gitignore').write_text('venv/\nlocal_requirements.txt\nnetbox/media/\nnetbox/netbox/configuration.py\n')
        (self.root / 'netbox').mkdir()
        (self.root / 'netbox/release.yaml').write_text('version: 4.5.3\n')
        (self.root / 'netbox/netbox').mkdir()
        (self.root / 'venv/bin').mkdir(parents=True)
        (self.root / 'venv/pyvenv.cfg').write_text('home = /usr/bin\n')
        candidates = [Path(sys.executable).resolve(), Path('/usr/local/bin/python3'),
                      Path('/usr/bin/python3')]
        trusted = next((p for p in candidates if p.is_file() and p.stat().st_uid == 0
                        and not p.stat().st_mode & 0o022), None)
        if trusted is None:
            self.skipTest('No root-owned Python interpreter available')
        (self.root / 'venv/bin/python').symlink_to(trusted.resolve())
        self.git('add', '.gitignore', 'netbox/release.yaml')
        self.git('commit', '-qm', 'Fixture')
        self.git('tag', 'v4.5.3')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.root), *args], check=True,
                       capture_output=True, text=True)

    def test_valid_tagged_install_reports_git_and_python(self):
        state = inspect_git_installation(self.root)
        self.assertEqual(state.version, '4.5.3')
        self.assertEqual(state.tag, 'v4.5.3')
        self.assertEqual(len(state.commit), 40)
        self.assertTrue(state.python_version.startswith('3.'))
        self.assertEqual(state.local_paths, ())

    def test_expected_ignored_local_files_are_inspected_not_rejected(self):
        (self.root / 'local_requirements.txt').write_text('plugin==1.0\n')
        (self.root / 'netbox/netbox/configuration.py').write_text('SECRET_KEY = "example"\n')
        (self.root / 'netbox/media').mkdir()
        state = inspect_git_installation(self.root)
        self.assertEqual(set(state.local_paths), {'local_requirements.txt',
                                                   'netbox/netbox/configuration.py', 'netbox/media'})

    def test_tracked_change_is_blocked_even_if_local_files_exist(self):
        (self.root / 'local_requirements.txt').write_text('plugin==1.0\n')
        (self.root / 'netbox/release.yaml').write_text('version: 4.5.4\n')
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'Tracked'):
            inspect_git_installation(self.root)

    def test_unrecognized_untracked_file_is_blocked(self):
        (self.root / 'custom-entrypoint.sh').write_text('echo test\n')
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'Unrecognized'):
            inspect_git_installation(self.root)

    def test_unofficial_origin_is_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com/evil/netbox.git')
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'origin'):
            inspect_git_installation(self.root)

    def test_origin_host_lookalike_is_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com.evil.example/netbox-community/netbox.git')
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'origin'):
            inspect_git_installation(self.root)

    def test_symlinked_install_or_git_directory_is_rejected(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'real directory'):
            inspect_git_installation(alias)

    def test_symlinked_local_config_rejected_without_following(self):
        outside = Path(self.temp.name) / 'outside-secret'
        outside.write_text('SECRET')
        (self.root / 'netbox/netbox/configuration.py').symlink_to(outside)
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'Unsafe local symlink'):
            inspect_git_installation(self.root)
        self.assertEqual(outside.read_text(), 'SECRET')

    def test_bad_tag_disagrees_with_release_yaml(self):
        self.git('tag', '-d', 'v4.5.3')
        self.git('tag', 'v4.4.1')
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'tag'):
            inspect_git_installation(self.root)

    def test_pre_v4_installation_is_unsupported(self):
        (self.root / 'netbox/release.yaml').write_text('version: 3.7.8\n')
        self.git('add', 'netbox/release.yaml')
        self.git('commit', '-qm', 'Unsupported fixture')
        with self.assertRaisesRegex(UpgradeDiscoveryError, '4.0 or newer'):
            inspect_git_installation(self.root)

    def test_no_release_tag_requires_manual_review(self):
        self.git('tag', '-d', 'v4.5.3')
        state = inspect_git_installation(self.root)
        self.assertIsNone(state.tag)
        self.assertTrue(state.warnings)

    def test_missing_python_is_not_accepted(self):
        (self.root / 'venv/bin/python').unlink()
        with self.assertRaisesRegex(UpgradeDiscoveryError, 'Python'):
            inspect_git_installation(self.root)

    def test_git_checkout_is_not_modified(self):
        self.git('status', '--porcelain')
        before = (self.root / '.git/index').stat().st_mtime_ns
        inspect_git_installation(self.root)
        self.assertEqual((self.root / '.git/index').stat().st_mtime_ns, before)


class PrerequisiteTests(unittest.TestCase):
    def test_supported_target_and_actual_service_versions(self):
        results = check_prerequisites('4.5.0', '3.12.7', postgres_version='15.2', redis_version='7.0.11')
        self.assertEqual([r.status for r in results], ['ok', 'ok', 'ok'])

    def test_incompatible_versions_block(self):
        results = check_prerequisites('4.7.1', '3.11.5', postgres_version='14.12', redis_version='5.0.14')
        self.assertEqual([r.status for r in results], ['blocked', 'blocked', 'blocked', 'unknown'])

    def test_unverified_server_versions_not_assumed(self):
        results = check_prerequisites('4.6.0', '3.12.2')
        self.assertEqual([r.status for r in results], ['ok', 'unknown', 'unknown'])

    def test_pre_v4_prerequisite_is_blocked(self):
        result, = check_prerequisites('3.7.8', '3.11.0')
        self.assertEqual(result.status, 'blocked')

    def test_unreviewed_release_is_unknown(self):
        result, = check_prerequisites('4.8.0', '3.14.2')
        self.assertEqual(result.status, 'unknown')
        self.assertIn('readthedocs', result.detail)

    def test_target_must_be_stable_exact(self):
        for version in ('4.7', 'v4.7.0-beta', 'whatever', '4.7.0rc1'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                check_prerequisites(version, '3.12.1')


if __name__ == '__main__':
    unittest.main()
