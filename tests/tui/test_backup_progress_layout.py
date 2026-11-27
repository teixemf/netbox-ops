"""Backup task/result layouts, scrolling and operation acknowledgement."""
import curses
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backup_progress import BackupTask, BackupTaskEvent, restore_tasks
from netbox_ops.backups import BackupReport
from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class BackupProgressLayoutTests(unittest.TestCase):
    def menu(self, language, dimensions):
        screen = FakeScreen(dimensions=dimensions)
        return CursesMenu(screen, Translator(language)), screen

    def test_planned_tasks_use_three_content_sized_panels_in_both_languages(self):
        for language in LANGUAGES:
            menu, screen = self.menu(language, (40, 120))
            tasks = restore_tasks('db', 'all')
            for task in tasks:
                menu._record_backup_step(BackupTaskEvent(
                    task, 'pending' if task.applicable else 'not_applicable', 0, 9))
            menu._draw_backup_progress('Restore', ('prepare', 0, 9, ''), 0)
            frame = screen.frames[-1]
            headings = [next(y for y, _, text, _ in frame if text == menu.t(key))
                        for key in ('nbo_progress_heading', 'nbo_task_header', 'nbo_result_heading')]
            self.assertEqual(headings, sorted(headings))
            self.assertEqual(len(menu._panels), 3)
            self.assertEqual(menu._panels[1][1] - menu._panels[0][3], 1)
            self.assertEqual(menu._panels[2][1] - menu._panels[1][3], 1)
            self.assertLess(menu._panels[2][3], 38)
            for task in tasks:
                self.assertTrue(any(text == menu.t('nbo_stage_' + task.phase)
                                    for _, _, text, _ in frame))
            extraction = next(y for y, _, text, _ in frame
                              if text == menu.t('nbo_stage_extract_media'))
            self.assertTrue(any(y == extraction and text.strip() == '● N/A'
                                for y, _, text, _ in frame))
            self.assertTrue(any(text == menu.t('nbo_backup_result_pending') for _, _, text, _ in frame))
            self.assertNotIn(menu.t('nbo_job_success'), [text for _, _, text, _ in frame])

    def test_compact_scroll_and_resize_preserve_all_task_and_result_content(self):
        for language in LANGUAGES:
            menu, screen = self.menu(language, (16, 50))
            for index in range(18):
                task = BackupTask('verify', f'file-{index:02}.dump')
                menu._record_backup_step(BackupTaskEvent(task, 'done', 18, 18))
            long_path = '/backups/' + 'x' * 100 + '.dump'
            result = BackupReport(Path(long_path), 'db',
                                  'Previous database preserved: nbo_previous\n'
                                  'Redis cache and RQ data were preserved; check cache coherency.')
            def draw():
                menu._draw_backup_progress('Verify', ('complete', 18, 18, ''), 0,
                                           finished=True, result=result)
            draw()
            menu._scroll_backup_progress(curses.KEY_END, finished=True)
            draw()
            self.assertIn('file-17.dump', ' '.join(t for _, _, t, _ in screen.frames[-1]))
            menu._scroll_backup_progress(9, finished=True)
            menu._scroll_backup_progress(curses.KEY_END, finished=True)
            draw()
            self.assertTrue(any(text.startswith('> ' + menu.t('nbo_result_heading'))
                                for _, _, text, _ in screen.frames[-1]))
            self.assertIn('coherency.', ' '.join(t for _, _, t, _ in screen.frames[-1]))
            self.assertEqual(''.join(menu._backup_wrap(long_path, 44)), long_path)
            for frame in screen.frames:
                self.assertFalse(any(3 <= y < 14 and x + len(text) > 49
                                     for y, x, text, _ in frame))
            screen.dimensions = (45, 140)
            draw()
            self.assertTrue(any('Previous database preserved' in text for _, _, text, _ in screen.frames[-1]))
            self.assertEqual(menu._backup_result_offset, 0)
            self.assertEqual(screen.frames[-1][-1][3], 0)  # monochrome fallback

    def test_initial_plan_is_rendered_before_worker_and_acknowledgement_is_required(self):
        menu, screen = self.menu('en', (30, 100))
        tasks = (BackupTask('prepare'), BackupTask('verify'))
        screen.keys = iter([ord('q'), curses.KEY_DOWN, 10])
        pool = MagicMock()

        def submit(fn, callback):
            self.assertTrue(any(text == 'Preparation' for _, _, text, _ in screen.frames[0]))
            self.assertTrue(any(text == 'Checking integrity' for _, _, text, _ in screen.frames[0]))
            future = Future()
            future.set_result(fn(callback))
            return future

        pool.submit.side_effect = submit
        pool.__enter__.return_value = pool

        def worker(callback):
            for index, task in enumerate(tasks, 1):
                callback(BackupTaskEvent(task, 'running', index - 1, len(tasks)))
                callback(BackupTaskEvent(task, 'done', index, len(tasks)))
            return 'backup.dump'

        with patch('netbox_ops.tui.concurrent.futures.ThreadPoolExecutor', return_value=pool):
            self.assertTrue(menu._run_backup_job('Verify', worker, tasks=tasks))
        self.assertGreaterEqual(len(screen.frames), 4)
        self.assertEqual(screen.wait_ms, -1)
        self.assertTrue(any('backup.dump' in text for _, _, text, _ in screen.frames[-1]))
