"""Task progress is evidence of actual work, not a timeline inference."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from netbox_ops.backup_progress import (
    BackupProgress, BackupTask, BackupTaskEvent, batch_tasks, create_tasks, restore_tasks,
)
from netbox_ops.backups import BackupError, BackupManager
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class BackupProgressTests(unittest.TestCase):
    def test_creation_and_restore_plans_distinguish_conditional_work(self):
        for kind in ('db', 'media', 'combined'):
            tasks = {t.phase: t.applicable for t in create_tasks(kind)}
            self.assertEqual(tasks['dump_db'], kind != 'media')
            self.assertEqual(tasks['archive_media'], kind != 'db')
            self.assertEqual(tasks['stop_services'], kind == 'combined')
        for kind, only, extract, database in (
            ('db', 'all', False, True), ('media', 'all', True, False),
            ('combined', 'all', True, True), ('combined', 'db', True, True),
            ('combined', 'media', True, False), ('full', 'db', True, True),
            ('full', 'media', True, False), ('full', 'all', True, True),
        ):
            with self.subTest(kind=kind, only=only):
                tasks = {t.phase: t.applicable for t in restore_tasks(kind, only)}
                self.assertEqual(tasks['extract_media'], extract)
                self.assertEqual(tasks['stage_db'], database)
                self.assertEqual(tasks['verify_application'], database)

    def test_pending_running_failed_and_not_run_are_explicit(self):
        events, legacy = [], []
        tasks = (BackupTask('prepare'), BackupTask('verify'),
                 BackupTask('extract_media', applicable=False), BackupTask('replace'))
        tracker = BackupProgress(tasks, lambda *args: legacy.append(args), events.append)
        with self.assertRaisesRegex(BackupError, 'bad archive'):
            with tracker.operation():
                with tracker.step('prepare'):
                    pass
                with tracker.step('verify'):
                    raise BackupError('bad archive')
        self.assertEqual([e.state for e in events[:4]],
                         ['pending', 'pending', 'not_applicable', 'pending'])
        self.assertEqual([e.state for e in events if e.task.phase == 'verify'],
                         ['pending', 'running', 'failed'])
        self.assertEqual(events[-1].state, 'not_run')
        self.assertEqual(tracker.counts, (1, 3))
        self.assertFalse(any(event[0] == 'complete' for event in legacy))

    def test_skip_excludes_task_and_dynamic_cleanup_is_observed(self):
        events = []
        tracker = BackupProgress((BackupTask('stop_services'), BackupTask('replace')),
                                 events=events.append)
        with tracker.operation():
            tracker.start('stop_services')
            tracker.done('stop_services', skipped=True)
            with tracker.step('replace'):
                pass
            with tracker.step('cleanup_db'):
                pass
        self.assertEqual(tracker.counts, (2, 2))
        self.assertEqual([(e.task.phase, e.state) for e in events[-2:]],
                         [('cleanup_db', 'running'), ('cleanup_db', 'done')])

    def test_verification_failure_keeps_exact_file_states(self):
        manager = MagicMock()
        manager.verify.side_effect = [None, BackupError('corrupt')]
        entries = [SimpleNamespace(kind='db', path=Path(name))
                   for name in ('one.dump', 'two.dump', 'three.dump')]
        events = []
        with self.assertRaisesRegex(BackupError, 'corrupt'):
            CursesMenu._verify_job_many(manager, entries, events.append)
        self.assertEqual(manager.verify.call_count, 2)
        self.assertEqual({e.task.detail: e.state for e in events},
                         {'one.dump': 'done', 'two.dump': 'failed', 'three.dump': 'not_run'})
        self.assertEqual((events[-1].done, events[-1].total), (1, 3))

    def test_delete_failure_preserves_completed_and_unattempted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ('one.dump', 'two.dump', 'three.dump')]
            for path in paths:
                path.write_bytes(b'PGDMP')
            manager = BackupManager(Configuration(DEFAULT_UNITS.copy(), root))
            original = Path.unlink

            def unlink(path, *args, **kwargs):
                if path == paths[1]:
                    raise OSError('delete failed')
                return original(path, *args, **kwargs)

            events = []
            with (patch('netbox_ops.backups._require_root'),
                  patch('netbox_ops.backups.ExclusiveLock'),
                  patch.object(manager, '_owned_backup_path', side_effect=lambda path: path),
                  patch('netbox_ops.backups.write_audit'),
                  patch.object(Path, 'unlink', unlink)):
                with self.assertRaisesRegex(OSError, 'delete failed'):
                    manager.delete_backups(paths, task_progress=events.append)
            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())
            self.assertTrue(paths[2].exists())
            self.assertEqual({e.task.detail: e.state for e in events if e.task.phase == 'delete'},
                             {'one.dump': 'done', 'two.dump': 'failed', 'three.dump': 'not_run'})

    def test_tui_never_promotes_previous_running_task_on_next_event(self):
        for language in ('en', 'pt'):
            menu = CursesMenu(FakeScreen(dimensions=(30, 100)), Translator(language))
            first, second = batch_tasks('verify', ('one.dump', 'two.dump'))
            menu._record_backup_step(BackupTaskEvent(first, 'running', 0, 2))
            menu._record_backup_step(BackupTaskEvent(second, 'running', 0, 2))
            self.assertEqual(menu._backup_steps[0], ('verify', 'running', 'one.dump'))
            menu._record_backup_step(BackupTaskEvent(first, 'failed', 0, 2))
            menu._record_backup_step(BackupTaskEvent(second, 'done', 1, 2))
            self.assertEqual(menu._backup_steps,
                             [('verify', 'failed', 'one.dump'), ('verify', 'done', 'two.dump')])
            menu._draw_backup_progress('Verify', ('verify', 1, 2, ''), 0,
                                       finished=True, error='failed')
            frame = menu.stdscr.frames[-1]
            self.assertTrue(any(menu.t('nbo_task_state_failed') in text for _, _, text, _ in frame))
            self.assertTrue(any('1/2' in text for _, _, text, _ in frame))
            self.assertFalse(any('2/2' in text for _, _, text, _ in frame))
