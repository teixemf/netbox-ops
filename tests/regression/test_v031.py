"""v0.3.1 regression: responsive listing, version caching, live service actions.

All tests use temporary directories and simulated commands; never touch systemd/DB.
"""
from __future__ import annotations

import curses
import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess

from netbox_ops import __version__
from netbox_ops.backups import BackupManager, BackupEntry
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.services import ServiceState, ServiceVersionCache
from netbox_ops.tui import CursesMenu, action_enabled
from tests.helpers.fake_screen import FakeScreen


class BackupPerformanceTests(unittest.TestCase):
    def setUp(self):
        # Test privileged flows without running the test process as root.
        root_guard = patch('netbox_ops.backups._require_root')
        root_guard.start()
        self.addCleanup(root_guard.stop)
        # File locking is tested separately; do not access global /run paths here.
        lock_guard = patch('netbox_ops.backups.ExclusiveLock')
        lock_guard.start()
        self.addCleanup(lock_guard.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        (self.media / 'sample.png').write_bytes(b'not-a-real-png')
        self.cfg = Configuration(DEFAULT_UNITS.copy(), self.root / 'backups',
                                 'netbox', 'netbox', self.media)
        self.manager = BackupManager(self.cfg)
        self.cfg.backup_directory.mkdir(mode=0o700)

    def test_quick_listing_never_decompresses_archives_or_reads_manifest(self):
        for number in range(30):
            path = self.cfg.backup_directory / f'netbox-full-20261120T1234{number:02d}Z.full.tar.gz'
            with path.open('wb') as file:
                file.write(b'not-a-tar-large-archive')
        with patch('netbox_ops.backups._read_archive_metadata',
                   side_effect=AssertionError('Archive read!')), \
             patch('netbox_ops.backups._read_sidecar_metadata',
                   side_effect=AssertionError('Sidecar read!')):
            rows = self.manager.quick_list()
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(row.kind == 'full' and row.size > 0 for row in rows))

    def test_media_backup_produces_fast_metadata_sidecar_without_credentials(self):
        with patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            archive = self.manager.backup('media').path
        sidecar = Path(str(archive)+'.json')
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(sidecar.read_text())['netbox_version'], '4.4.1')
        entry = next(x for x in self.manager.quick_list() if x.path == archive)
        with patch('netbox_ops.backups._read_archive_metadata',
                   side_effect=AssertionError('Decompression during preview')):
            detailed = self.manager.describe_one(entry)
        self.assertEqual(detailed.version,'4.4.1')
        self.assertFalse(detailed.legacy)

    def test_metadata_cache_once_for_same_selection(self):
        with patch('netbox_ops.backups._version', return_value='4.4.1'), \
             patch('netbox_ops.backups.write_audit'):
            archive = self.manager.backup('media').path
        screen = FakeScreen([curses.KEY_DOWN, -1, curses.KEY_UP, curses.KEY_DOWN, 27,27],
                            dimensions=(30,125))
        menu = CursesMenu(screen, Translator('pt'))
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), \
             patch('netbox_ops.backups.BackupManager', return_value=self.manager), \
             patch.object(self.manager, 'describe_one', wraps=self.manager.describe_one) as reader:
            menu._backups()
        self.assertEqual(reader.call_count, 1)

    def test_f5_reloads_inventory_not_on_every_key(self):
        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_UP, -1, curses.KEY_DOWN,
                             curses.KEY_F5, 27, 27], dimensions=(30,125))
        menu = CursesMenu(screen, Translator('pt'))
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), \
             patch('netbox_ops.backups.BackupManager', return_value=self.manager), \
             patch.object(self.manager,'quick_list',wraps=self.manager.quick_list) as listing:
            menu._backups()
        self.assertEqual(listing.call_count,2) # enter, F5; never on arrow/idle


