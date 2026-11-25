"""Grouped NetBox upgrade navigation without changes to command execution."""
import curses
import unittest
from unittest.mock import patch

from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.menu import MENU_ACTIONS, TUI_MENU_ACTIONS, UPGRADE_ACTIONS
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class UpgradeSubmenuTests(unittest.TestCase):
    def test_cli_paths_and_numbers_remain_unchanged(self):
        self.assertEqual([(n, group, action) for n, group, action, _ in MENU_ACTIONS], [
            ('1', 'services', 'manage'), ('2', 'backup', 'manage'),
            ('3', 'updates', 'check'), ('4', 'upgrade', 'netbox'),
            ('5', 'upgrade', 'plugins'), ('6', 'upgrade', 'stack'),
            ('7', 'self-upgrade', ''),
        ])
        self.assertEqual(TUI_MENU_ACTIONS[-1][:3], ('5', 'self-upgrade', ''))

    def test_numeric_selection_routes_to_each_existing_upgrade_view(self):
        for language in LANGUAGES:
            t = Translator(language)
            self.assertIn('upgrade_submenu_hint', t.messages)
            for index, (operation, label) in enumerate(UPGRADE_ACTIONS, 1):
                with self.subTest(language=language, operation=operation):
                    screen = FakeScreen([ord(str(index)), 27], dimensions=(16, 50))
                    menu = CursesMenu(screen, t)
                    with patch.object(menu, '_upgrade') as upgrade:
                        menu._upgrade_menu()
                    upgrade.assert_called_once_with(operation)
                    self.assertTrue(any(text.startswith(f'  {index}  ') and
                                        t(label)[:10] in text for _, _, text, _ in screen.frames[0]))

    def test_arrows_enter_and_escape_return_to_parent(self):
        screen = FakeScreen([10, curses.KEY_DOWN, curses.KEY_DOWN, 10, 27])
        menu = CursesMenu(screen, Translator('en'))
        with patch.object(menu, '_upgrade') as upgrade:
            menu._upgrade_menu()
        upgrade.assert_called_once_with('plugins')
        self.assertFalse(any(attr == curses.A_REVERSE for _, _, _, attr in screen.frames[0]))

    def test_invalid_shortcut_and_escape_after_focus_never_open_upgrade(self):
        screen = FakeScreen([ord('4'), curses.KEY_DOWN, 27])
        menu = CursesMenu(screen, Translator('en'))
        with patch.object(menu, '_upgrade') as upgrade:
            menu._upgrade_menu()
        upgrade.assert_not_called()
        self.assertEqual(list(screen.keys), [])

    def test_global_quit_inside_upgrade_submenu(self):
        menu = CursesMenu(FakeScreen([ord('q')]), Translator('en'))
        with patch.object(menu, '_init_screen'), patch.object(menu, '_main',
                side_effect=menu._upgrade_menu), patch.object(menu, '_upgrade') as upgrade:
            self.assertEqual(menu.run(), 0)
        upgrade.assert_not_called()
