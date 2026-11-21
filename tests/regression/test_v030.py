"""v0.3.0 scope regression tests; do not touch real PostgreSQL or systemd."""
from __future__ import annotations

import curses
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupEntry, BackupError, BackupManager, BackupReport
from netbox_ops.cli import build_parser, dispatch
from netbox_ops.config import Configuration, DEFAULT_UNITS, load_config
from netbox_ops.i18n import Translator
from netbox_ops.services import SystemdServices
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class NewBackupTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.file = self.media / 'upload.bin'
        self.file.write_bytes(b'original upload')
        self.secret = self.root / 'ldap_config.py'
        self.secret.write_text('LDAP_PASSWORD="do not back up"')
        self.config = self.root / 'configuration.py'
        self.config.write_text('SECRET_KEY="do not back up"')
        self.backups = self.root / 'backups'
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.backups, 'netbox', 'netbox', self.media)
        self.manager = BackupManager(self.cfg)
        self.sys = MagicMock()
        self.sys.status.return_value = []

    def patch_io(self):
        return (
            patch('netbox_ops.backups.SystemdServices', return_value=self.sys),
            patch('netbox_ops.backups._version', return_value='4.4.1'),
            patch('netbox_ops.backups.write_audit'),
            patch('netbox_ops.backups._django_check'),
            patch('netbox_ops.backups._db_dump', side_effect=lambda cfg, target:
                  target.write_bytes(b'PGDMP fake dump')),
        )

    def test_media_excludes_secrets_and_can_restore(self):
        with patch('netbox_ops.backups.write_audit'), patch('netbox_ops.backups._version', return_value='4.4.1'):
            archive = self.manager.backup('media').path
        with tarfile.open(archive, 'r:gz') as f:
            self.assertEqual({x.name for x in f if x.isfile() and x.name!='manifest.json'},
                             {'payload/media/upload.bin'})
        self.file.write_bytes(b'altered')
        self.config.write_text('SECRET_KEY="preserve this"')
        patches = self.patch_io()
        with patches[0], patches[1], patches[2], patches[3]:
            self.manager.restore('media', archive)
        self.assertEqual(self.file.read_bytes(), b'original upload')
        self.assertIn('preserve this', self.config.read_text())
        self.assertIn('do not back up', self.secret.read_text())

    def test_legacy_config_includes_only_media(self):
        config = self.root / 'legacy.json'
        config.write_text(json.dumps({'backup': {
            'directory': str(self.backups),
            'artifacts': [
                {'id':'configuration','path':str(self.config),'required':True},
                {'id':'ldap','path':str(self.secret),'required':False},
                {'id':'media','path':str(self.media),'required':False},
            ]}}))
        loaded = load_config(config)
        self.assertEqual(loaded.artifacts, (('media',self.media,True),))
        self.assertEqual(loaded.media_directory, self.media)
        with patch('netbox_ops.backups.write_audit'):
            archive = BackupManager(loaded).backup('media').path
        self.assertNotIn(b'LDAP_PASSWORD', archive.read_bytes())
        with tarfile.open(archive,'r:gz') as f:
            self.assertEqual({x['id'] for x in json.load(f.extractfile('manifest.json'))['artifacts']}, {'media'})

    def test_full_restore_only_media_never_stages_database(self):
        patches = self.patch_io()
        with patches[0], patches[1], patches[2], patches[4]:
            full = self.manager.backup('full').path
        self.file.write_bytes(b'changed')
        with patches[0], patches[1], patches[2], patches[3], \
             patch('netbox_ops.backups._preflight_db') as preflight, \
             patch('netbox_ops.backups._stage_db') as stage, \
             patch('netbox_ops.backups._rename_db') as rename:
            restored = self.manager.restore('full', full, only='media')
        self.assertEqual(restored.kind,'full')
        self.assertEqual(self.file.read_bytes(), b'original upload')
        preflight.assert_not_called()
        stage.assert_not_called()
        rename.assert_not_called()
        self.assertEqual(len(self.manager.list_backups('db')),0)

    def test_full_restore_only_db_never_replaces_media(self):
        patches = self.patch_io()
        with patches[0], patches[1], patches[2], patches[4]:
            full = self.manager.backup('full').path
        self.file.write_bytes(b'changed')
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._preflight_db'), \
             patch('netbox_ops.backups._stage_db',side_effect=lambda cfg, p: 'nbo_stage_fake' if p.is_file() else (_ for _ in ()).throw(AssertionError('Staged dump missing'))), \
             patch('netbox_ops.backups._rename_db') as rename, \
             patch('netbox_ops.backups._terminate'), \
             patch('netbox_ops.backups._replace_artifacts') as replace:
            restored = self.manager.restore('full',full, only='db')
        self.assertIn('Previous database',restored.detail)
        self.assertEqual(self.file.read_bytes(), b'changed')
        self.assertEqual(rename.call_count,2)
        replace.assert_not_called()
        self.assertEqual(len(self.manager.list_backups('media')),0)

    def test_full_restore_all_combines_db_and_media(self):
        patches = self.patch_io()
        with patches[0], patches[1], patches[2], patches[4]:
            full = self.manager.backup('full').path
        self.file.write_bytes(b'changed')
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._preflight_db'), \
             patch('netbox_ops.backups._stage_db',side_effect=lambda cfg, p: 'nbo_stage_fake' if p.is_file() else (_ for _ in ()).throw(AssertionError('Staged dump missing'))), \
             patch('netbox_ops.backups._rename_db') as rename, \
             patch('netbox_ops.backups._terminate'):
            self.manager.restore('full',full,only='all')
        self.assertEqual(self.file.read_bytes(),b'original upload')
        self.assertEqual(rename.call_count,2)
        self.assertEqual(len(self.manager.list_backups('media')),1)
        self.assertEqual(len(self.manager.list_backups('db')),1)

    def test_rejects_unsupported_partial_restore(self):
        with self.assertRaisesRegex(BackupError,'Partial restore'):
            self.manager.restore('media',self.root/'not-existent',only='db')

    def test_delete_single_includes_sidecar_and_rejects_external_files(self):
        with patch('netbox_ops.backups._db_dump',side_effect=lambda cfg,d:d.write_bytes(b'PGDMP fake')), \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups._version',return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            db = self.manager.backup('db').path
            sidecar = Path(str(db)+'.json')
            self.assertTrue(sidecar.exists())
            with self.assertRaisesRegex(BackupError,'local'):
                self.manager.delete_backup(self.config)
            self.assertTrue(self.config.exists())
            deleted = self.manager.delete_backup(db)
            self.assertGreater(deleted,0)
            self.assertFalse(sidecar.exists())
            self.assertFalse(db.exists())

    def test_delete_selected_does_not_delete_unrecognised_files(self):
        with patch('netbox_ops.backups.write_audit'):
            saved=self.manager.backup('media').path
            strange = self.backups/'important-unrelated.txt'
            strange.write_text('keep')
            count, released = self.manager.delete_backups([saved])
            self.assertEqual(count,1)
            self.assertGreater(released,0)
            self.assertEqual(strange.read_text(),'keep')

    def test_legacy_full_is_marked_incompatible(self):
        patches=self.patch_io()
        with patches[0],patches[1],patches[2],patches[4]:
            full=self.manager.backup('full').path
        # Rewrite manifest to pretend the old package backed up credentials.
        altered=self.backups/'legacy.full.tar.gz'
        with tarfile.open(full,'r:gz') as src, tarfile.open(altered,'w:gz') as dst:
            for member in src:
                if member.name=='manifest.json':
                    meta=json.load(src.extractfile(member))
                    meta['artifacts'].append({'id':'ldap','kind':'file'})
                    payload=json.dumps(meta).encode()
                    member.size=len(payload)
                    dst.addfile(member,io.BytesIO(payload))
                else:
                    dst.addfile(member,src.extractfile(member) if member.isfile() else None)
        found=[e for e in self.manager.describe_backups() if e.path==altered][0]
        self.assertTrue(found.legacy)
        with self.assertRaisesRegex(BackupError,'unknown artifact|media-only'):
            self.manager.verify('full',altered)

    def test_progress_has_complete_stage_counts(self):
        stages=[]
        with patch('netbox_ops.backups.write_audit'):
            self.manager.backup('media',progress=lambda *args:stages.append(args))
        self.assertEqual(stages[-1][:3],('complete',3,3))
        self.assertTrue(any(step[0]=='archive_media' for step in stages))


