"""Offline tests for the persistent, read-only self-upgrade catalogue."""
import unittest
from concurrent.futures import Future
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


class ImmediateExecutor:
    """Exercise the real TUI job wrapper deterministically, without a real tty."""

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class SelfUpgradeTuiTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('netbox_ops.tui.concurrent.futures.ThreadPoolExecutor',
                                ImmediateExecutor))
        self.enterContext(patch('netbox_ops.self_upgrade_ui.tui_recovery_state',
                                side_effect=lambda t: (True, [t('self_upgrade_no_result')])))

    def test_select_release_and_show_sanitized_notes(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        releases = [release('v0.4.0'), release('v0.6.0', 'Fixed\x1b[2J')]
        with (patch('netbox_ops.tui.__version__', '0.5.0'),
              patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=releases) as discovery,
              patch.object(menu, '_choose', side_effect=[0, 0, 1, None]) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        discovery.assert_called_once_with()
        self.assertIn('0.5.0', str(choose.call_args_list[0].kwargs['overview']))
        entries = choose.call_args_list[1].args[1]
        self.assertIn('v0.6.0', entries[0][0])
        self.assertIn('installation blocked', entries[0][0])
        self.assertIn('downgrade blocked', entries[1][0])
        self.assertIn('v0.6.0', str(choose.call_args_list[2].kwargs['overview']))
        self.assertNotIn(menu.t('self_upgrade_install_reviewed'),
                         [label for label, _ in choose.call_args_list[2].args[1]])
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
        self.assertIn(menu.t('self_upgrade_empty'), str(choose.call_args.kwargs['overview']))
        self.assertEqual([label for label, _ in choose.call_args.args[1]],
                         [menu.t('self_upgrade_status_menu'), menu.t('upgrade_refresh')])
        show.assert_not_called()

    def test_discovery_error_is_localized(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    side_effect=ReleaseDiscoveryError('private error')),
              patch.object(menu, '_choose', return_value=None) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        choose.assert_called_once()
        self.assertIn(menu.t('self_upgrade_discovery_failed'), str(show.call_args_list))
        self.assertNotIn('private error', str(show.call_args))
        self.assertIn(menu.t('upgrade_refresh'), str(choose.call_args.args[1]))

    def test_cancel_does_not_open_changelog(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=[release('v9.0.0')]),
              patch.object(menu, '_choose', side_effect=[0, None, None]) as choose,
              patch.object(menu, '_show_lines') as show):
            menu._self_upgrade()
        self.assertIn(menu.t('self_upgrade_unverified'), str(choose.call_args_list[1]))
        show.assert_not_called()
