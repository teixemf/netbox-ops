"""0.4.0: RHEL/Debian discovery, safe PostgreSQL selection, updated TUI."""
from __future__ import annotations

import ast
import curses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops import __version__
from netbox_ops.platform import (detect_platform, discover_service_units,
                                 postgres_cluster_port, pg_binary, PlatformError)
from netbox_ops.config import Configuration, DEFAULT_UNITS, load_config, ConfigError
from netbox_ops.i18n import Translator
from netbox_ops.backups import BackupManager
from netbox_ops.tui import CursesMenu
from netbox_ops.cli import build_parser, dispatch
from tests.helpers.fake_screen import FakeScreen


def result(text: str = '', rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, text, '')


class Platform040Tests(unittest.TestCase):
    def test_distribution_family_detected_from_os_release_and_id_like(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / 'os-release'
            for content, family in (
                    ('ID=debian\nVERSION_ID="12"\n', 'debian'),
                    ('ID=ubuntu\nID_LIKE=debian\n', 'debian'),
                    ('ID=rhel\nVERSION_ID="9.4"\n', 'rhel'),
                    ('ID=rocky\nID_LIKE="rhel centos fedora"\n', 'rhel'),
                    ('ID=almalinux\nID_LIKE="rhel centos fedora"\n', 'rhel'),
                    ('ID=other\n', 'other'),
            ):
                with self.subTest(family=family, content=content):
                    f.write_text(content)
                    self.assertEqual(detect_platform(f).family, family)

    def test_debian_redis_and_pg_cluster_unit(self):
        def runner(args):
            if args[0] == 'pg_lsclusters':
                return result('16 main 5432 online postgres /var/lib/postgresql/16/main /var/log/postgresql/pg.log\n')
            return result('loaded\n' if args[-1] == 'redis-server.service' else 'not-found\n')
        units = discover_service_units('debian', runner)
        self.assertEqual(units['redis'], 'redis-server.service')
        self.assertEqual(units['postgresql'], 'postgresql@16-main.service')
        self.assertEqual(postgres_cluster_port(units['postgresql'], runner), 5432)

    def test_debian_multicluster_is_not_silently_selected(self):
        def runner(args):
            if args[0] == 'pg_lsclusters':
                return result('15 main 5433 online postgres /x /y\n16 main 5432 online postgres /a /b\n')
            return result('loaded\n')
        with self.assertRaisesRegex(PlatformError, 'Multiple PostgreSQL clusters'):
            discover_service_units('debian', runner)
        units = discover_service_units('debian', runner, skip_postgres=True)
        self.assertEqual(units['redis'], 'redis-server.service')
        self.assertEqual(units['postgresql'], 'postgresql.service')

    def test_rhel_pgdg_postgres_version_unit_and_redis(self):
        def runner(args):
            loaded = {'postgresql-16.service', 'redis.service'}
            return result('loaded\n' if args[-1] in loaded else 'not-found\n')
        units = discover_service_units('rhel', runner)
        self.assertEqual(units['postgresql'], 'postgresql-16.service')
        self.assertEqual(units['redis'], 'redis.service')

    def test_rhel_multiple_pg_versions_requires_explicit_config(self):
        def runner(args):
            return result('loaded\n' if args[-1] in ('postgresql-15.service',
                                                       'postgresql-16.service') else 'not-found\n')
        with self.assertRaises(PlatformError):
            discover_service_units('rhel', runner)

    def test_debian_json_override_and_cluster_port(self):
        with tempfile.TemporaryDirectory(dir='/root') as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'netbox': 'netbox-custom.service'},
                                        'backup': {'postgres_port': 5433,
                                                   'directory': temp + '/backup',
                                                   'media_directory': temp + '/media'}}))
            path.chmod(0o600)
            with patch('netbox_ops.config.default_units', return_value={
                **DEFAULT_UNITS, 'redis': 'redis-server.service',
                'postgresql': 'postgresql@16-main.service'}):
                config = load_config(path)
            self.assertEqual(config.units['netbox'], 'netbox-custom.service')
            self.assertEqual(config.units['redis'], 'redis-server.service')
            self.assertEqual(config.postgres_port, 5433)

    def test_debian_postgresql_meta_unit_is_rejected_when_cluster_exists(self):
        with tempfile.TemporaryDirectory(dir='/root') as temp:
            path = Path(temp)/'config.json'
            path.write_text(json.dumps({'services': {'postgresql':'postgresql.service'}}))
            path.chmod(0o600)
            with patch('netbox_ops.config.default_units', return_value=DEFAULT_UNITS.copy()), \
                 patch('netbox_ops.config.detect_platform') as d, \
                 patch('netbox_ops.config.has_postgres_clusters', return_value=True):
                d.return_value.family='debian'
                with self.assertRaisesRegex(ConfigError, 'not a cluster unit'):
                    load_config(path)

    def test_rhel_systemctl_single_listing_detects_versioned_unit(self):
        def runner(args):
            if 'list-unit-files' in args:
                return result('postgresql-16.service enabled\nredis.service enabled\n')
            return result('loaded\n' if args[-1]=='redis.service' else 'not-found\n')
        profile = discover_service_units('rhel', runner)
        self.assertEqual(profile['postgresql'], 'postgresql-16.service')

    def test_multiple_clusters_can_be_resolved_by_explicit_unit(self):
        with tempfile.TemporaryDirectory(dir='/root') as temp:
            path = Path(temp)/'config.json'
            path.write_text(json.dumps({'services': {
                'postgresql': 'postgresql@16-main.service'},
                'backup': {'postgres_port': 5432}}))
            path.chmod(0o600)
            with patch('netbox_ops.config.default_units', side_effect=PlatformError('Multiple PostgreSQL clusters')), \
                 patch('netbox_ops.config.discover_service_units', return_value={
                     **DEFAULT_UNITS, 'redis': 'redis-server.service'}), \
                 patch('netbox_ops.config.detect_platform') as d:
                d.return_value.family = 'debian'
                cfg = load_config(path)
            self.assertEqual(cfg.units['postgresql'], 'postgresql@16-main.service')
            self.assertEqual(cfg.units['redis'], 'redis-server.service')

    def test_pg_client_location_is_configurable_and_rejects_unknown_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'pg_restore'
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
            self.assertEqual(pg_binary('pg_restore', Path(temp)), str(path))
            with self.assertRaises(PlatformError):
                pg_binary('unknown', Path(temp))

    def test_pg_commands_use_pinned_binary_and_port(self):
        from netbox_ops.backups import _pg
        config = Configuration(DEFAULT_UNITS, pg_bin_directory=Path('/opt/pgsql-16/bin'), postgres_port=5434)
        with patch('netbox_ops.backups.pg_binary', return_value='/opt/pgsql-16/bin/psql'), \
             patch('netbox_ops.backups._run', return_value='ok') as run:
            self.assertEqual(_pg('psql','-c','SELECT 1',cfg=config),'ok')
        self.assertEqual(run.call_args.args[0],
                         ['runuser','-u','postgres','--','/opt/pgsql-16/bin/psql',
                          '--port','5434','-c','SELECT 1'])

    def test_readonly_platform_command(self):
        parser = build_parser()
        args = parser.parse_args(['platform'])
        with patch('netbox_ops.cli.load_config', return_value=Configuration(DEFAULT_UNITS)), \
             patch('netbox_ops.platform.detect_platform') as profile, \
             patch('netbox_ops.platform.pg_binary', return_value='/usr/bin/psql'):
            profile.return_value.family='debian'
            profile.return_value.distro='debian'
            profile.return_value.version='12'
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()) as output:
                rc=dispatch(args, Translator('pt'))
        self.assertEqual(rc,0)
        self.assertIn('debian',output.getvalue())
        self.assertIn('postgresql.service',output.getvalue())

    def test_install_script_supports_python312_or_newer_on_both_families(self):
        root=Path(__file__).resolve().parents[2]
        installer=(root/'install.sh').read_text()
        upgrade=(root/'upgrade.sh').read_text()
        self.assertIn('FAMILY=debian',installer)
        self.assertIn('FAMILY=rhel',installer)
        self.assertIn('python3.12',installer)
        self.assertIn('python_is_supported',upgrade)
        self.assertIn('>= 12',upgrade)
        self.assertIn('PYTHON_BIN',upgrade)

    def test_source_compiles_with_python312_grammar(self):
        root=Path(__file__).resolve().parents[2]
        for source in (root/'netbox_ops').rglob('*.py'):
            with self.subTest(source=source.name):
                ast.parse(source.read_text(), filename=str(source), feature_version=(3,12))


