"""Read-only self-upgrade CLI tests; never contact GitHub or local services."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from netbox_ops.cli import main
from netbox_ops.self_upgrade_releases import (
    OfficialRelease, ReleaseAsset, ReleaseDiscoveryError, Version,
)


def release(tag, notes='Changes'):
    asset = ReleaseAsset('unused', 'unused', 1)
    return OfficialRelease(Version.parse(tag), tag, notes, '2026-11-24', asset, asset)


class SelfUpgradeCliTests(unittest.TestCase):
    def run_cli(self, *args, releases=None, error=None):
        with (patch('netbox_ops.self_upgrade_ui.__version__', '0.5.0'),
              patch('netbox_ops.self_upgrade_ui.discover_releases',
                    return_value=releases or [], side_effect=error) as discovery,
              patch('netbox_ops.cli.load_config', side_effect=AssertionError('no config')),
              patch('netbox_ops.cli.SystemdServices', side_effect=AssertionError('no services')),
              redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            result = main(list(args))
        return result, output.getvalue(), errors.getvalue(), discovery.call_count

    def test_list_orders_versions_and_blocks_installation(self):
        result, out, err, calls = self.run_cli(
            '--lang', 'en', 'self-upgrade', 'list',
            releases=[release('v0.4.0'), release('v0.6.0'), release('v0.5.0')])
        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)
        self.assertFalse(err)
        self.assertLess(out.index('v0.6.0'), out.index('v0.5.0'))
        self.assertLess(out.index('v0.5.0'), out.index('v0.4.0'))
        self.assertIn('Compatibility not verified', out)
        self.assertIn('downgrade blocked', out)
        self.assertIn('Installed netbox-ops: 0.5.0', out)

    def test_portuguese_list_is_localized(self):
        result, out, _, _ = self.run_cli(
            '--lang', 'pt', 'self-upgrade', 'list', releases=[release('v0.6.0')])
        self.assertEqual(result, 0)
        self.assertIn('Compatibilidade não verificada', out)
        self.assertIn('instalação bloqueada', out)

    def test_changelog_uses_exact_version_and_removes_control_characters(self):
        result, out, err, _ = self.run_cli(
            '--lang', 'en', 'self-upgrade', 'show', '0.6.0',
            releases=[release('v0.7.0', 'Not selected'),
                      release('v0.6.0', 'Fixed\r\n\x1b[2J\x07Important\u202e')])
        self.assertEqual(result, 0)
        self.assertFalse(err)
        self.assertIn('Fixed\n', out)
        self.assertIn('Important', out)
        self.assertNotIn('Not selected', out)
        for control in ('\x1b', '\x07', '\u202e', '\r'):
            self.assertNotIn(control, out)

    def test_invalid_version_does_not_contact_github(self):
        result, _, err, calls = self.run_cli(
            '--lang', 'en', 'self-upgrade', 'show', '../v0.6.0')
        self.assertEqual(result, 2)
        self.assertEqual(calls, 0)
        self.assertIn('Invalid stable version', err)

    def test_missing_release(self):
        result, _, err, _ = self.run_cli(
            '--lang', 'en', 'self-upgrade', 'show', 'v0.6.0')
        self.assertEqual(result, 1)
        self.assertIn('Official release 0.6.0 not found', err)

    def test_discovery_error_is_safe_and_localized(self):
        result, _, err, _ = self.run_cli(
            '--lang', 'pt', 'self-upgrade', 'list',
            error=ReleaseDiscoveryError('\x1b[2J'))
        self.assertEqual(result, 1)
        self.assertIn('Não foi possível consultar', err)
        self.assertNotIn('\x1b', err)

    def test_empty_catalogue(self):
        result, out, _, _ = self.run_cli('--lang', 'en', 'self-upgrade', 'list')
        self.assertEqual(result, 0)
        self.assertIn('No official stable releases', out)

    def test_empty_and_oversized_notes(self):
        for notes, expected in (('', 'No release notes published.'),
                                ('x' * 40_001, 'Release notes truncated.')):
            with self.subTest(expected=expected):
                result, out, _, _ = self.run_cli(
                    '--lang', 'en', 'self-upgrade', 'show', 'v0.6.0',
                    releases=[release('v0.6.0', notes)])
                self.assertEqual(result, 0)
                self.assertIn(expected, out)

    def test_bare_command_remains_disabled_without_network(self):
        result, _, _, calls = self.run_cli('self-upgrade')
        self.assertEqual(result, 3)
        self.assertEqual(calls, 0)


if __name__ == '__main__':
    unittest.main()
