"""Confirmation failures stay inside the original TUI view and never authorize work."""
import curses
import unittest
from unittest.mock import patch

from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class InlineConfirmationTests(unittest.TestCase):
    def exercise(self, language, service, keys, dimensions=(24, 100)):
        t = Translator(language)
        for key in ('nbo_confirm_failed_title', 'nbo_confirm_failed_message',
                    'tui_confirm_token', 'tui_confirm_prompt', 'tui_confirm_hint'):
            self.assertIn(key, t.messages, f'{language}: missing {key}')
        screen = FakeScreen(keys, dimensions=dimensions)
        menu = CursesMenu(screen, t)
        menu.colors['failure'] = 1234
        title = t('tui_confirm_title') if service else 'Test operation'
        with patch.object(menu, '_show_lines') as popup:
            if service:
                result = menu._confirm_action('restart', ['postgresql'])
            else:
                result = menu._confirm_token(title, 'Review this operation.', 'UPGRADE')
        popup.assert_not_called()
        self.assertEqual(list(screen.keys), [])
        for frame in screen.frames:
            self.assertTrue(any(y == 1 and text == title for y, _, text, _ in frame))
        return result, screen.frames, t

    @staticmethod
    def errors(frame):
        return ' '.join(text for _, _, text, attr in frame if attr == 1234)

    def test_repeated_invalid_attempts_clear_input_then_exact_retry_succeeds(self):
        for language in LANGUAGES:
            for service, token in ((False, 'UPGRADE'), (True, 'RESTART postgresql')):
                with self.subTest(language=language, service=service):
                    keys = [*map(ord, 'wrong'), 10, 10, *map(ord, token), 10]
                    result, frames, t = self.exercise(language, service, keys)
                    self.assertTrue(result)
                    for index in (6, 7):
                        self.assertIn(t('nbo_confirm_failed_title'), self.errors(frames[index]))
                        self.assertIn(t('nbo_confirm_failed_message'), self.errors(frames[index]))
                        self.assertTrue(any(text == t('tui_confirm_prompt')
                                            for _, _, text, _ in frames[index]))
                    self.assertFalse(self.errors(frames[8]))
                    self.assertTrue(any(text == t('tui_confirm_prompt') + token[0]
                                        for _, _, text, _ in frames[8]))
                    self.assertFalse(self.errors(frames[-1]))

    def test_wrong_case_then_escape_does_not_confirm(self):
        for language in LANGUAGES:
            for service, token in ((False, 'upgrade'), (True, 'restart postgresql')):
                with self.subTest(language=language, service=service):
                    result, frames, t = self.exercise(language, service, [*map(ord, token), 10, 27])
                    self.assertFalse(result)
                    self.assertIn(t('nbo_confirm_failed_title'), self.errors(frames[-1]))

    def test_escape_without_submission_has_no_error(self):
        for language in LANGUAGES:
            for service in (True, False):
                with self.subTest(language=language, service=service):
                    result, frames, _ = self.exercise(language, service, [ord('x'), 27])
                    self.assertFalse(result)
                    self.assertTrue(all(not self.errors(frame) for frame in frames))

    def test_small_terminal_keeps_error_and_input_clear_of_footer(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                result, frames, t = self.exercise(language, True, [10, 27], (16, 50))
                self.assertFalse(result)
                frame = frames[-1]
                self.assertIn(t('nbo_confirm_failed_title'), self.errors(frame))
                input_y = next(y for y, _, text, _ in frame if text == t('tui_confirm_prompt'))
                error_rows = [y for y, _, _, attr in frame if attr == 1234]
                self.assertTrue(all(input_y < y < 14 for y in error_rows))

    def test_target_list_remains_scrollable_after_invalid_token(self):
        screen = FakeScreen([10, curses.KEY_DOWN, 27], dimensions=(16, 50))
        menu = CursesMenu(screen, Translator('en'))
        menu.colors['failure'] = 1234
        names = [f'backup-{i}.dump' for i in range(5)]
        self.assertFalse(menu._confirm_token('Delete', 'Review targets.', 'DELETE 5 BACKUPS',
                                             target_names=names))
        frame = screen.frames[-1]
        self.assertTrue(self.errors(frame))
        self.assertTrue(any('2. backup-1.dump' in text for _, _, text, _ in frame))
        error_rows = [y for y, _, _, attr in frame if attr == 1234]
        self.assertTrue(all(y < 14 for y in error_rows))

    def test_token_is_teal_bold_and_input_is_adjacent_on_tall_terminal(self):
        for language in LANGUAGES:
            for colored in (True, False):
                screen = FakeScreen([27], dimensions=(45, 140))
                menu = CursesMenu(screen, Translator(language))
                if colored:
                    menu.colors.update(title=1, title_panel=2, text=3, text_panel=4)
                self.assertFalse(menu._confirm_token('Create', 'Create database backup?', 'CREATE'))
                frame = screen.frames[-1]
                token = next((y, attr) for y, _, text, attr in frame if text == 'CREATE')
                field = next(y for y, _, text, _ in frame if text == menu.t('tui_confirm_prompt'))
                self.assertEqual(token[1], (2 if colored else 0) | curses.A_BOLD)
                self.assertEqual(field, token[0] + 1)
                self.assertLess(field, 12)
                self.assertEqual(len(menu._panels), 1)

    def test_long_targets_and_warning_remain_accessible_after_resize(self):
        for language in LANGUAGES:
            screen = FakeScreen(dimensions=(16, 50))
            menu = CursesMenu(screen, Translator(language))
            menu.colors['warning'] = 456
            targets = ['backup-' + 'x' * 100 + '.dump', 'final-backup.dump']
            summary = [('Irreversible deletion', 'warning'), ('Review targets', '')]
            offset = 0
            seen = []
            for _ in range(15):
                frame = menu._frame('Delete', menu.t('tui_confirm_hint'))
                offset, visible = menu._confirmation_panel(
                    *frame, summary, '', token='DELETE 2 BACKUPS', invalid=True,
                    target_names=targets, offset=offset)
                screen.refresh()
                self.assertTrue(any(text == 'Irreversible deletion' and attr == 456
                                    for _, _, text, attr in screen.frames[-1]))
                self.assertTrue(all(y < 14 for y, _, text, _ in screen.frames[-1]
                                    if text == menu.t('tui_confirm_prompt')))
                seen.extend(text for _, _, text, _ in screen.frames[-1])
                offset = menu._confirmation_scroll(curses.KEY_DOWN, offset, visible)
            self.assertTrue(any('final-backup.dump' in text for text in seen))
            self.assertTrue(any('x' * 30 in text for text in seen))
            screen.dimensions = (45, 140)
            frame = menu._frame('Delete', menu.t('tui_confirm_hint'))
            offset, _ = menu._confirmation_panel(
                *frame, summary, '', token='DELETE 2 BACKUPS', target_names=targets, offset=offset)
            self.assertEqual(offset, 0)

    def test_secret_input_is_masked_and_compact(self):
        for language in LANGUAGES:
            screen = FakeScreen([*map(ord, 'secret-value'), 10], dimensions=(45, 100))
            menu = CursesMenu(screen, Translator(language))
            self.assertEqual(menu._upgrade_input('Authorization', 'Enter optional header', secret=True),
                             'secret-value')
            self.assertFalse(any('secret-value' in text for frame in screen.frames
                                 for _, _, text, _ in frame))
            self.assertTrue(any(y < 12 and text == '> ' + '*' * 12
                                for y, _, text, _ in screen.frames[-1]))
            self.assertEqual(len(menu._panels), 1)
