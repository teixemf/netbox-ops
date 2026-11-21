"""v0.3.5: pre-restore labels, aligned table, responsive journal and live mode."""
from __future__ import annotations

import curses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops import __version__
from netbox_ops.backups import BackupManager
from netbox_ops.config import DEFAULT_UNITS, Configuration
from netbox_ops.i18n import Translator
from netbox_ops.journal import JournalPanel
from netbox_ops.services import ServiceState
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class RestoreTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root/'media'
        self.media.mkdir()
        (self.media/'one.txt').write_text('old')
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.root/'backups',
                                 'netbox', 'netbox', self.media)
        self.manager = BackupManager(self.cfg)

    def test_restore_creates_labeled_media_safety_backup_with_manifest(self):
        with patch('netbox_ops.backups._version',return_value='4.4.2'), \
             patch('netbox_ops.backups.write_audit'):
            source = self.manager.backup('media').path
        (self.media/'one.txt').write_text('before restore')
        systemd=MagicMock()
        systemd.status.return_value=[]
        with patch('netbox_ops.backups.SystemdServices',return_value=systemd), \
             patch('netbox_ops.backups._django_check'), \
             patch('netbox_ops.backups.write_audit'), \
             patch('netbox_ops.backups._version',return_value='4.4.2'):
            self.manager.restore('media',source)
        self.assertEqual((self.media/'one.txt').read_text(),'old')
        backup_files=self.manager.list_backups('media')
        self.assertEqual(len(backup_files),2)
        safety=next(p for p in backup_files if '-pre-restore-' in p.name)
        with patch('netbox_ops.backups._read_archive_metadata',side_effect=AssertionError('Must use sidecar')):
            described=self.manager.describe_one(next(e for e in self.manager.quick_list() if e.path==safety))
        self.assertEqual(described.purpose,'pre_restore')
        self.assertFalse(described.legacy)
        self.assertEqual(self.manager.verify('media',safety)['purpose'],'pre_restore')
        self.assertEqual(json.loads(Path(str(safety)+'.json').read_text())['purpose'],'pre_restore')
        normal=next(e for e in self.manager.quick_list() if e.path==source)
        self.assertEqual(normal.purpose,'manual')

    def test_db_backup_explicit_safety_purpose_and_filename(self):
        def fake_dump(cfg,target):
            target.write_bytes(b'PGDMP'+b'x'*50)
        with patch('netbox_ops.backups._db_dump',side_effect=fake_dump), \
             patch('netbox_ops.backups._check_dump'), \
             patch('netbox_ops.backups.write_audit'), \
             patch('netbox_ops.backups._version',return_value='4.4.2'):
            report=self.manager._backup('db',purpose='pre_restore')
        self.assertIn('db-pre-restore-', report.path.name)
        meta=json.loads(Path(str(report.path)+'.json').read_text())
        self.assertEqual(meta['purpose'],'pre_restore')
        self.assertEqual(self.manager.quick_list()[0].purpose,'pre_restore')

    def test_rejects_unknown_purpose(self):
        with self.assertRaisesRegex(Exception,'purpose'):
            self.manager._backup('media',purpose='danger')
        self.assertFalse(self.cfg.backup_directory.exists())

    def test_backup_table_centers_headers_and_displays_tag(self):
        with patch('netbox_ops.backups._version',return_value='4.4.2'), \
             patch('netbox_ops.backups.write_audit'):
            report=self.manager._backup('media', purpose='pre_restore')
        screen=FakeScreen([curses.KEY_DOWN,27,27],dimensions=(40,160))
        menu=CursesMenu(screen,Translator('pt'))
        with patch('netbox_ops.tui.load_config',return_value=self.cfg):
            menu._backups()
        shown=' '.join(x for _,_,x,_ in screen.writes)
        self.assertIn('ETIQUETA',shown)
        self.assertIn('PRÉ-REST.',shown)
        self.assertIn('Pré-restauro',shown)
        self.assertIn(report.path.name,shown)
        self.assertTrue(any('    TIPO' in x or '    TIPO ' in x for _,_,x,_ in screen.writes))


