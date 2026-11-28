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
                self.assertEqual(any(text == menu.t('nbo_stage_' + task.phase)
                                     for _, _, text, _ in frame), task.applicable)
                if task.applicable:
                    y = next(y for y, x, text, _ in frame
                             if menu._panels[1][1] < y < menu._panels[1][3] and x > 3
                             and text == menu.t('nbo_stage_' + task.phase))
                    self.assertTrue(any(row == y and x == 3 and text.strip() == '○'
                                        for row, x, text, _ in frame))
            self.assertNotIn('N/A', ' '.join(text for _, _, text, _ in frame))
            self.assertNotIn(menu.t('nbo_task_state_pending'), ' '.join(text for _, _, text, _ in frame))
            self.assertNotIn(menu.t('nbo_backup_result_pending'), ' '.join(text for _, _, text, _ in frame))
            self.assertNotIn(menu.t('nbo_job_success'), [text for _, _, text, _ in frame])

    def test_running_marker_pulse_changes_only_marker_and_stops_on_completion(self):
        for language in LANGUAGES:
            menu, screen = self.menu(language, (40, 120))
            menu.colors.update(title=11, title_panel=12, pulse_low=21, pulse_low_panel=22,
                               pulse_high=31, pulse_high_panel=32, pulse_peak=41, pulse_peak_panel=42)
            for phase, state in (('prepare', 'running'), ('verify', 'pending')):
                menu._record_backup_step(BackupTaskEvent(BackupTask(phase), state, 0, 2))
            frames = []
            for instant in (0.2, 0.6, 1.0, 1.4, 1.8, 2.2, 2.6):
                with patch('netbox_ops.tui.time.monotonic', return_value=instant):
                    menu._draw_backup_progress('Restore', ('prepare', 0, 2, ''), 0)
                frames.append(screen.frames[-1])
            self.assertEqual([next(attr for _, _, text, attr in frame if text == '●') for frame in frames],
                             [22, 12, 32, 42, 32, 12, 22])
            def static(frame):
                return [entry for entry in frame if entry[2] != '●']
            self.assertTrue(all(static(frame) == static(frames[0]) for frame in frames))
            screen.dimensions = (16, 50)
            menu._draw_backup_progress('Restore', ('prepare', 0, 2, ''), 0)
            self.assertTrue(any(text == '●' for _, _, text, _ in screen.frames[-1]))
            menu._record_backup_step(BackupTaskEvent(BackupTask('prepare'), 'done', 1, 2))
            menu._draw_backup_progress('Restore', ('prepare', 1, 2, ''), 0)
            self.assertFalse(any(text == '●' for _, _, text, _ in screen.frames[-1]))
            self.assertTrue(any(text.startswith('● ' + menu.t('nbo_task_state_done'))
                                for _, _, text, _ in screen.frames[-1]))
            menu._record_backup_step(BackupTaskEvent(BackupTask('verify'), 'failed', 1, 2))
            menu._draw_backup_progress('Restore', ('verify', 1, 2, ''), 0, finished=True, error='failed')
            self.assertFalse(any(text == '●' for _, _, text, _ in screen.frames[-1]))
            self.assertTrue(any(text.startswith('● ' + menu.t('nbo_task_state_failed'))
                                for _, _, text, _ in screen.frames[-1]))

    def test_running_marker_is_static_without_full_palette(self):
        for colors in ({}, {'title': 1}, {'pulse_low': 2, 'pulse_high': 3}):
            menu, screen = self.menu('en', (30, 100))
            menu.colors.update(colors)
            menu._record_backup_step(BackupTaskEvent(BackupTask('prepare'), 'running', 0, 1))
            menu._draw_backup_progress('Backup', ('prepare', 0, 1, ''), 0)
            self.assertFalse(any(text == '●' for _, _, text, _ in screen.frames[-1]))
            self.assertTrue(any(text.startswith('● Running') for _, _, text, _ in screen.frames[-1]))

    def test_task_states_preserve_failure_skipped_and_unrun(self):
        for language in LANGUAGES:
            for dimensions in ((16, 50), (40, 120)):
                menu, screen = self.menu(language, dimensions)
                for phase, state in (('prepare', 'done'), ('verify', 'failed'),
                                     ('extract_media', 'not_applicable'), ('stage_db', 'skipped'),
                                     ('finalize', 'pending')):
                    menu._record_backup_step(BackupTaskEvent(BackupTask(phase), state, 1, 4))
                menu._draw_backup_progress('Restore', ('verify', 1, 4, ''), 0,
                                           finished=True, error='failure')
                frames = [screen.frames[-1]]
                for key in (curses.KEY_HOME, curses.KEY_END):
                    menu._scroll_backup_progress(key, finished=True)
                    menu._draw_backup_progress('Restore', ('verify', 1, 4, ''), 0,
                                               finished=True, error='failure')
                    frames.append(screen.frames[-1])
                text = ' '.join(value for frame in frames for _, _, value, _ in frame)
                for state in ('done', 'failed', 'skipped', 'not_run'):
                    self.assertIn(menu.t('nbo_task_state_' + state), text)
                self.assertNotIn(menu.t('nbo_stage_extract_media'), text)
                self.assertNotIn('○', text)
                self.assertIn('●', text)
                self.assertEqual(menu._backup_steps[-1][1], 'pending')

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
