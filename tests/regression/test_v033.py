"""v0.3.3: chosen palette, round Braille donut, no redundant results page."""
from __future__ import annotations

import curses
import unittest
from unittest.mock import patch, MagicMock
from netbox_ops.tui import CursesMenu
from netbox_ops.i18n import Translator
from netbox_ops.services import ServiceError
from tests.helpers.fake_screen import FakeScreen

class AutoAcknowledgeScreen(FakeScreen):
    def getch(self):
        return 27

class V033Tests(unittest.TestCase):
    def test_braille_donut_is_round_and_uses_three_classes(self):
        data=CursesMenu._braille_donut(100,350,550,1000)
        self.assertEqual(len(data),9)
        self.assertTrue(all(len(row)==18 for row in data))
        on={(x,y) for y,row in enumerate(data) for x,(c,t) in enumerate(row) if c!=' '}
        self.assertTrue(on)
        self.assertLess(max(x for x,y in on)-min(x for x,y in on),18)
        self.assertEqual({t for row in data for c,t in row if c != ' '},
                         {'disk_backup','disk_other','disk_free'})
        self.assertEqual(data[4][8][0], ' ')
        self.assertEqual(data[4][9][0], ' ')

    def test_service_progress_keeps_last_page_without_show_lines(self):
        screen=AutoAcknowledgeScreen(dimensions=(31,110))
        m=CursesMenu(screen,Translator('pt'))
        def perform(systemd,action,names,on_progress):
            on_progress('running',0,1,'netbox','')
            on_progress('done',1,1,'netbox','active')
            return [('netbox','active')],None
        with patch('netbox_ops.cli.perform_service_batch',side_effect=perform),\
             patch.object(m,'_show_lines') as show:
            m._execute_batch(MagicMock(),'restart',['netbox'])
        show.assert_not_called()
        words=' '.join(w[2] for w in screen.frames[-1])
        self.assertIn('Operação concluída',words)
        self.assertIn('OK',words)
        self.assertIn('100%',words)
        self.assertIn('Enter/Esc',words)

    def test_service_failure_stays_on_same_progress_screen(self):
        screen=AutoAcknowledgeScreen(dimensions=(31,110))
        m=CursesMenu(screen,Translator('en'))
        def perform(systemd,action,names,on_progress):
            on_progress('running',0,1,'redis','')
            on_progress('error',0,1,'redis','denied')
            return [],'redis: denied'
        with patch('netbox_ops.cli.perform_service_batch',side_effect=perform),\
             patch.object(m,'_show_lines') as show:
            m._execute_batch(MagicMock(),'restart',['redis'])
        show.assert_not_called()
        words=' '.join(w[2] for w in screen.frames[-1])
        self.assertIn('denied',words)
        self.assertIn('!!',words)
        self.assertNotIn('100%',words)

    def test_successful_backup_final_panel_not_extra_result_screen(self):
        screen=AutoAcknowledgeScreen(dimensions=(32,110))
        m=CursesMenu(screen,Translator('pt'))
        def worker(progress):
            progress('prepare',0,2,'')
            progress('dump_db',1,2,'')
            progress('complete',2,2,'')
            return 'backup.dump'
        with patch.object(m,'_show_lines') as show:
            self.assertTrue(m._run_backup_job('Backup',worker))
        show.assert_not_called()
        words=' '.join(w[2] for w in screen.frames[-1])
        self.assertIn('100%',words) if '100%' in words else self.assertIn('2/2',words)
        self.assertIn('backup.dump',words)
        self.assertIn('Operação concluída',words)
        self.assertIn('Enter/Esc',words)

    def test_failed_backup_final_panel_keeps_error_and_steps(self):
        screen=AutoAcknowledgeScreen(dimensions=(32,110))
        m=CursesMenu(screen,Translator('pt'))
        def worker(progress):
            progress('prepare',0,3,'')
            progress('dump_db',1,3,'')
            raise ServiceError('simulated pg_dump error')
        with patch.object(m,'_show_lines') as show:
            self.assertFalse(m._run_backup_job('Backup',worker))
        show.assert_not_called()
        words=' '.join(w[2] for w in screen.frames[-1])
        self.assertIn('simulated pg_dump error',words)
        self.assertIn('!!',words)

    def test_selected_palette_has_dark_background_and_deep_teal_selection(self):
        screen=FakeScreen()
        m=CursesMenu(screen,Translator('en'))
        with patch('netbox_ops.tui.curses.has_colors',return_value=True),\
             patch('netbox_ops.tui.curses.start_color'),\
             patch('netbox_ops.tui.curses.use_default_colors'),\
             patch('netbox_ops.tui.curses.COLORS',256,create=True),\
             patch('netbox_ops.tui.curses.init_pair') as pairs,\
             patch('netbox_ops.tui.curses.color_pair',side_effect=lambda n:n),\
             patch('netbox_ops.tui.curses.curs_set'),\
             patch('netbox_ops.tui.curses.set_escdelay'),\
             patch.dict('os.environ', {}, clear=True):
            m._init_screen()
        used=[c.args for c in pairs.call_args_list]
        self.assertIn((1,255,233),used)
        self.assertIn((6,255,23),used)

if __name__=='__main__':
    unittest.main()
