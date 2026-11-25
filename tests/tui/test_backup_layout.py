"""Backup inventory layout and read-only navigation regressions."""
from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupEntry
from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class BackupLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entries = []
        for index in range(25):
            name = f'backup-{index:02d}-' + 'x' * 100 + '.tar.gz'
            path = self.root / name
            path.touch()
            Path(str(path) + '.json').touch()
            self.entries.append(BackupEntry(path, 'db', '20261125T120000Z',
                                            12 * 1024 * 1024, '4.7.2'))

    def render(self, language, dimensions, keys):
        screen = FakeScreen(keys, dimensions=dimensions)
        menu = CursesMenu(screen, Translator(language))
        manager = MagicMock()
        manager.quick_list.return_value = self.entries
        manager.storage_breakdown.return_value = (100, 100, 800, 1000, len(self.entries))
        manager.describe_one.side_effect = lambda entry: entry
        with patch('netbox_ops.backups.BackupManager', return_value=manager), \
             patch('netbox_ops.tui.load_config'):
            menu._backups()
        manager.backup.assert_not_called()
        manager.restore.assert_not_called()
        manager.delete_backups.assert_not_called()
        manager.verify.assert_not_called()
        self.assertEqual(screen.wait_ms, -1)
        return screen.frames

    @staticmethod
    def find(frame, prefix):
        return next((y, x, text) for y, x, text, _ in frame if text.startswith(prefix))

    def test_wide_table_aligns_left_and_scrolls_with_fixed_header(self):
        for language in ('en', 'pt'):
            with self.subTest(language=language):
                t = Translator(language)
                frames = self.render(language, (36, 120), [curses.KEY_DOWN, curses.KEY_END, 27, 27])
                initial, focused, last = frames[:3]
                title_y = self.find(initial, t('nbo_backup_table'))[0]
                action_y = self.find(initial, t('nbo_backup_actions_label'))[0]
                detail_y = self.find(initial, t('nbo_detail_label'))[0]
                storage_y = self.find(initial, t('nbo_storage_label'))[0]
                self.assertEqual(title_y, 3)
                self.assertLess(title_y, action_y)
                self.assertLess(action_y, detail_y)
                self.assertEqual(storage_y, detail_y)
                header = next(text for y, x, text, _ in focused
                              if y == 4 and x == 4 and t('nbo_date') in text)
                row = self.find(focused, '[ ] db')[2]
                for column, value in (('type', 'db'), ('date', '2026-11-25 12:00'),
                                      ('size', '12.0 MiB'), ('tag_header', t('nbo_manual_short')),
                                      ('name', 'backup-00-')):
                    self.assertEqual(header.index(t('nbo_' + column)), row.index(value))
                self.assertTrue(any(text == header and y == 4 for y, _, text, _ in last))
                self.assertTrue(any('backup-24-' in text and 5 <= y < action_y
                                    for y, _, text, _ in last))
                self.assertTrue(any('backup-00-' in text and y > detail_y
                                    for y, _, text, _ in focused))
                self.assertTrue(any('backup-24-' in text and y > detail_y
                                    for y, _, text, _ in last))
                self.assertEqual(sum(text == '│' for _, _, text, _ in initial), 1)

    def test_minimum_terminal_preserves_list_and_full_filename(self):
        for language in ('en', 'pt'):
            with self.subTest(language=language):
                t = Translator(language)
                frames = self.render(language, (16, 50), [curses.KEY_DOWN, ord('i'), 27, 27, 27])
                focused, details = frames[1:3]
                action_y = self.find(focused, t('nbo_backup_actions_label')[:40])[0]
                detail_y = self.find(focused, t('nbo_detail_label'))[0]
                storage_y = self.find(focused, t('nbo_storage_label'))[0]
                self.assertTrue(any(text.startswith('[ ] db') and y < action_y
                                    for y, _, text, _ in focused))
                self.assertLess(action_y, detail_y)
                self.assertLess(detail_y, storage_y)
                self.assertLess(storage_y, 14)
                # The read-only detail viewer wraps without losing any part
                # of a filename, rather than relying on the truncated table.
                full_detail = ''.join(text for y, x, text, _ in details if x == 3 and 3 <= y < 13)
                self.assertIn(self.entries[0].path.name, full_detail)

    def test_select_all_remains_a_real_action_after_separator(self):
        frames = self.render('en', (30, 120),
                             [9] + [curses.KEY_RIGHT] * 4 + [10, 27, 27, 27])
        self.assertTrue(any(text.startswith('[x] db') for frame in frames
                            for _, _, text, _ in frame))
        self.assertTrue(any(text.startswith('[Select all]') and attr == curses.A_REVERSE
                            for frame in frames for _, _, text, attr in frame))


if __name__ == '__main__':
    unittest.main()
