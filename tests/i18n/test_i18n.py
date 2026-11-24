from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from netbox_ops.cli import build_parser, interactive_menu
from netbox_ops.i18n import CATALOGUE_DIR, Translator, detect_language


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


class I18nTests(unittest.TestCase):
    def test_detect_language(self):
        cases = (
            ({'LANG': 'pt_PT.UTF-8'}, 'pt'),
            ({'LANG': 'en_US.UTF-8'}, 'en'),
            ({'LANG': 'fr_FR.UTF-8'}, 'en'),
            ({'LANG': 'C.UTF-8'}, 'en'),
            ({}, 'en'),
            ({'LC_MESSAGES': 'pt_PT', 'LANG': 'en_GB'}, 'pt'),
            ({'LC_ALL': 'C', 'LC_MESSAGES': 'pt_PT'}, 'en'),
            ({'LC_ALL': '', 'LANG': 'pt-PT'}, 'pt'),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment), patch.dict('os.environ', environment, clear=True):
                self.assertEqual(detect_language(), expected)
                self.assertEqual(build_parser().parse_args([]).lang, expected)

    def test_explicit_language_overrides_locale(self):
        with patch.dict('os.environ', {'LANG': 'pt_PT.UTF-8'}, clear=True):
            self.assertEqual(build_parser().parse_args(['--lang', 'en']).lang, 'en')
        with patch.dict('os.environ', {'LC_ALL': 'C'}, clear=True):
            self.assertEqual(build_parser().parse_args(['--lang', 'pt']).lang, 'pt')

    def test_key_parity(self):
        pt = json.loads((CATALOGUE_DIR / 'pt.json').read_text())
        en = json.loads((CATALOGUE_DIR / 'en.json').read_text())
        self.assertEqual(set(pt), set(en))
        for key in pt:
            self.assertTrue(pt[key])
            self.assertTrue(en[key])

    def test_translation(self):
        self.assertIn('Gerir', Translator('pt')('menu_2'))
        self.assertIn('Manage', Translator('en')('menu_2'))

    def test_menu_language_switch_and_stub(self):
        answers = iter(['l', '2', '3', '0'])
        with patch('builtins.input', side_effect=lambda *_: next(answers)):
            with redirect_stdout(io.StringIO()) as output:
                rc = interactive_menu(Translator('pt'))
        self.assertEqual(rc, 0)
        self.assertIn('Language changed to English', output.getvalue())
        self.assertIn('[NOT IMPLEMENTED] updates check', output.getvalue())
