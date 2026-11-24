"""Offline tests for the read-only self-upgrade screen."""
import unittest
from unittest.mock import patch

from netbox_ops.i18n import Translator
from netbox_ops.self_upgrade_releases import (
    OfficialRelease, ReleaseAsset, ReleaseDiscoveryError, Version,
)
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


def release(tag, notes='Changes'):
    asset = ReleaseAsset('unused', 'unused', 1)
    return OfficialRelease(Version.parse(tag), tag, notes, '2026-11-24', asset, asset)


class SelfUpgradeTuiTests(unittest.TestCase):
    def test_select_release_and_show_sanitized_notes(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        releases = [release('v0.4.0'), release('v0.6.0', 'Fixed\x1b[2J')]
        with (patch('netbox_ops.tui.__version__', '0.5.0'),
              patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=releases) as discovery,
              patch.object(menu, '_choose', side_effect=[0, None]) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        discovery.assert_called_once_with()
        title, entries, _ = choose.call_args_list[0].args
        self.assertIn('0.5.0', title)
        self.assertIn('v0.6.0', entries[0][0])
        self.assertIn('installation blocked', entries[0][0])
        self.assertIn('downgrade blocked', entries[1][0])
        notes = '\n'.join(line for line, _ in show.call_args.args[1])
        self.assertIn('Fixed', notes)
        self.assertNotIn('\x1b', notes)
        self.assertIn('Read-only view', notes)

    def test_empty_catalogue(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        with (patch('netbox_ops.self_upgrade_releases.discover_releases', return_value=[]),
              patch.object(menu, '_choose', return_value=None) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        choose.assert_called_once()
        self.assertIn('No official stable releases', str(show.call_args))

    def test_discovery_error_is_localized(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    side_effect=ReleaseDiscoveryError('private error')),
              patch.object(menu, '_choose', return_value=None) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        choose.assert_called_once()
        self.assertIn('Não foi possível consultar', str(show.call_args_list))
        self.assertNotIn('private error', str(show.call_args))

    def test_cancel_does_not_open_changelog(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=[release('v9.0.0')]),
              patch.object(menu, '_choose', return_value=None) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        self.assertIn('Compatibilidade não verificada', str(choose.call_args))
        show.assert_not_called()