class JournalUiTests(unittest.TestCase):
    def setUp(self):
        self.service=ServiceState('netbox','netbox.service','loaded','active','running',
                                  'enabled','1234',0,200 * 1024**2,1)
        self.states=[self.service,*[ServiceState(n,n+'.service','loaded','active','running',
                                                  'enabled','200',0,1024**2,1)
                                     for n in ('netbox-rq','nginx','postgresql','redis')]]

    def test_manage_has_logs_and_bottom_horizontal_operations(self):
        screen=FakeScreen([curses.KEY_DOWN,-1,27,27],dimensions=(44,165))
        menu=CursesMenu(screen,Translator('pt'))
        systemd=MagicMock()
        systemd.status.return_value=self.states
        systemd.logs.return_value=['exemplo do journal']
        with patch('netbox_ops.tui.SystemdServices',return_value=systemd), \
             patch('netbox_ops.tui.load_config'):
            menu._manage()
        shown=' '.join(v for _,_,v,_ in screen.writes)
        self.assertIn('ÚLTIMOS LOGS',shown)
        self.assertIn('Live log: OFF',shown)
        self.assertIn('Iniciar',shown)
        self.assertIn('Desactivar',shown)
        # Footer operations appear lower than the log panel, not above it.
        log_lines=[y for y,x,v,_ in screen.writes if 'ÚLTIMOS LOGS' in v]
        action_lines=[y for y,x,v,_ in screen.writes if 'OPERAÇÕES SOBRE SERVIÇOS' in v]
        self.assertTrue(log_lines and action_lines)
        self.assertGreater(min(action_lines), min(log_lines))
        systemd.logs.assert_called_once_with('netbox',50)

    def test_enter_from_manage_opens_expanded_log_view(self):
        screen=FakeScreen([curses.KEY_DOWN, 10, 27, 27, 27],dimensions=(44,145))
        menu=CursesMenu(screen,Translator('pt'))
        systemd=MagicMock()
        systemd.status.return_value=self.states
        systemd.logs.return_value=['exemplo']
        with patch('netbox_ops.tui.SystemdServices',return_value=systemd), \
             patch('netbox_ops.tui.load_config'):
            menu._manage()
        shown=' '.join(v for _,_,v,_ in screen.writes)
        self.assertIn('LIVE LOG',shown.upper())
        self.assertIn('netbox.service',shown)

    def test_live_follower_nonblocking_reads_and_closes(self):
        read_fd,write_fd=os.pipe()
        stream=os.fdopen(read_fd,'rb',buffering=0)
        class FakePopen:
            def __init__(self):
                self.stdout=stream
                self.returncode=None
                self.terminated=False
            def poll(self):
                return None if not self.terminated else 0
            def terminate(self):
                self.terminated=True
            def wait(self,timeout=0):
                return 0
            def kill(self):
                self.terminated=True
        child=FakePopen()
        sd=MagicMock()
        sd.config.units=DEFAULT_UNITS.copy()
        sd.logs.return_value=['initial']
        try:
            with JournalPanel(sd,popen=lambda *a,**k:child) as panel:
                panel.select('netbox')
                self.assertTrue(panel.set_live(True))
                os.write(write_fd,b'2026-11-20 hello\n2026-11-20 world\n')
                panel.poll()
                self.assertIn('2026-11-20 hello',panel.lines)
                self.assertIn('2026-11-20 world',panel.lines)
                self.assertFalse(panel.set_live(False))
            self.assertTrue(child.terminated)
        finally:
            os.close(write_fd)

    def test_locales_cover_new_labels(self):
        for lang in ('en','pt'):
            t=Translator(lang)
            for key in ('nbo_live_action','nbo_manage_hint_035','nbo_expanded_hint',
                        'nbo_tag_header','nbo_pre_restore','nbo_purpose'):
                self.assertNotIn('<',t(key))

    def test_release(self):
        self.assertEqual(__version__,'0.4.0')


if __name__=='__main__':
    unittest.main()