class NewCliAndTuiTests(unittest.TestCase):
    def test_full_cli_restore_only_options(self):
        parser=build_parser()
        for only in ('db','media','all'):
            parsed=parser.parse_args(['restore','full','--file','/tmp/x.full.tar.gz','--only',only])
            self.assertEqual(parsed.only,only)
        with self.assertRaises(SystemExit):
            parser.parse_args(['restore','db','--file','/tmp/x.dump','--only','media'])

    def test_service_metrics_parsed_without_mutation(self):
        output='\n'.join(['LoadState=loaded','ActiveState=active','SubState=running',
                          'UnitFileState=enabled','MainPID=123','CPUUsageNSec=9000000000',
                          'MemoryCurrent=104857600','ControlGroup='])
        runner=MagicMock(return_value=subprocess.CompletedProcess([],0,output,''))
        state=SystemdServices(Configuration(DEFAULT_UNITS),runner).status(['netbox'])[0]
        self.assertEqual(state.cpu_usage_nsec,9000000000)
        self.assertEqual(state.memory_current,104857600)
        self.assertTrue(state.healthy)

    def test_backup_menu_opening_is_non_destructive(self):
        screen=FakeScreen([27])
        menu=CursesMenu(screen,Translator('en'))
        with patch('netbox_ops.tui.load_config'),patch('netbox_ops.backups.BackupManager') as manager:
            manager.return_value.quick_list.return_value=[]
            menu._backups()
        manager.return_value.backup.assert_not_called()
        manager.return_value.restore.assert_not_called()
        manager.return_value.delete_all.assert_not_called()

    def test_create_requires_type_selection_and_confirmation(self):
        screen=FakeScreen([ord('c'),curses.KEY_DOWN,10,27,27])
        menu=CursesMenu(screen,Translator('en'))
        with patch('netbox_ops.tui.load_config'),patch('netbox_ops.backups.BackupManager') as manager:
            manager.return_value.quick_list.return_value=[]
            menu._backups()
        manager.return_value.backup.assert_not_called()

    def test_full_backup_restore_menu_selects_only_db(self):
        entry=BackupEntry(Path('/tmp/backup.full.tar.gz'),'full','2026-11-20T10:00:00+00:00',1024,'4.4.1')
        menu=CursesMenu(FakeScreen([curses.KEY_DOWN,-1,ord('r'),curses.KEY_DOWN,10,
                *(ord(c) for c in 'RESTORE FULL DB'),10,27,27]),Translator('en'))
        captured=[]
        def fake_job(title,worker):
            captured.append(worker)
        class ImmediatePool:
            def __init__(self, **kwargs): pass
            def submit(self, fn, *args):
                from concurrent.futures import Future
                future = Future()
                future.set_result(fn(*args))
                return future
            def shutdown(self, **kwargs): pass
        with patch('netbox_ops.tui.load_config'),patch('netbox_ops.backups.BackupManager') as manager, \
             patch('netbox_ops.tui.concurrent.futures.ThreadPoolExecutor',ImmediatePool), \
             patch.object(menu,'_run_backup_job',side_effect=fake_job):
            manager.return_value.quick_list.return_value=[entry]
            manager.return_value.describe_one.return_value=entry
            menu._backups()
            self.assertEqual(len(captured),1)
            captured[0](lambda *args:None)
            manager.return_value.restore.assert_called_once_with('full',entry.path,only='db',progress=unittest.mock.ANY)


if __name__=='__main__':
    unittest.main()
