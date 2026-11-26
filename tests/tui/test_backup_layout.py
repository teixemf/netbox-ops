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

    def test_empty_inventory_hides_headers_and_updates_on_refresh(self):
        for language in ('en', 'pt'):
            with self.subTest(language=language):
                t = Translator(language)
                screen = FakeScreen([curses.KEY_F5, 27], dimensions=(36, 120))
                menu = CursesMenu(screen, t)
                manager = MagicMock()
                manager.quick_list.side_effect = [[], self.entries[:1]]
                manager.storage_breakdown.return_value = (100, 100, 800, 1000, 999)
                with (patch('netbox_ops.backups.BackupManager', return_value=manager),
                      patch('netbox_ops.tui.load_config')):
                    menu._backups()
                empty, full = screen.frames[:2]
                self.assertTrue(any(y == 4 and text == t('nbo_backup_empty')
                                    for y, _, text, _ in empty))
                self.assertFalse(any(y == 4 and t('nbo_date') in text for y, _, text, _ in empty))
                self.assertTrue(any(y == 4 and t('nbo_date') in text for y, _, text, _ in full))
                self.assertTrue(any(text == t('nbo_backup_table') for _, _, text, _ in empty))

    def test_counts_from_inventory_include_alias_and_unknown_once(self):
        from dataclasses import replace

        menu = CursesMenu(FakeScreen(), Translator('en'))
        entries = [replace(self.entries[0], kind=kind)
                   for kind in ('db', 'media', 'full', 'combined', 'legacy', 'unexpected')]
        self.assertEqual(menu._backup_inventory_counts(entries),
                         [('Database', 1), ('Media', 1), ('Combined', 2),
                          ('Legacy', 1), ('Unclassified', 1), ('Total', 6)])
        self.assertEqual(menu._backup_inventory_counts([]),
                         [('Database', 0), ('Media', 0), ('Combined', 0), ('Total', 0)])

    def test_selected_counter_tracks_marks_not_focus_and_keeps_footer_clear(self):
        for language in ('en', 'pt'):
            for dimensions in ((16, 50), (36, 120)):
                with self.subTest(language=language, dimensions=dimensions):
                    t = Translator(language)
                    frames = self.render(language, dimensions,
                                         [curses.KEY_DOWN, 32, ord('*'), ord('*'), 27, 27])
                    for frame, count in zip(frames, (0, 0, 1, 25, 0)):
                        action_y = self.find(frame, t('nbo_backup_actions_label')[:40])[0]
                        detail_y = self.find(frame, t('nbo_detail_label'))[0]
                        labels = (f'{t("nbo_chosen")}: {count}',
                                  f'{t("nbo_inventory_selected_short")}: {count}')
                        self.assertTrue(any(action_y < y < detail_y - 1 and text in labels
                                            for y, _, text, _ in frame))
                        self.assertFalse(any(text in labels and attr == curses.A_REVERSE
                                             for _, _, text, attr in frame))

    def test_storage_details_show_counts_and_multi_selection_at_minimum_size(self):
        t = Translator('en')
        screen = FakeScreen([ord('*'), ord('b'), 27, 27], dimensions=(16, 50))
        menu = CursesMenu(screen, t)
        manager = MagicMock()
        manager.quick_list.return_value = self.entries[:2]
        manager.storage_breakdown.return_value = (100, 100, 800, 1000, 999)
        with (patch('netbox_ops.backups.BackupManager', return_value=manager),
              patch('netbox_ops.tui.load_config'),
              patch.object(menu, '_show_lines') as show):
            menu._backups()
        rows = [line for line, _ in show.call_args.args[1]]
        self.assertIn('Database : 2', rows)
        self.assertIn('Media    : 0', rows)
        self.assertIn('Combined : 0', rows)
        self.assertIn('Total    : 2', rows)
        self.assertIn('Selected: 2', rows)
        self.assertIn(t('nbo_multi_restore_disabled'), rows)
        self.assertNotIn('999', str(rows))
        manager.describe_one.assert_not_called()

    def test_disk_measurement_failure_does_not_hide_inventory_counts(self):
        screen = FakeScreen([27], dimensions=(36, 120))
        menu = CursesMenu(screen, Translator('en'))
        manager = MagicMock()
        manager.quick_list.return_value = self.entries[:2]
        manager.storage_breakdown.side_effect = OSError('Disk measurement failed')
        with (patch('netbox_ops.backups.BackupManager', return_value=manager),
              patch('netbox_ops.tui.load_config')):
            menu._backups()
        frame = screen.frames[-1]
        self.assertTrue(any(text == 'Disk measurement failed' for _, _, text, _ in frame))
        for count in ('Database : 2', 'Media    : 0', 'Combined : 0', 'Total    : 2'):
            self.assertTrue(any(text == count for _, _, text, _ in frame), count)

    def test_inventory_refresh_failure_is_not_reported_as_zero_and_recovers(self):
        screen = FakeScreen([curses.KEY_F5, curses.KEY_F5, 27], dimensions=(36, 120))
        menu = CursesMenu(screen, Translator('en'))
        manager = MagicMock()
        manager.quick_list.side_effect = [[], OSError('Inventory read failed'), self.entries[:1]]
        manager.storage_breakdown.return_value = (100, 100, 800, 1000, 999)
        with (patch('netbox_ops.backups.BackupManager', return_value=manager),
              patch('netbox_ops.tui.load_config'), patch.object(menu, '_show_lines')):
            menu._backups()
        empty, failed, recovered = screen.frames[:3]
        self.assertTrue(any(text == 'Total    : 0' for _, _, text, _ in empty))
        self.assertTrue(any(text == 'Inventory unavailable' for _, _, text, _ in failed))
        self.assertFalse(any(text.startswith('Total ') for _, _, text, _ in failed))
        self.assertTrue(any(text == 'Total    : 1' for _, _, text, _ in recovered))

    def test_select_all_remains_a_real_action_after_separator(self):
        frames = self.render('en', (30, 120),
                             [9] + [curses.KEY_RIGHT] * 4 + [10, 27, 27, 27])
        self.assertTrue(any(text.startswith('[x] db') for frame in frames
                            for _, _, text, _ in frame))
        self.assertTrue(any(text.startswith('[Select all]') and attr == curses.A_REVERSE
                            for frame in frames for _, _, text, attr in frame))


if __name__ == '__main__':
    unittest.main()
