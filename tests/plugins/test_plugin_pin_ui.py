"""Exercise pin approval without writing a live installation."""
import curses
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netbox_ops.cli import build_parser
from netbox_ops.i18n import Translator
from netbox_ops.plugin_inventory import InstalledPackage, PinProposal, PluginInventory, PluginInventoryError
from netbox_ops.plugin_pin_ui import build_pin_review, apply_pin_review, pin_review_lines, run_pin_cli
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen


class PinReviewTests(unittest.TestCase):
    def setUp(self):
        self.inventory = PluginInventory(
            Path('/opt/netbox'), 'hash', (), (InstalledPackage('sample', '1.0', ()),),
            ('sample',), (
                PinProposal('sample', '1.0', 1, 'sample # keep\r\n', 'sample==1.0 # keep\r\n'),
                PinProposal('other', '2.0', None, '', 'other==2.0\r\n'),
            ), (),
        )
        self.t = Translator('en')

    def invoke(self, argv, inventory=None):
        args = build_parser().parse_args(['upgrade', 'pins', *argv])
        with patch('netbox_ops.plugin_pin_ui.inspect_git_installation'), patch(
            'netbox_ops.plugin_pin_ui.inventory_plugins', return_value=inventory or self.inventory,
        ), patch('netbox_ops.plugin_pin_ui.apply_approved_pins') as apply, redirect_stdout(
            io.StringIO(),
        ) as out, redirect_stderr(io.StringIO()):
            result = run_pin_cli(args, self.t)
        return result, out.getvalue(), apply

    def test_default_review_is_read_only_and_displays_exact_edits(self):
        result, output, apply = self.invoke([])
        self.assertEqual(result, 0)
        self.assertIn('sample==1.0 # keep', output)
        self.assertIn('\\r\\n', output)
        self.assertIn('Pin review fingerprint:', output)
        apply.assert_not_called()

    def test_apply_requires_selection_and_exact_approval(self):
        review = build_pin_review(self.inventory, ('sample',))
        for args in (
            ['--apply'],
            ['--plugin', 'sample', '--apply'],
            ['--all', '--apply', '--approve-pins', review.fingerprint],
            ['--plugin', 'sample', '--approve-pins', review.fingerprint],
        ):
            result, _, apply = self.invoke(args)
            self.assertEqual(result, 1)
            apply.assert_not_called()
        result, _, apply = self.invoke(['--plugin', 'sample', '--apply', '--approve-pins', review.fingerprint])
        self.assertEqual(result, 0)
        apply.assert_called_once_with(self.inventory, ('sample',), approved=True)

    def test_stale_preview_and_forged_selection_are_rejected(self):
        review = build_pin_review(self.inventory, ('sample',))
        stale = replace(self.inventory, requirements_hash='different')
        result, _, apply = self.invoke(['--plugin', 'sample', '--apply', '--approve-pins', review.fingerprint], stale)
        self.assertEqual(result, 1)
        apply.assert_not_called()
        with patch('netbox_ops.plugin_pin_ui.apply_approved_pins') as backend:
            with self.assertRaises(PluginInventoryError):
                apply_pin_review(replace(review, selected=('other',)), review.fingerprint)
            backend.assert_not_called()

    def test_many_pins_can_be_reviewed_on_small_terminal(self):
        proposals = tuple(PinProposal(f'plugin-{i}', '1.0', None, '', f'plugin-{i}==1.0\n')
                          for i in range(25))
        inventory = replace(self.inventory, proposals=proposals)
        review = build_pin_review(inventory, tuple(p.package for p in proposals))
        lines = pin_review_lines(review, self.t)
        screen = FakeScreen([curses.KEY_NPAGE] * 10 + [10], dimensions=(16, 50))
        menu = CursesMenu(screen, self.t)
        menu._show_lines('Pins', [(line, '') for line in lines], allow_quit=True)
        self.assertTrue(any('plugin-9' in entry[2] for entry in screen.writes))
        self.assertTrue(any('/' in entry[2] for entry in screen.writes))