class ServiceVersionTests(unittest.TestCase):
    def test_version_caching_netbox_and_rq_share_one_lookup(self):
        runner = MagicMock()
        versions = ServiceVersionCache(runner=runner)
        patcher = patch('netbox_ops.services.read_netbox_version', return_value='4.4.1')
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        # Synchronize with the cache future, without timing sleeps.
        future = versions._futures.get('netbox')
        if future is None:
            versions.request('netbox')
            future = versions._futures['netbox']
        future.result(timeout=2)
        self.assertEqual(versions.get('netbox'),'4.4.1')
        self.assertEqual(versions.get('netbox-rq'),'4.4.1')
        self.assertEqual(mocked.call_count,1)
        self.assertEqual(runner.call_count,0)
        versions.reset(['netbox-rq'])
        versions.request('netbox')
        versions._futures['netbox'].result(timeout=2)
        self.assertEqual(mocked.call_count,2)
        versions._executor.shutdown(wait=True)

    def test_unavailable_version_does_not_break_screen(self):
        runner = MagicMock(side_effect=FileNotFoundError('missing package'))
        versions=ServiceVersionCache(runner=runner)
        versions.request('redis')
        versions._futures['redis'].result(timeout=2)
        self.assertEqual(versions.get('redis'), '—')
        self.assertEqual(versions.get('redis'), '—')
        self.assertEqual(runner.call_count,1)
        versions._executor.shutdown(wait=True)

    def test_actions_follow_running_and_boot_state(self):
        def state(active, enabled):
            return ServiceState('netbox','netbox.service','loaded',active,
                                'running' if active=='active' else 'dead', enabled)
        active=state('active','enabled')
        self.assertFalse(action_enabled('start',active))
        self.assertTrue(action_enabled('stop',active))
        self.assertTrue(action_enabled('restart',active))
        self.assertFalse(action_enabled('enable',active))
        self.assertTrue(action_enabled('disable',active))
        inactive=state('inactive','disabled')
        self.assertTrue(action_enabled('start',inactive))
        self.assertFalse(action_enabled('stop',inactive))
        self.assertFalse(action_enabled('restart',inactive))
        self.assertTrue(action_enabled('enable',inactive))
        self.assertFalse(action_enabled('disable',inactive))
        self.assertFalse(action_enabled('enable',state('active','static')))

    def test_service_detail_is_logs_free_and_has_uptime_field(self):
        state=ServiceState('nginx','nginx.service','loaded','active','running',
                           'enabled','123',0,16*1024**2,1)
        screen=FakeScreen([27])
        tui=CursesMenu(screen,Translator('en'))
        tui.versions.request=MagicMock()
        tui.versions.get=MagicMock(return_value='1.26.1')
        sys=MagicMock()
        tui._detail(sys,state)
        output=' '.join(text for _, _, text, _ in screen.writes)
        self.assertIn('Version: 1.26.1',output)
        self.assertIn('Uptime:',output)
        self.assertIn('RAM: 16.0 MiB',output)
        sys.logs.assert_called_once()

    def test_manage_layout_contains_operational_metrics_not_logs(self):
        service = ServiceState('netbox','netbox.service','loaded','active','running',
                               'enabled','42',0,123*1024**2,1)
        states = [service, *[ServiceState(name,name+'.service','loaded','active','running',
                                          'enabled',str(i),0,1024**2,1)
                              for i,name in enumerate(('netbox-rq','nginx','postgresql','redis'),2)]]
        screen = FakeScreen([curses.KEY_DOWN,27,27],dimensions=(35,145))
        menu=CursesMenu(screen,Translator('en'))
        menu.versions.request=MagicMock()
        menu.versions.get=MagicMock(return_value='4.4.1')
        service_api=MagicMock()
        service_api.status.return_value=states
        with patch('netbox_ops.tui.SystemdServices',return_value=service_api), \
             patch('netbox_ops.tui.load_config'):
            menu._manage()
        shown=' '.join(part for _, _, part, _ in screen.writes)
        self.assertIn('RAM: 123.0 MiB',shown)
        self.assertIn('Version: 4.4.1',shown)
        self.assertIn('START',shown.upper())
        self.assertIn('LATEST SERVICE JOURNAL',shown)
        service_api.logs.assert_called_once_with('netbox', 50)

    def test_mixed_batch_start_only_applies_to_inactive_services(self):
        running = ServiceState('netbox','netbox.service','loaded','active','running','enabled')
        stopped = ServiceState('netbox-rq','netbox-rq.service','loaded','inactive','dead','enabled')
        others = [ServiceState(name,name+'.service','loaded','active','running','enabled')
                  for name in ('nginx','postgresql','redis')]
        screen = FakeScreen([curses.KEY_DOWN,32,curses.KEY_DOWN,32,
                             9,10,27,27],dimensions=(30,130))
        menu = CursesMenu(screen,Translator('pt'))
        service_api=MagicMock()
        service_api.status.return_value=[running,stopped,*others]
        with patch('netbox_ops.tui.SystemdServices',return_value=service_api), \
             patch('netbox_ops.tui.load_config'), \
             patch('netbox_ops.tui.os.geteuid',return_value=0), \
             patch.object(menu,'_confirm_action',return_value=True) as confirm, \
             patch.object(menu,'_execute_batch') as execute:
            menu._manage()
        confirm.assert_called_once_with('start',['netbox-rq'],skipped=['netbox'])
        execute.assert_called_once_with(service_api,'start',['netbox-rq'])

    def test_version_is_032(self):
        self.assertEqual(__version__, '0.4.0')


if __name__ == '__main__':
    unittest.main()
