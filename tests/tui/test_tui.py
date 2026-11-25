"""TUI tests are run without a real systemd or without controlling a real tty."""
from __future__ import annotations

import curses
import unittest
from unittest.mock import MagicMock, patch

from netbox_ops.i18n import Translator
from netbox_ops.tui import (CursesMenu, SECTIONS, make_main_items,
                            move_selection, safe_text, action_enabled, move_action_focus)
from netbox_ops.services import ServiceState


from tests.helpers.fake_screen import FakeScreen


class MenuNavigationTests(unittest.TestCase):
    def test_all_actions_plus_language_and_exit(self):
        pt = make_main_items(Translator('pt'))
        en = make_main_items(Translator('en'))
        self.assertEqual(len(pt), 9)
        self.assertEqual(len(en), 9)
        self.assertEqual(pt[0][2], ('services', 'manage'))
        self.assertEqual(pt[6][2], ('self-upgrade', ''))
        self.assertEqual(pt[-1][1], 'exit')
        self.assertEqual(SECTIONS, {0: 'menu_services', 1: 'menu_backups',
                                    2: 'menu_updates', 6: 'menu_maintenance'})
        self.assertIn('Manage services', en[0][0])
        for items in (pt, en):
            self.assertEqual([item[0].split()[0] for item in items],
                             ['1', '2', '3', '4', '5', '6', '7', 'L', 'Q'])

    def test_numeric_shortcuts_open_main_menu_actions(self):
        for key, method, args in (('1', '_manage', ()), ('2', '_backups', ()),
                                  ('4', '_upgrade', ('netbox',)), ('5', '_upgrade', ('plugins',)),
                                  ('6', '_upgrade', ('stack',)), ('7', '_self_upgrade', ())):
            with self.subTest(key=key):
                screen = FakeScreen([ord(key), ord('q')], dimensions=(16, 60))
                menu = CursesMenu(screen, Translator('en'))
                systemd = MagicMock()
                systemd.status.return_value = []
                with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
                     patch('netbox_ops.tui.load_config'), \
                     patch('netbox_ops.backups.BackupManager') as backups, \
                     patch.object(menu, method, return_value=None) as action:
                    backups.return_value.storage_stats.return_value = (0, 0, 0)
                    menu.versions.request = MagicMock()
                    self.assertEqual(menu._main(), 0)
                action.assert_called_once_with(*args)
                self.assertTrue(any(text.startswith('Q  ') for _, _, text, _ in screen.frames[0]))

    def test_global_quit_from_services_and_backups(self):
        for view, keys in (('manage', [curses.KEY_DOWN, 32, ord('Q')]),
                           ('backups', [ord('Q')])):
            with self.subTest(view=view):
                screen = FakeScreen(keys)
                menu = CursesMenu(screen, Translator('en'))
                systemd = MagicMock()
                systemd.status.return_value = [ServiceState('netbox', 'netbox.service',
                                                             'loaded', 'active', 'running', 'enabled')]
                with patch.object(menu, '_init_screen'), patch('netbox_ops.tui.load_config'), \
                     patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
                     patch('netbox_ops.backups.BackupManager') as backups, \
                     patch('netbox_ops.tui.JournalPanel.close') as close:
                    backups.return_value.quick_list.return_value = []
                    self.assertEqual(menu.run(view), 0)
                if view == 'manage':
                    close.assert_called_once()
                self.assertEqual(screen.wait_ms, -1)

    def test_quit_from_nested_menu_unwinds_to_run(self):
        screen = FakeScreen([ord('Q')])
        menu = CursesMenu(screen, Translator('en'))
        with patch.object(menu, '_init_screen'), patch.object(menu, '_main',
                side_effect=lambda: menu._choose('Menu', [('choice', '')], 'hint')):
            self.assertEqual(menu.run(), 0)

    def test_q_does_not_acknowledge_results_or_cancel_token_input(self):
        screen = FakeScreen([ord('Q'), ord('q'), 10])
        menu = CursesMenu(screen, Translator('en'))
        with patch('netbox_ops.tui.curses.flushinp') as flush:
            menu._wait_result_ack()
        self.assertEqual(flush.call_count, 2)
        self.assertEqual(list(screen.keys), [])
        screen = FakeScreen([ord('Q'), 10])
        menu = CursesMenu(screen, Translator('en'))
        self.assertTrue(menu._confirm_token('Confirm', 'Enter Q', 'Q'))
        screen = FakeScreen([ord('Q'), 27])
        menu = CursesMenu(screen, Translator('en'))
        menu._show_lines('Error', [('failure', 'failure')])
        self.assertEqual(len(screen.frames), 2)

    def test_arrows_wrap_home_end_and_pages(self):
        self.assertEqual(move_selection(0, curses.KEY_UP, 15), 14)
        self.assertEqual(move_selection(14, curses.KEY_DOWN, 15), 0)
        self.assertEqual(move_selection(8, curses.KEY_HOME, 15), 0)
        self.assertEqual(move_selection(8, curses.KEY_END, 15), 14)
        self.assertEqual(move_selection(8, curses.KEY_NPAGE, 15), 14)
        self.assertEqual(move_selection(8, curses.KEY_PPAGE, 15), 2)

    def test_printed_system_errors_do_not_include_control_characters(self):
        self.assertEqual(safe_text('bad\x1b[31m\nservice'), 'bad [31m service')

    def test_fast_escape_delay_configured(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        with patch('netbox_ops.tui.curses.set_escdelay') as delay, \
             patch('netbox_ops.tui.curses.curs_set'), \
             patch('netbox_ops.tui.curses.has_colors', return_value=False):
            menu._init_screen()
        delay.assert_called_once_with(45)

    def test_tui_entry_navigation_and_escape(self):
        screen = FakeScreen(keys=[curses.KEY_DOWN, curses.KEY_DOWN, 10])
        menu = CursesMenu(screen, Translator('pt'))
        menu._init_screen = lambda: None
        result = menu._choose('Menu', [('a', ''), ('b', ''), ('c', '')], 'hint')
        self.assertEqual(result, 1)
        self.assertTrue(any('c' in entry[2] for entry in screen.writes))

    def test_language_can_change_without_exiting(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        with patch.object(menu, '_choose', return_value=1):
            menu._language()
        self.assertEqual(menu.t.language, 'en')


class RestartSafetyTests(unittest.TestCase):
    def test_confirmation_accepts_only_literal_RESTART(self):
        keys = [*(ord(ch) for ch in 'RESTART'), 10]
        screen = FakeScreen(keys)
        self.assertTrue(CursesMenu(screen, Translator('pt'))._confirm(['netbox']))
        self.assertFalse(CursesMenu(FakeScreen([*(ord(ch) for ch in 'restart'), 10, 27]),
                                     Translator('pt'))._confirm(['netbox']))
        self.assertFalse(CursesMenu(FakeScreen([27]), Translator('pt'))._confirm(['netbox']))

    def test_cancelled_manage_action_never_executes_systemctl(self):
        # Mark first service; attempt disabled action; clear marks and return.
        screen = FakeScreen([curses.KEY_DOWN, 32, 9, 10, 27, 27, 27, 27, 27])
        menu = CursesMenu(screen, Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = [ServiceState(n, f'{n}.service', 'loaded',
                                       'active', 'running', 'enabled', '123')
                                       for n in ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
             patch('netbox_ops.tui.load_config'), \
             patch.object(menu, '_choose', return_value=None), \
             patch.object(menu, '_execute_batch') as batch:
            menu._manage()
        batch.assert_not_called()
        systemd.restart.assert_not_called()

    def test_unimplemented_tui_item_never_runs_business_logic(self):
        menu = CursesMenu(FakeScreen([curses.KEY_DOWN,curses.KEY_DOWN,curses.KEY_DOWN,10,ord('q')]), Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = []
        with patch.object(menu,'_init_screen'), patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
             patch('netbox_ops.tui.load_config'), patch('netbox_ops.backups.BackupManager') as backups, \
             patch.object(menu, '_show_lines') as show:
            backups.return_value.storage_stats.return_value = (0,0,0)
            self.assertEqual(menu.run(), 0)
        self.assertEqual(show.call_count, 1)
        self.assertIn('NÃO IMPLEMENTADO', show.call_args.args[1][0][0])


class NewServiceTuiTests(unittest.TestCase):
    def _state(self, name='netbox', active='active'):
        return ServiceState(name, f'{name}.service', 'loaded', active,
                            'running' if active == 'active' else 'dead', 'enabled', '4321')

    def test_escape_ignored_only_on_main_menu(self):
        menu = CursesMenu(FakeScreen([27, ord('q')]), Translator('pt'))
        self.assertEqual(menu._choose('menu', [('one', '')], 'hint', main_menu=True), 'quit')
        menu = CursesMenu(FakeScreen([27]), Translator('pt'))
        self.assertIsNone(menu._choose('submenu', [('one', '')], 'hint'))

    def test_first_escape_clears_highlight_second_returns_from_submenu(self):
        screen = FakeScreen([curses.KEY_DOWN, 27, 27])
        menu = CursesMenu(screen, Translator('pt'))
        self.assertIsNone(menu._choose('submenu', [('one', ''), ('two', '')], 'hint'))
        self.assertEqual(len(screen.frames), 3)
        self.assertFalse(any(e[-1] == curses.A_REVERSE for e in screen.frames[0]))
        self.assertTrue(any(e[-1] == curses.A_REVERSE for e in screen.frames[1]))
        self.assertFalse(any(e[-1] == curses.A_REVERSE for e in screen.frames[2]))

    def test_main_menu_esc_clears_highlight_but_only_q_exits(self):
        screen = FakeScreen([curses.KEY_DOWN, 27, 27, ord('q')])
        menu = CursesMenu(screen, Translator('en'))
        self.assertEqual(menu._choose('main', [('one', '')], 'hint', main_menu=True), 'quit')
        self.assertFalse(any(e[-1] == curses.A_REVERSE for e in screen.frames[-1]))

    def test_main_menu_escape_then_q_exits(self):
        menu = CursesMenu(FakeScreen([27, ord('q')]), Translator('pt'))
        systemd=MagicMock()
        systemd.status.return_value=[]
        with patch.object(menu,'_init_screen'), patch('netbox_ops.tui.SystemdServices',return_value=systemd), \
             patch('netbox_ops.tui.load_config'), patch('netbox_ops.backups.BackupManager') as backups:
            backups.return_value.storage_stats.return_value=(0,0,0)
            self.assertEqual(menu.run(),0)

    def test_status_initial_view_has_no_highlight(self):
        menu = CursesMenu(FakeScreen([27]), Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = [self._state(n) for n in
                                       ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.load_config'), \
             patch('netbox_ops.tui.SystemdServices', return_value=systemd):
            menu._status()
        self.assertFalse(any('› ' in entry[2] for entry in menu.stdscr.writes))

    def test_table_can_open_detail_without_modifying_services(self):
        screen = FakeScreen([curses.KEY_DOWN, 10, 27, 27])
        menu = CursesMenu(screen, Translator('pt'))
        manager = MagicMock()
        manager.status.return_value = [self._state(n) for n in
                                       ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.load_config'), \
             patch('netbox_ops.tui.SystemdServices', return_value=manager), \
             patch.object(menu, '_detail') as detail:
            menu._status()
        detail.assert_called_once()
        self.assertEqual(detail.call_args.args[1].name, 'netbox')
        self.assertTrue(any('5/5 serviços activos' in write[2] for write in screen.writes))
        self.assertTrue(any('netbox-rq' in write[2] for write in screen.writes))

    def test_details_display_cpu_ram_version_not_journal(self):
        screen = FakeScreen([27])
        menu = CursesMenu(screen, Translator('en'))
        menu.cpu_display['netbox'] = '2.3%'
        menu.versions.get = MagicMock(return_value='4.4.1')
        menu.versions.request = MagicMock()
        systemd = MagicMock()
        systemd.logs.return_value = ['journal ready']
        menu._detail(systemd, self._state())
        self.assertTrue(any('CPU: 2.3%' in x[2] for x in screen.writes))
        self.assertTrue(any('Version: 4.4.1' in x[2] for x in screen.writes))
        systemd.logs.assert_called_once()
        systemd.restart.assert_not_called()

    def test_details_are_read_only_even_after_enter(self):
        screen = FakeScreen([10, 27, 27])  # Enter opens journal viewer only
        menu = CursesMenu(screen, Translator('pt'))
        systemd = MagicMock()
        systemd.logs.return_value = ['a journal entry']
        menu._detail(systemd, self._state())
        for action in ('start', 'stop', 'restart', 'enable', 'disable'):
            getattr(systemd, action).assert_not_called()
        self.assertFalse(any('ACÇÕES' in write[2] for write in screen.writes))

    def test_critical_confirmation_name_is_required(self):
        text = 'RESTART postgresql'
        menu = CursesMenu(FakeScreen([*(ord(c) for c in text), 10]), Translator('en'))
        self.assertTrue(menu._confirm_action('restart', ['postgresql']))
        menu = CursesMenu(FakeScreen([*(ord(c) for c in 'RESTART'), 10, 27]), Translator('en'))
        self.assertFalse(menu._confirm_action('restart', ['postgresql']))

    def test_action_disabled_for_active_or_inactive(self):
        self.assertFalse(action_enabled('start', self._state(active='active')))
        self.assertTrue(action_enabled('stop', self._state(active='active')))
        self.assertFalse(action_enabled('stop', self._state(active='inactive')))
        self.assertTrue(action_enabled('start', self._state(active='inactive')))
        self.assertFalse(action_enabled('restart', self._state(active='inactive')))

    def test_custom_log_count_and_cancel(self):
        menu = CursesMenu(FakeScreen([ord('5'), ord('0'), 10]), Translator('pt'))
        self.assertEqual(menu._read_number(20), 50)
        menu = CursesMenu(FakeScreen([27]), Translator('pt'))
        self.assertIsNone(menu._read_number(20))

    def test_read_only_detail_does_not_fetch_journal_or_control_services(self):
        menu = CursesMenu(FakeScreen([9, curses.KEY_PPAGE, curses.KEY_NPAGE,
                                     ord('+'), 27]), Translator('pt'))
        systemd = MagicMock()
        menu._detail(systemd, self._state())
        systemd.logs.assert_called_once()
        systemd.start.assert_not_called()
        systemd.stop.assert_not_called()
        systemd.restart.assert_not_called()

    def test_manage_initial_screen_has_no_cyan_selection(self):
        screen = FakeScreen([27])
        menu = CursesMenu(screen, Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = [self._state(n) for n in
                                       ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
             patch('netbox_ops.tui.load_config'):
            menu._manage()
        self.assertFalse(any(e[-1] == curses.A_REVERSE for e in screen.frames[0]))
        systemd.start.assert_not_called()

    def test_service_action_groups_fit_and_preserve_focus(self):
        for language in ('en', 'pt'):
            labels = [Translator(language)('action_' + action) for action in
                      ('start', 'stop', 'restart', 'enable', 'disable')]
            for width in (40, 54, 90):
                with self.subTest(language=language, width=width):
                    positions = CursesMenu._backup_action_positions(
                        labels, width, separator_before=3)
                    self.assertEqual(len(positions), len(labels))
                    for (x, row), label in zip(positions, labels):
                        self.assertLessEqual(x + len(label) + 2, width)
                        self.assertGreaterEqual(row, 0)
                    previous_x, previous_row = positions[2]
                    startup_x, startup_row = positions[3]
                    if startup_row == previous_row:
                        self.assertEqual(startup_x - previous_x - len(labels[2]) - 2, 4)
                        self.assertEqual(move_action_focus(2, curses.KEY_RIGHT, positions), 3)
                    else:
                        self.assertGreater(startup_row, previous_row)
                    if width == 90:
                        self.assertEqual(startup_row, previous_row)
                        self.assertEqual(move_action_focus(3, curses.KEY_LEFT, positions), 2)

    def test_manage_renders_non_selectable_service_separator(self):
        for language in ('en', 'pt'):
            with self.subTest(language=language):
                screen = FakeScreen([27])
                menu = CursesMenu(screen, Translator(language))
                systemd = MagicMock()
                systemd.status.return_value = [self._state('netbox')]
                with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
                     patch('netbox_ops.tui.load_config'):
                    menu._manage()
                separators = [entry for entry in screen.frames[0] if entry[2] == '│']
                self.assertEqual(len(separators), 1)
                self.assertNotEqual(separators[0][-1], curses.A_REVERSE)
                systemd.start.assert_not_called()

    def test_manage_multi_select_and_action(self):
        # Two service checkboxes and A to choose a single action.
        screen = FakeScreen([curses.KEY_DOWN, 32, curses.KEY_DOWN, 32,
                             9, curses.KEY_RIGHT, curses.KEY_RIGHT, 10, 27, 27, 27, 27])
        menu = CursesMenu(screen, Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = [self._state(n) for n in
                                       ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
             patch('netbox_ops.tui.load_config'), \
             patch.object(menu, '_choose', return_value=2), \
             patch.object(menu, '_confirm_action', return_value=True) as confirmed, \
             patch.object(menu, '_execute_batch') as executed:
            menu._manage()
        confirmed.assert_called_once_with('restart', ['netbox', 'netbox-rq'])
        executed.assert_called_once_with(systemd, 'restart', ['netbox', 'netbox-rq'])

    def test_manage_never_changes_services_without_confirmation(self):
        screen = FakeScreen([curses.KEY_DOWN, 32, 9, 10, 27, 27, 27, 27])
        menu = CursesMenu(screen, Translator('pt'))
        systemd = MagicMock()
        systemd.status.return_value = [self._state(n) for n in
                                       ('netbox', 'netbox-rq', 'nginx', 'postgresql', 'redis')]
        with patch('netbox_ops.tui.SystemdServices', return_value=systemd), \
             patch('netbox_ops.tui.load_config'), \
             patch.object(menu, '_choose', return_value=0), \
             patch.object(menu, '_confirm_action', return_value=False), \
             patch.object(menu, '_show_lines'), \
             patch.object(menu, '_execute_batch') as executed:
            menu._manage()
        executed.assert_not_called()

    def test_progress_bar_counts_actual_completions(self):
        screen = FakeScreen([])
        menu = CursesMenu(screen, Translator('en'))
        menu._draw_progress('restart', 1, 4, 'redis', 'done', 'active')
        self.assertTrue(any('25%' in item[2] for item in screen.writes))
        self.assertTrue(any('1 of 4 services completed' in item[2] for item in screen.writes))


if __name__ == '__main__':
    unittest.main()
