"""Spatial keyboard navigation for wrapped Services and Backups actions."""

import curses
import unittest

from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu, move_action_focus
from tests.helpers.fake_screen import FakeScreen


class WrappedActionNavigationTests(unittest.TestCase):
    def setUp(self):
        self.menu = CursesMenu(FakeScreen(), Translator('en'))

    def test_horizontal_arrows_stay_in_the_same_row(self):
        positions = [(0, 0), (12, 0), (0, 1), (13, 1), (0, 2)]
        self.assertEqual(move_action_focus(0, curses.KEY_RIGHT, positions), 1)
        self.assertEqual(move_action_focus(1, curses.KEY_RIGHT, positions), 1)
        self.assertEqual(move_action_focus(2, curses.KEY_LEFT, positions), 2)
        self.assertEqual(move_action_focus(3, curses.KEY_LEFT, positions), 2)

    def test_vertical_arrows_follow_visual_columns(self):
        positions = [(0, 0), (12, 0), (0, 1), (13, 1), (0, 2)]
        self.assertEqual(move_action_focus(1, curses.KEY_DOWN, positions), 3)
        self.assertEqual(move_action_focus(3, curses.KEY_UP, positions), 1)
        self.assertEqual(move_action_focus(3, curses.KEY_DOWN, positions), 4)
        self.assertEqual(move_action_focus(4, curses.KEY_UP, positions), 2)
        self.assertEqual(move_action_focus(0, curses.KEY_UP, positions), 0)
        self.assertEqual(move_action_focus(4, curses.KEY_DOWN, positions), 4)

    def test_j_and_k_follow_vertical_arrows(self):
        positions = [(0, 0), (11, 0), (0, 1), (11, 1)]
        self.assertEqual(move_action_focus(1, ord('j'), positions), 3)
        self.assertEqual(move_action_focus(3, ord('k'), positions), 1)

    def test_single_row_does_not_wrap(self):
        positions = [(0, 0), (12, 0), (25, 0)]
        self.assertEqual(move_action_focus(2, curses.KEY_RIGHT, positions), 2)
        self.assertEqual(move_action_focus(0, curses.KEY_LEFT, positions), 0)
        self.assertEqual(move_action_focus(1, curses.KEY_UP, positions), 1)
        self.assertEqual(move_action_focus(1, curses.KEY_DOWN, positions), 1)

    def test_narrow_layouts_for_both_action_bars_and_locales(self):
        for locale in ('en', 'pt'):
            translator = Translator(locale)
            for kind, labels in (
                ('services', [translator('action_' + action) for action in
                              ('start', 'stop', 'restart', 'enable', 'disable')]),
                ('backups', [translator('nbo_act_' + action) for action in
                             ('create', 'verify', 'restore', 'delete', 'select_all')]),
            ):
                for width in (32, 46, 72, 120):
                    with self.subTest(locale=locale, kind=kind, width=width):
                        positions = self.menu._backup_action_positions(labels, width)
                        self.assertEqual(len(positions), len(labels))
                        for index, (x, row) in enumerate(positions):
                            for key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                                target = move_action_focus(index, key, positions)
                                self.assertEqual(positions[target][1], row)
                                if target != index:
                                    self.assertEqual(
                                        (positions[target][0] > x),
                                        key == curses.KEY_RIGHT,
                                    )
                            for key, offset in ((curses.KEY_UP, -1),
                                                (curses.KEY_DOWN, 1)):
                                target = move_action_focus(index, key, positions)
                                target_rows = {r for _, r in positions}
                                expected_row = row + offset if row + offset in target_rows else row
                                self.assertEqual(positions[target][1], expected_row)

    def test_resizing_recomputes_the_navigation_from_new_positions(self):
        labels = ['Create', 'Verify', 'Restore', 'Delete', 'Select all']
        wide = self.menu._backup_action_positions(labels, 100)
        narrow = self.menu._backup_action_positions(labels, 25)
        self.assertEqual(move_action_focus(0, curses.KEY_DOWN, wide), 0)
        self.assertNotEqual(move_action_focus(0, curses.KEY_DOWN, narrow), 0)

    def test_empty_positions_do_not_move_focus(self):
        self.assertEqual(move_action_focus(0, curses.KEY_RIGHT, []), 0)


if __name__ == '__main__':
    unittest.main()
