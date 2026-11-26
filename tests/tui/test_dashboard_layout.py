"""Main dashboard layout without a real terminal or systemd."""
import curses
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.backups import BackupSummary
from netbox_ops.config import SERVICE_NAMES
from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.services import ServiceError, ServiceState
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


def service_states():
    return [
        ServiceState('netbox', 'netbox.service', 'loaded', 'active', 'running', 'enabled',
                     memory_current=123456789),
        ServiceState('netbox-rq', 'netbox-rq.service', 'loaded', 'failed', 'failed', 'enabled'),
        ServiceState('nginx', 'nginx.service', 'loaded', 'activating', 'start', 'enabled',
                     memory_current=1024),
        ServiceState('postgresql', 'postgresql.service', 'loaded', 'inactive', 'dead', 'enabled'),
        ServiceState('redis', 'redis.service', 'not-found', 'inactive', 'dead', 'disabled'),
    ]


class ResizeScreen(FakeScreen):
    def getch(self):
        key = super().getch()
        if key == curses.KEY_RESIZE:
            self.dimensions = (16, 50)
        return key


class DashboardLayoutTests(unittest.TestCase):
    def menu(self, language, dimensions=(34, 140), screen=None):
        screen = screen or FakeScreen(dimensions=dimensions)
        menu = CursesMenu(screen, Translator(language))
        menu.versions.get = MagicMock(side_effect=lambda name: {
            'netbox': '4.7.3', 'netbox-rq': '4.7.3', 'nginx': '1.28.0',
            'postgresql': '16.2-custom-long-version', 'redis': None,
        }[name])
        menu.versions.request = MagicMock()
        return menu, screen

    def test_wide_table_columns_align_and_do_not_stretch(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                menu, screen = self.menu(language)
                t = menu.t
                for key in ('nbo_dashboard_failed', 'nbo_dashboard_transition'):
                    self.assertIn(key, t.messages)
                end = menu._draw_platform_status(42, 4, 85, service_states(),
                                                  {'netbox': '123.4%'}, '')
                self.assertEqual(end, 10)
                headers = {text.strip(): (x, len(text)) for y, x, text, _ in screen.writes if y == 4}
                names = [t('tui_col_service'), t('tui_col_state'),
                         t('nbo_service_version').upper(), 'CPU', 'RAM']
                self.assertEqual(list(headers), names)
                row = {x: text for y, x, text, _ in screen.writes if y == 5}
                for name in names:
                    self.assertIn(headers[name][0], row)
                self.assertEqual(row[headers[names[0]][0]], '● netbox')
                self.assertEqual(row[headers[names[1]][0]], t('tui_active'))
                self.assertEqual(row[headers['CPU'][0]].strip(), '123.4%')
                self.assertEqual(len(row[headers['CPU'][0]]), headers['CPU'][1])
                self.assertEqual(len(row[headers['RAM'][0]]), headers['RAM'][1])
                self.assertLess(headers['RAM'][0] + headers['RAM'][1], 42 + 85)
                text = ' '.join(text for _, _, text, _ in screen.writes)
                self.assertIn(t('nbo_dashboard_failed'), text)
                self.assertIn(t('nbo_dashboard_transition'), text)
                self.assertIn(t('tui_inactive'), text)
                self.assertIn('—', text)
                self.assertIn('…', text)

    def test_narrow_layout_keeps_state_and_never_overflows_panel(self):
        for language in LANGUAGES:
            for width in (24, 36, 48, 64):
                with self.subTest(language=language, width=width):
                    menu, screen = self.menu(language)
                    menu._draw_platform_status(24, 4, width, service_states(),
                                               {'netbox': '1234.5%'}, '')
                    self.assertTrue(all(x + len(text) <= 24 + width
                                        for _, x, text, _ in screen.writes))
                    headings = [text.strip() for y, _, text, _ in screen.writes if y == 4]
                    self.assertIn(menu.t('tui_col_service'), headings)
                    self.assertIn(menu.t('tui_col_state'), headings)
                    if width == 24:
                        self.assertEqual(len(headings), 2)
                    if 'CPU' in headings:
                        self.assertTrue(any(text.strip() == '1234.5%' for _, _, text, _ in screen.writes))
                    for name in SERVICE_NAMES:
                        self.assertTrue(any(name in text for _, _, text, _ in screen.writes))

    def test_missing_service_and_failed_refresh_do_not_show_old_metrics(self):
        menu, screen = self.menu('en')
        menu._draw_platform_status(42, 4, 85, service_states()[:1], {'redis': '99.9%'}, '')
        self.assertTrue(any(text == '? redis' for _, _, text, _ in screen.writes))
        self.assertFalse(any('99.9%' in text for _, _, text, _ in screen.writes))
        screen.writes.clear()
        menu.versions.get.reset_mock()
        menu._draw_platform_status(42, 4, 85, service_states(), {'netbox': '99.9%'}, 'systemd unavailable')
        self.assertIn((5, 42, 'systemd unavailable', 0), screen.writes)
        self.assertFalse(any('99.9%' in text or '● netbox' in text for _, _, text, _ in screen.writes))
        menu.versions.get.assert_not_called()

    def test_storage_respects_bottom_and_reserves_latest_backup_rows(self):
        for language in LANGUAGES:
            menu, screen = self.menu(language)
            disk = (1024, 3072, 4096, 8192, 2)
            end = menu._draw_dashboard_storage(42, 11, 85, 24, (1024, 4096, 2), disk, '')
            self.assertEqual(end, 21)
            self.assertTrue(any(len(text) == 1 and 0x2800 < ord(text) < 0x2900
                                for _, _, text, _ in screen.writes))
            self.assertTrue(all(y < end for y, _, _, _ in screen.writes))
            screen.writes.clear()
            self.assertEqual(menu._draw_dashboard_storage(24, 11, 24, 13,
                                                           (1024, 4096, 2), disk, ''), 14)
            self.assertTrue(all(11 <= y <= 13 and x + len(text) <= 48
                                for y, x, text, _ in screen.writes))
            self.assertTrue(any('2 / 1.0 KiB' in text for _, _, text, _ in screen.writes))
            self.assertTrue(any('4.0 KiB' in text for _, _, text, _ in screen.writes))
            screen.writes.clear()
            menu._draw_dashboard_storage(24, 11, 24, 13, (1024, 4096, 2), disk, 'storage unavailable')
            self.assertFalse(any('KiB' in text for _, _, text, _ in screen.writes))
            self.assertTrue(any('storage unavailable' in text for _, _, text, _ in screen.writes))

    def test_main_resize_and_refresh_keep_separator_and_footer_clear(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                screen = ResizeScreen([curses.KEY_RESIZE, curses.KEY_F5, ord('q')],
                                      dimensions=(34, 140))
                menu, _ = self.menu(language, screen=screen)
                systemd = MagicMock()
                systemd.status.side_effect = [service_states(), ServiceError('systemd unavailable')]
                manager = MagicMock()
                latest = BackupSummary(Path('/backup/latest.dump'), 'db',
                                       dt.datetime(2026, 11, 25, 12, 34, tzinfo=dt.timezone.utc),
                                       'filename', 'manual')
                manager.dashboard_storage.side_effect = [
                    ((1024, 3072, 4096, 8192, 2), latest), OSError('storage unavailable'),
                ]
                with (patch('netbox_ops.tui.load_config'),
                      patch('netbox_ops.tui.SystemdServices', return_value=systemd),
                      patch('netbox_ops.backups.BackupManager', return_value=manager)):
                    self.assertEqual(menu._main(), 0)
                self.assertEqual(systemd.status.call_count, 2)
                self.assertEqual(manager.dashboard_storage.call_count, 2)
                for frame, width, height in zip(screen.frames, (140, 50, 50), (34, 16, 16)):
                    split = max(22, width * 35 // 100)
                    x = split + 2
                    self.assertIn((10, x, '─' * (width - split - 4), 0), frame)
                    label = menu.t('nbo_storage_label' if frame is not screen.frames[1]
                                   else 'nbo_latest_filename')[:width - split - 4]
                    self.assertTrue(any(y == 11 and col == x and text == label
                                        for y, col, text, _ in frame))
                    self.assertFalse(any(y >= height - 2 and col >= x and 'KiB' in text
                                         for y, col, text, _ in frame))
                final = ' '.join(text for _, _, text, _ in screen.frames[-1])
                self.assertIn('systemd unavailable', final)
                self.assertIn('storage unavailable', final)
                self.assertNotIn('1.0 KiB', final)
                self.assertNotIn('2026-11-25 12:34 UTC', final)
                for frame in screen.frames[:2]:
                    self.assertTrue(any('2026-11-25 12:34 UTC' in text for _, _, text, _ in frame))
                    self.assertTrue(any('db / ' + menu.t('nbo_manual_short') in text
                                        for _, _, text, _ in frame))
                self.assertEqual(screen.wait_ms, -1)

    def test_latest_lines_show_utc_source_and_unknown_tag_in_both_languages(self):
        stamp = dt.datetime(2026, 11, 26, 2, 5, tzinfo=dt.timezone(dt.timedelta(hours=1)))
        for language in LANGUAGES:
            menu, _ = self.menu(language)
            for kind, source, purpose, tag in (
                ('combined', 'filename', 'manual', 'nbo_manual_short'),
                ('full', 'filename', 'pre_restore', 'nbo_pre_restore_short'),
                ('legacy', 'mtime', 'unknown', 'nbo_latest_unknown_tag'),
            ):
                latest = BackupSummary(Path('/backup/archive'), kind, stamp, source, purpose)
                for width in (24, 85):
                    with self.subTest(language=language, kind=kind, width=width):
                        rows = menu._latest_backup_lines(latest, width)
                        self.assertTrue(all(len(line) <= width for line, _ in rows))
                        text = ' '.join(line for line, _ in rows)
                        self.assertIn(menu.t('nbo_latest_' + source), text)
                        self.assertIn('2026-11-26 01:05 UTC', text)
                        self.assertIn(kind + ' / ' + menu.t(tag), text)
            self.assertEqual(menu._latest_backup_lines(None, 24),
                             [(menu.t('nbo_latest_none'), 'muted')])

    def test_dashboard_refresh_cadence_return_and_f5_replace_latest_snapshot(self):
        screen = FakeScreen([-1, -1, ord('2'), curses.KEY_F5, ord('q')], dimensions=(34, 140))
        menu, _ = self.menu('en', screen=screen)
        systemd = MagicMock()
        systemd.status.return_value = service_states()
        manager = MagicMock()
        before = BackupSummary(Path('/backup/one.dump'), 'db',
                               dt.datetime(2026, 11, 25, 12, tzinfo=dt.timezone.utc),
                               'filename', 'manual')
        after = BackupSummary(Path('/backup/two.media.tar.gz'), 'media',
                              dt.datetime(2026, 11, 26, 1, tzinfo=dt.timezone.utc),
                              'mtime', 'pre_restore')
        manager.dashboard_storage.side_effect = [
            ((1024, 3072, 4096, 8192, 1), before),
            ((0, 4096, 4096, 8192, 0), None),
            ((1024, 3072, 4096, 8192, 1), after),
            OSError('backup inventory unavailable'),
        ]
        with (patch('netbox_ops.tui.load_config'),
              patch('netbox_ops.tui.SystemdServices', return_value=systemd),
              patch('netbox_ops.backups.BackupManager', return_value=manager),
              patch('netbox_ops.tui.time.monotonic', side_effect=[100, 101, 115, 116, 117]),
              patch.object(menu, '_backups') as backups):
            self.assertEqual(menu._main(), 0)
        backups.assert_called_once()
        self.assertEqual(manager.dashboard_storage.call_count, 4)
        frames = [' '.join(text for _, _, text, _ in frame) for frame in screen.frames]
        self.assertIn('2026-11-25 12:00 UTC', frames[0])
        self.assertIn('2026-11-25 12:00 UTC', frames[1])
        self.assertIn(menu.t('nbo_latest_none'), frames[2])
        self.assertNotIn('2026-11-25 12:00 UTC', frames[2])
        self.assertIn('2026-11-26 01:00 UTC', frames[3])
        self.assertIn('media / ' + menu.t('nbo_pre_restore_short'), frames[3])
        self.assertIn('backup inventory unavailable', frames[4])
        self.assertNotIn('2026-11-26 01:00 UTC', frames[4])
