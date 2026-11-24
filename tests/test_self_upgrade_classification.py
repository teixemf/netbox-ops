"""End-to-end read-only classification with mocked official metadata assets."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from netbox_ops.cli import main
from netbox_ops.i18n import Translator
from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_catalogue import CompatibilityCatalogue
from netbox_ops.self_upgrade_compatibility import LocalEnvironment
from netbox_ops.self_upgrade_manifest import read_release_requirements
from netbox_ops.self_upgrade_releases import Version, parse_releases
from netbox_ops.self_upgrade_ui import detect_environment
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen
from tests.test_self_upgrade_manifest import payload, row


class CompatibilityInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.installed = Version(0, 5, 0)
        self.environment = LocalEnvironment((3, 12), 'debian', 'archive')
        self.contents = {
            'v0.4.0': payload('v0.4.0'),
            'v0.6.0': payload('v0.6.0'),
            'v0.7.0': payload('v0.7.0', required_intermediates=['v0.6.0']),
            'v0.8.0': payload('v0.8.0', python_min='3.13'),
            'v0.9.0': b'{}',
        }
        self.releases = parse_releases([row(tag, data) for tag, data in self.contents.items()])
        self.requests = []

    def catalogue(self, releases, installed, environment):
        def reader(release):
            self.requests.append(release.tag)
            return read_release_requirements(
                release, opener=lambda *args, **kwargs: io.BytesIO(self.contents[release.tag]))
        return CompatibilityCatalogue(releases, installed=installed,
                                      environment=environment, reader=reader)

    def run_cli(self, *args):
        with (patch('netbox_ops.self_upgrade_ui.__version__', '0.5.0'),
              patch('netbox_ops.self_upgrade_ui.discover_releases', return_value=self.releases),
              patch('netbox_ops.self_upgrade_ui.detect_environment', return_value=self.environment),
              patch('netbox_ops.self_upgrade_ui.CompatibilityCatalogue', side_effect=self.catalogue),
              patch('netbox_ops.cli.load_config', side_effect=AssertionError('config used')),
              redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err):
            result = main(list(args))
        self.assertEqual(result, 0)
        self.assertFalse(err.getvalue())
        return out.getvalue()

    def test_cli_lists_compatible_incompatible_unknown_and_older(self):
        out = self.run_cli('--lang', 'en', 'self-upgrade', 'list')
        self.assertIn('v0.9.0  Compatibility not verified', out)
        self.assertIn('v0.8.0  Incompatible', out)
        self.assertIn('v0.7.0  Compatible', out)
        self.assertIn('v0.4.0  Older version', out)
        self.assertNotIn('v0.4.0', self.requests)
        self.assertIn('installation is not enabled', out)

    def test_cli_details_show_mandatory_path(self):
        out = self.run_cli('--lang', 'en', 'self-upgrade', 'show', 'v0.7.0')
        self.assertIn('Required upgrade path: v0.6.0 -> v0.7.0', out)
        self.assertIn('Changes', out)
        self.assertEqual(self.requests, ['v0.7.0', 'v0.6.0'])

    def test_cli_explains_blocked_intermediate_in_portuguese(self):
        self.releases = [r for r in self.releases if r.version != Version(0, 6, 0)]
        out = self.run_cli('--lang', 'pt', 'self-upgrade', 'show', 'v0.7.0')
        self.assertIn('Compatibilidade não verificada', out)
        self.assertIn('Bloqueado na versão 0.6.0', out)
        self.assertNotIn('Percurso de upgrade obrigatório:', out)

    def test_unknown_environment_never_reads_manifest(self):
        self.environment = LocalEnvironment(None, None, None)
        out = self.run_cli('--lang', 'en', 'self-upgrade', 'show', 'v0.7.0')
        self.assertIn('local installation environment could not be verified', out)
        self.assertEqual(self.requests, [])

    def test_tui_uses_same_snapshot_and_shows_path(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        with (patch('netbox_ops.tui.__version__', '0.5.0'),
              patch('netbox_ops.self_upgrade_releases.discover_releases', return_value=self.releases),
              patch('netbox_ops.self_upgrade_ui.detect_environment', return_value=self.environment),
              patch('netbox_ops.self_upgrade_ui.CompatibilityCatalogue', side_effect=self.catalogue),
              patch.object(menu, '_choose', side_effect=[2, None]) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        self.assertIn('v0.7.0  Compatible', choose.call_args_list[0].args[1][2][0])
        self.assertIn('Required upgrade path: v0.6.0 -> v0.7.0', str(show.call_args))
        self.assertEqual(self.requests.count('v0.7.0'), 1)
        self.assertEqual(self.requests.count('v0.6.0'), 1)

    def test_environment_is_derived_from_verified_installation(self):
        from netbox_ops.self_upgrade_installation import InstalledRelease

        local = InstalledRelease(self.installed, self.environment, None, (1, 2))
        with patch('netbox_ops.self_upgrade_installation.inspect_installation', return_value=local):
            self.assertEqual(detect_environment(self.installed), self.environment)
        with patch('netbox_ops.self_upgrade_installation.inspect_installation',
                   return_value=replace(local, version=Version(0, 4, 0))):
            self.assertIsNone(detect_environment(self.installed).layout)
        with patch('netbox_ops.self_upgrade_installation.inspect_installation',
                   side_effect=ReleaseArchiveError('unknown installation')):
            self.assertIsNone(detect_environment(self.installed).layout)
