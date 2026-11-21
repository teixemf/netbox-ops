from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from netbox_ops.cli import interactive_menu
from netbox_ops.i18n import CATALOGUE_DIR, Translator


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


class I18nTests(unittest.TestCase):
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
