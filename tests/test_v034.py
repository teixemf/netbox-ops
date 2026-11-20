"""v0.3.4: backup inventory has a single detail pane and bottom actions."""
from __future__ import annotations

import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops import __version__
from netbox_ops.backups import BackupManager
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu
from test_tui import FakeScreen


class BackupView034Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'backups'
        self.directory.mkdir()
        self.files = []
        for i in range(3):
            path = self.directory / f'netbox-db-20261120T13000{i}Z-{i}.dump'
            path.write_bytes(b'PGDMP' + bytes([i]))
            meta = {'kind': 'db', 'netbox_version_source': 'release.yaml',
                    'netbox_version': '4.4.1', 'created_utc': '2026-11-20T13:00:00+00:00'}
            Path(str(path) + '.json').write_text(json.dumps(meta), encoding='utf-8')
            self.files.append(path)
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.directory)
        self.manager = BackupManager(self.cfg)

    def menu(self, keys, *, lang='pt', dimensions=(40, 150)):
        screen = FakeScreen(keys, dimensions=dimensions)
        menu = CursesMenu(screen, Translator(lang))
        return menu, screen

    def run_menu(self, menu):
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), \
             patch('netbox_ops.backups.BackupManager', return_value=self.manager):
            menu._backups()

    def test_version(self):
        self.assertEqual(__version__, '0.3.6')

    def test_action_bar_single_row_wide_terminal_and_wraps_narrow(self):
        labels = [Translator('pt')('nbo_act_' + action) for action in
                  ('create', 'restore', 'verify', 'delete',
                   'select_all', 'refresh')]
        wide = CursesMenu._backup_action_positions(labels, 145)
        self.assertEqual({y for x, y in wide}, {0})
        self.assertEqual(len(wide), 6)
        narrow = CursesMenu._backup_action_positions(labels, 45)
        self.assertGreater(max(y for _, y in narrow), 0)
        for i, (x, y) in enumerate(narrow):
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + len(labels[i]) + 2, 45)
            if i and y == narrow[i - 1][1]:
                self.assertGreater(x, narrow[i - 1][0])

    def test_detail_appears_in_right_panel_without_enter_popup(self):
        menu, screen = self.menu([curses.KEY_DOWN, 10, 27, 27])
        with patch.object(menu, '_show_lines') as popup:
            self.run_menu(menu)
        popup.assert_not_called()
        shown = ' '.join(s for y, x, s, style in screen.writes)
        self.assertIn('4.4.1', shown)
        self.assertIn(self.files[2].name, shown)
        self.assertIn('Conteúdo:', shown)
        self.assertIn('Apenas BD', shown)

    def test_multiple_marked_shows_summary_and_restore_disabled(self):
        menu, screen = self.menu([curses.KEY_DOWN, 32,
                                  curses.KEY_DOWN, 32,
                                  9, curses.KEY_RIGHT, curses.KEY_RIGHT, 10,
                                  27, 27, 27, 27])
        with patch.object(menu, '_choose') as choose, \
             patch.object(menu, '_confirm_token') as confirm, \
             patch.object(menu, '_show_lines') as popup:
            self.run_menu(menu)
        choose.assert_not_called()
        confirm.assert_not_called()
        popup.assert_called_once()
        self.assertIn('um backup', str(popup.call_args).lower())
        shown = ' '.join(s for y, x, s, style in screen.writes)
        self.assertIn('SELECÇÃO MÚLTIPLA', shown)
        self.assertIn('Restauro indisponível', shown)
        self.assertIn('Seleccionados: 2', shown)
        self.assertIn('Tamanho total', shown)

    def test_tab_enter_runs_button_not_detail(self):
        menu, screen = self.menu([9, 10, 27, 27], lang='en')
        with patch.object(menu, '_choose', return_value=None) as choose, \
             patch.object(menu, '_show_lines') as popup, \
             patch.object(menu, '_run_backup_job') as job:
            self.run_menu(menu)
        choose.assert_called_once()
        popup.assert_not_called()
        job.assert_not_called()
        self.assertIn('Create backup', choose.call_args.args[0])

    def test_select_all_then_tab_verify_executes_each_file(self):
        menu, screen = self.menu([ord('*'), 9, curses.KEY_RIGHT,
                                  10, 27, 27, 27], lang='en')
        def no_side_effect(title, job):
            job(lambda *args: None)
            return True
        with patch.object(menu, '_run_backup_job', side_effect=no_side_effect), \
             patch.object(self.manager, 'verify', return_value={}) as verify:
            self.run_menu(menu)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual({call.args[1] for call in verify.call_args_list}, set(self.files))
        self.assertIn('MULTIPLE BACKUPS', ' '.join(t for _, _, t, _ in screen.writes))

    def test_marked_backup_remains_preview_target_after_cursor_moves(self):
        menu, screen = self.menu([curses.KEY_DOWN, 32, curses.KEY_DOWN,
                                  27, 27, 27])
        self.run_menu(menu)
        # The second row is highlighted, but the one marked file remains
        # the preview and the eventual restore target.
        rendered = ' '.join(text for _, _, text, _ in screen.frames[3])
        self.assertIn(self.files[2].name, rendered)

    def test_tab_enter_delete_targets_only_marked_files(self):
        menu, screen = self.menu([curses.KEY_DOWN, 32, curses.KEY_DOWN, 32,
                                  9, curses.KEY_RIGHT, curses.KEY_RIGHT,
                                  curses.KEY_RIGHT, 10, 27, 27, 27, 27])
        calls = []
        def perform(title, worker):
            calls.append(title)
            worker(lambda *args: None)
            return True
        with patch.object(menu, '_confirm_token', return_value=True) as confirm, \
             patch.object(menu, '_run_backup_job', side_effect=perform), \
             patch.object(self.manager, 'delete_backups', return_value=(2, 12)) as delete:
            self.run_menu(menu)
        self.assertEqual(len(calls), 1)
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.args[2], 'DELETE 2 BACKUPS')
        delete.assert_called_once()
        self.assertEqual(set(delete.call_args.args[0]), set(self.files[1:]))

    def test_refresh_action_on_bar_reloads_inventory_once(self):
        keys = [9, *(curses.KEY_RIGHT for _ in range(5)), 10, 27, 27]
        menu, _ = self.menu(keys)
        with patch.object(self.manager, 'quick_list', wraps=self.manager.quick_list) as ls:
            self.run_menu(menu)
        self.assertEqual(ls.call_count, 2)

    def test_single_marked_restore_enter_reaches_confirmation_not_popup(self):
        menu, screen = self.menu([curses.KEY_DOWN, 32, 9, curses.KEY_RIGHT, curses.KEY_RIGHT, 10,
                                  27, 27, 27, 27])
        with patch.object(menu, '_confirm_token', return_value=False) as confirm, \
             patch.object(menu, '_run_backup_job') as job, \
             patch.object(menu, '_show_lines') as popup:
            self.run_menu(menu)
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual(confirm.call_args.args[2], 'RESTORE DB')
        job.assert_not_called()
        popup.assert_not_called()

    def test_narrow_terminal_still_displays_action_bar(self):
        menu, screen = self.menu([curses.KEY_DOWN, 27, 27],
                                  dimensions=(21, 72))
        self.run_menu(menu)
        contents = ' '.join(text for _, _, text, _ in screen.frames[-2])
        self.assertIn('OPERAÇÕES SOBRE BACKUPS', contents)
        self.assertIn('[Criar]', contents)
        self.assertIn('[Actualizar]', contents)
        self.assertTrue(all(0 <= y < 21 for y, x, text, attr in screen.frames[-2]))

    def test_metadata_cached_per_file_and_cursor_changes_do_not_verify(self):
        menu, screen = self.menu([curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP,
                                  curses.KEY_DOWN, 27, 27])
        with patch.object(self.manager, 'describe_one', wraps=self.manager.describe_one) as load, \
             patch.object(self.manager, 'verify') as verify:
            self.run_menu(menu)
        self.assertEqual(load.call_count, 2)
        verify.assert_not_called()

    def test_legacy_restore_is_disabled(self):
        path = self.directory / 'netbox-files-20261120T120000Z.files.tar.gz'
        path.write_bytes(b'legacy')
        menu, screen = self.menu([curses.KEY_UP, 9, curses.KEY_RIGHT, curses.KEY_RIGHT, 10,
                                  27, 27, 27])
        with patch.object(menu, '_choose') as choose, \
             patch.object(menu, '_confirm_token') as confirm, \
             patch.object(menu, '_show_lines') as show:
            self.run_menu(menu)
        show.assert_called_once()
        choose.assert_not_called()
        confirm.assert_not_called()

    def test_english_and_portuguese_translations_available(self):
        required = ('nbo_backup_hint_034', 'nbo_multi_title', 'nbo_contents',
                    'nbo_kind_full', 'nbo_kind_contents_media', 'nbo_multi_restore_disabled')
        for lang in ('pt', 'en'):
            t = Translator(lang)
            for key in required:
                with self.subTest(lang=lang, key=key):
                    self.assertNotEqual(t(key), f'<{key}>')


if __name__ == '__main__':
    unittest.main()
