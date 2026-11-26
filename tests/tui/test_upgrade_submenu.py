"""Direct upgrade dashboard routing; CLI paths remain unchanged."""
import unittest
from unittest.mock import patch

from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.menu import MENU_ACTIONS, TUI_MENU_ACTIONS
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class UpgradeDashboardRoutingTests(unittest.TestCase):
    def test_cli_paths_and_numbers_remain_unchanged(self):
        self.assertEqual([(n, group, action) for n, group, action, _ in MENU_ACTIONS], [
            ('1', 'services', 'manage'), ('2', 'backup', 'manage'),
            ('3', 'updates', 'check'), ('4', 'upgrade', 'netbox'),
            ('5', 'upgrade', 'plugins'), ('6', 'upgrade', 'stack'),
            ('7', 'self-upgrade', ''),
        ])
        self.assertEqual(TUI_MENU_ACTIONS[2][:3], ('3', 'upgrade', 'menu'))
        self.assertEqual(TUI_MENU_ACTIONS[-1][:3], ('4', 'self-upgrade', ''))

    def test_upgrade_entry_opens_dashboard_directly(self):
        for language in LANGUAGES:
            menu = CursesMenu(FakeScreen(), Translator(language))
            with patch('netbox_ops.upgrade_dashboard.UpgradeDashboard.run') as run:
                menu._upgrade_menu()
            run.assert_called_once_with()
