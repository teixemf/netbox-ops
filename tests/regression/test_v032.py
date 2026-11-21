"""v0.3.2: correct version, explicit journals, multi-backup operations and progress."""
from __future__ import annotations

import curses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.netbox_version import read_netbox_version
from netbox_ops.services import ServiceVersionCache, ServiceState
from netbox_ops.tui import CursesMenu
from netbox_ops.i18n import Translator
from netbox_ops.backups import BackupManager, BackupEntry, BackupError
from netbox_ops.config import Configuration, DEFAULT_UNITS
from tests.helpers.fake_screen import FakeScreen

class V032Tests(unittest.TestCase):
    def test_netbox_version_from_release_yaml_not_django(self):
        with tempfile.TemporaryDirectory() as d:
            release=Path(d)/'release.yaml'
            release.write_text('version: "4.4.1"\nname: NetBox\n')
            self.assertEqual(read_netbox_version(release),'4.4.1')
            release.write_text('name: Django\n')
            self.assertEqual(read_netbox_version(release),'')

    def test_version_cache_reports_release_and_not_manage_version(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess([],0,'5.2.9\n',''))
        with patch('netbox_ops.services.read_netbox_version',return_value='4.4.1') as source:
            cache=ServiceVersionCache(runner=runner)
            cache.request('netbox')
            cache.request('netbox-rq')
            cache._futures['netbox'].result(timeout=2)
            self.assertEqual(cache.get('netbox-rq'),'4.4.1')
            source.assert_called_once()
            runner.assert_not_called()
            cache._executor.shutdown(wait=True)

    def test_detail_loads_logs_only_after_l_and_count_after_n(self):
        state=ServiceState('netbox','netbox.service','loaded','active','running','enabled','42',0,1024,1)
        menu=CursesMenu(FakeScreen([ord('l'),27,27],dimensions=(28,125)),Translator('pt'))
        menu.versions.request=MagicMock()
        menu.versions.get=MagicMock(return_value='4.4.1')
        systemd=MagicMock()
        systemd.status.return_value=[state]
        systemd.logs.return_value=['a journal line']
        menu._detail(systemd,state)
        systemd.logs.assert_called_once_with('netbox',20)
        self.assertIn('a journal line',' '.join(x[2] for x in menu.stdscr.writes))
        self.assertIn('4.4.1',' '.join(x[2] for x in menu.stdscr.writes))

    def test_disk_donut_labels_three_categories(self):
        screen=FakeScreen(dimensions=(40,150))
        menu=CursesMenu(screen,Translator('en'))
        menu._draw_storage_ring(5,10,(100,400,500,1000,5))
        dots=[row for row in screen.writes if len(row[2]) == 1 and 0x2800 <= ord(row[2]) <= 0x28ff]
        self.assertGreater(len(dots),40)
        self.assertIn('10.0%', ' '.join(row[2] for row in screen.writes))

    def test_progress_lists_tasks_and_status(self):
        screen=FakeScreen(dimensions=(30,130))
        m=CursesMenu(screen,Translator('en'))
        m._service_steps=[]
        m._draw_progress('restart',0,2,'netbox','running','')
        m._draw_progress('restart',1,2,'netbox','done','active')
        m._draw_progress('restart',1,2,'redis','running','')
        self.assertEqual([(x[0],x[1]) for x in m._service_steps],
                         [('netbox','done'),('redis','running')])
        m._backup_steps=[]
        m._record_backup_step(('prepare',0,3,''))
        m._record_backup_step(('dump_db',1,3,''))
        self.assertEqual(m._backup_steps[0][1],'done')
        self.assertEqual(m._backup_steps[1][1],'running')
        m._draw_backup_progress('Backup',('dump_db',1,3,''),1)
        self.assertIn('TASKS', ' '.join(x[2] for x in screen.writes))
        m._backup_steps=[]
        m._backup_started_counts={}
        m._record_backup_step(('verify',0,2,'first.dump'))
        m._record_backup_step(('verify',1,2,'first.dump'))
        m._record_backup_step(('verify',1,2,'second.dump'))
        self.assertEqual([(x[1],x[2]) for x in m._backup_steps],
                         [('done','first.dump'),('running','second.dump')])

    def test_backup_multiselect_space_and_all_are_nondestructive(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            backup_dir=root/'backups'
            backup_dir.mkdir(mode=0o700)
            for i in range(3):
                (backup_dir/f'netbox-db-20261120T11000{i}Z.dump').write_bytes(b'PGDMP')
            cfg=Configuration(DEFAULT_UNITS.copy(),backup_dir)
            manager=BackupManager(cfg)
            screen=FakeScreen([curses.KEY_DOWN,32,curses.KEY_DOWN,32,ord('*'),27,27,27],dimensions=(35,135))
            menu=CursesMenu(screen,Translator('pt'))
            with patch('netbox_ops.tui.load_config',return_value=cfg),\
                 patch('netbox_ops.backups.BackupManager',return_value=manager),\
                 patch.object(manager,'delete_backups') as deletion, \
                 patch.object(manager,'verify') as verification:
                menu._backups()
            deletion.assert_not_called()
            verification.assert_not_called()
            output=' '.join(row[2] for row in screen.writes)
            self.assertIn('Seleccionados',output)

    def test_bulk_menu_delete_uses_only_explicitly_marked_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            folder=root/'backups'
            folder.mkdir(mode=0o700)
            for i in range(3):
                (folder/f'netbox-db-20261120T13000{i}Z.dump').write_bytes(b'PGDMP')
            manager=BackupManager(Configuration(DEFAULT_UNITS.copy(),folder))
            items=manager.quick_list()
            menu=CursesMenu(FakeScreen([curses.KEY_DOWN,32,curses.KEY_DOWN,32,ord('d'),
                                       27,27,27],dimensions=(33,130)),Translator('pt'))
            calls=[]
            def run_job(title,worker):
                calls.append((title,worker))
                worker(lambda *args: None)
                return True
            with patch('netbox_ops.tui.load_config'), \
                 patch('netbox_ops.backups.BackupManager',return_value=manager), \
                 patch.object(menu,'_confirm_token',return_value=True) as confirmation, \
                 patch.object(menu,'_run_backup_job',side_effect=run_job), \
                 patch.object(manager,'delete_backups',return_value=(2,8192)) as deletion:
                menu._backups()
            confirmation.assert_called_once()
            self.assertEqual(confirmation.call_args.args[2],'DELETE 2 BACKUPS')
            deletion.assert_called_once()
            selected=deletion.call_args.args[0]
            self.assertEqual(selected,[items[0].path,items[1].path])
            self.assertTrue(items[2].path.exists())

    def test_bulk_menu_verify_selection_and_select_all(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            folder=root/'backups'
            folder.mkdir(mode=0o700)
            for i in range(3):
                (folder/f'netbox-db-20261120T12000{i}Z.dump').write_bytes(b'PGDMP')
            manager=BackupManager(Configuration(DEFAULT_UNITS.copy(),folder))
            menu=CursesMenu(FakeScreen([ord('*'),ord('v'),27,27],dimensions=(33,130)),Translator('en'))
            def run_job(title,worker):
                worker(lambda *args: None)
                return True
            with patch('netbox_ops.tui.load_config'), \
                 patch('netbox_ops.backups.BackupManager',return_value=manager), \
                 patch.object(menu,'_run_backup_job',side_effect=run_job), \
                 patch.object(manager,'verify',return_value={'status':'ok'}) as verification:
                menu._backups()
            self.assertEqual(verification.call_count,3)
            self.assertEqual({call.args[1] for call in verification.call_args_list},
                             {e.path for e in manager.quick_list()})

    def test_pre031_versions_not_displayed_as_netbox_release(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'backup'
            folder.mkdir(mode=0o700)
            source=folder/'sample.dump'
            source.write_bytes(b'PGDMPmock')
            meta={'kind':'db','netbox_version':'5.2.9','tool':'netbox-ops','format':1,
                  'created_utc':'2026-11-20T10:00:00+00:00'}
            Path(str(source)+'.json').write_text(json.dumps(meta))
            manager=BackupManager(Configuration(DEFAULT_UNITS.copy(),folder))
            entry=manager.describe_one(manager.quick_list()[0])
            self.assertEqual(entry.version,'')
            from netbox_ops.backups import _version_guard,BackupError
            with patch('netbox_ops.backups._version',return_value='4.4.1'):
                with self.assertRaises(BackupError):
                    _version_guard(meta,accept_unknown=False,allow_mismatch=False)
                _version_guard(meta,accept_unknown=True,allow_mismatch=False)

    def test_selected_bulk_delete_preflight_rejects_external_and_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            backup_dir=root/'backup'
            backup_dir.mkdir(mode=0o700)
            good=backup_dir/'a.dump'
            good.write_bytes(b'PGDMPmock')
            outside=root/'outside.dump'
            outside.write_bytes(b'untouched')
            cfg=Configuration(DEFAULT_UNITS.copy(),backup_dir)
            manager=BackupManager(cfg)
            with patch('netbox_ops.backups._require_root'),patch('netbox_ops.backups.ExclusiveLock'):
                with self.assertRaises(BackupError):
                    manager.delete_backups([good,outside])
                with self.assertRaises(BackupError):
                    manager.delete_backups([good,good])
            self.assertTrue(good.exists())
            self.assertTrue(outside.exists())

    def test_selected_verification_reports_each_file(self):
        entries=[BackupEntry(Path('/tmp/a.dump'),'db','',1,''),BackupEntry(Path('/tmp/b.dump'),'db','',1,'')]
        manager=MagicMock()
        events=[]
        result=CursesMenu._verify_job_many(manager,entries,lambda *args:events.append(args))
        self.assertEqual(manager.verify.call_count,2)
        self.assertEqual(events[-1][0],'complete')
        self.assertEqual(events[-1][1:],(2,2,''))
        self.assertIn('2 backups',result)

if __name__=='__main__':
    unittest.main()