class Ui040Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.backups=self.root/'backups'
        self.backups.mkdir()
        self.cfg=Configuration(DEFAULT_UNITS.copy(),self.backups)
        self.manager=BackupManager(self.cfg)
        for idx in range(3):
            name = f'netbox-db-20261120T13000{idx}Z-{idx}.dump'
            p=self.backups/name
            p.write_bytes(b'PGDMP_mock')
            (self.backups/(name+'.json')).write_text(json.dumps({
                'kind':'db','tool':'netbox-ops','format':1,
                'netbox_version':'4.4.1','netbox_version_source':'release.yaml',
                'purpose':'manual', 'created_utc': '2026-11-20T13:00:00+00:00'}))

    def test_wrong_confirmation_shows_error_then_goes_back(self):
        screen=FakeScreen([ord('W'),ord('R'),ord('O'),ord('N'),ord('G'),10,27])
        menu=CursesMenu(screen,Translator('pt'))
        self.assertFalse(menu._confirm_token('Delete backup','Delete?', 'DELETE BACKUP'))
        lines=' '.join(text for _,_,text,_ in screen.writes)
        self.assertIn('Confirmação incorrecta',lines)
        self.assertIn('Nenhuma operação foi iniciada',lines)

    def test_correct_confirmation_does_not_show_alert(self):
        keys=[*(ord(ch) for ch in 'CREATE'),10]
        screen=FakeScreen(keys)
        menu=CursesMenu(screen,Translator('en'))
        with patch.object(menu,'_show_lines') as alert:
            self.assertTrue(menu._confirm_token('create','confirm','CREATE'))
        alert.assert_not_called()

    def test_backup_list_and_manual_tag_above_actions_and_storage_chart(self):
        screen=FakeScreen([curses.KEY_DOWN, 27, 27],dimensions=(45,165))
        menu=CursesMenu(screen,Translator('pt'))
        with patch('netbox_ops.tui.load_config',return_value=self.cfg), \
             patch('netbox_ops.backups.BackupManager',return_value=self.manager):
            menu._backups()
        writes=screen.writes
        self.assertTrue(any('MANUAL' in txt for _,_,txt,_ in writes))
        self.assertTrue(any('ARMAZENAMENTO' in txt for _,_,txt,_ in writes))
        donut_positions=[y for y,x,txt,_ in writes if len(txt)==1 and 0x2800<ord(txt)<0x2900]
        self.assertTrue(donut_positions)
        actions = [y for y, _, txt, _ in writes if 'OPERAÇÕES SOBRE BACKUPS' in txt]
        table = [y for y, _, txt, _ in writes if txt.startswith('BACKUPS DISPONÍVEIS')]
        self.assertTrue(actions)
        self.assertTrue(table)
        self.assertLess(max(table), min(actions))
        self.assertLess(max(actions), min(donut_positions))
        self.assertTrue(any('MANUAL' in txt and max(table) < y < min(actions)
                            for y, _, txt, _ in writes))

    def test_detail_follows_current_focus_while_another_is_marked(self):
        screen=FakeScreen([curses.KEY_DOWN,32,curses.KEY_DOWN,27,27,27],dimensions=(45,165))
        menu=CursesMenu(screen,Translator('pt'))
        with patch('netbox_ops.tui.load_config',return_value=self.cfg):
            menu._backups()
        written=' '.join(t for _,_,t,_ in screen.frames[3])
        self.assertIn('netbox-db-20261120T130001Z-1.dump',written)
        self.assertIn('Seleccionados: 1',written)

    def test_full_restore_scope_menu_uses_arrows_and_no_implicit_default(self):
        from netbox_ops.backups import BackupEntry
        item=BackupEntry(Path('/tmp/backup.full.tar.gz'),'full','2026-11-20T18:00:00+00:00',11,'4.4.1')
        screen=FakeScreen([10,curses.KEY_DOWN,curses.KEY_DOWN,10],dimensions=(26,105))
        menu=CursesMenu(screen,Translator('en'))
        self.assertEqual(menu._choose_restore_scope(item),'media')
        text=' '.join(t for _,_,t,_ in screen.writes)
        self.assertIn('SELECTED BACKUP',text)
        self.assertIn('Restore PostgreSQL data only',text)
        self.assertIn('Restore database and media together',text)

    def test_version_format(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__=='__main__':
    unittest.main()
