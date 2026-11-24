"""Offline self-upgrade execution interface tests."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.cli import main
from netbox_ops.i18n import Translator
from netbox_ops.self_upgrade_compatibility import LocalEnvironment, UpgradeDecision
from netbox_ops.self_upgrade_installation import InstalledRelease
from netbox_ops.self_upgrade_releases import Version
from netbox_ops.self_upgrade_transaction import SwapError
from netbox_ops.self_upgrade_ui import HANDOFF_EXIT, Review, execute_review, status_lines
from netbox_ops.tui import CursesMenu
from tests.helpers.fake_screen import FakeScreen
from tests.tui.test_self_upgrade_tui import release


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.old = Version(0, 5, 0)
        self.middle = Version(0, 6, 0)
        self.target = Version(0, 7, 0)
        self.local = InstalledRelease(self.old, LocalEnvironment((3, 12), 'debian', 'archive'),
                                      Path('/usr/bin/python3.12'), (1, 2))
        self.review = Review(self.local, self.target,
                             UpgradeDecision('compatible', 'verified_plan', (self.middle, self.target)))
        self.prepare = self.start_patch('netbox_ops.self_upgrade_ui.prepare_next_release')
        self.launch = self.start_patch('netbox_ops.self_upgrade_ui.launch_prepared_release', return_value=42)
        self.discard = self.start_patch('netbox_ops.self_upgrade_ui.discard_prepared_release')
        self.start_patch('netbox_ops.self_upgrade_ui.os.geteuid', return_value=0)
        self.inspect = self.start_patch('netbox_ops.self_upgrade_ui.review_upgrade', return_value=self.review)

    def start_patch(self, name, *args, **kwargs):
        p = patch(name, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def cli(self, *args):
        with (patch('netbox_ops.cli.load_config', side_effect=AssertionError('config read')),
              patch('netbox_ops.cli.SystemdServices', side_effect=AssertionError('services used')),
              redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_plan_displays_intermediate_without_preparing(self):
        code, out, _ = self.cli('--lang', 'en', 'self-upgrade', 'plan', 'v0.7.0')
        self.assertEqual(code, 0)
        self.assertIn('v0.6.0 -> v0.7.0', out)
        self.prepare.assert_not_called()

    def test_cli_requires_exact_token_and_reports_submission_not_success(self):
        code, out, _ = self.cli('--lang', 'en', 'self-upgrade', 'install',
                                'v0.7.0', '--confirm', 'yes')
        self.assertEqual(code, 3)
        self.prepare.assert_not_called()
        code, out, _ = self.cli('--lang', 'en', 'self-upgrade', 'install',
                                'v0.7.0', '--confirm', 'UPGRADE v0.7.0')
        self.assertEqual(code, 0)
        self.assertIn('Upgrade submitted', out)
        self.assertNotIn('completed and validated', out)
        self.prepare.assert_called_once_with(self.target, expected_installed=self.local,
                                            expected_steps=(self.middle, self.target))
        self.launch.assert_called_once_with(self.prepare.return_value)

    def test_noninteractive_missing_token_fails(self):
        with patch('netbox_ops.self_upgrade_ui.sys.stdin.isatty', return_value=False):
            code, _, err = self.cli('--lang', 'en', 'self-upgrade', 'install', 'v0.7.0')
        self.assertEqual(code, 2)
        self.assertIn('UPGRADE v0.7.0', err)
        self.prepare.assert_not_called()

    def test_blocked_plan_never_prepares(self):
        self.inspect.return_value = replace(self.review, decision=UpgradeDecision(
            'unverified', 'missing_or_invalid_metadata', blocked_at=self.target))
        code, _, _ = self.cli('--lang', 'pt', 'self-upgrade', 'install',
                              'v0.7.0', '--confirm', self.review.token)
        self.assertEqual(code, 3)
        self.prepare.assert_not_called()

    def test_launch_failure_discards_unused_stage(self):
        self.launch.side_effect = SwapError('private detail')
        code, out, _ = self.cli('--lang', 'en', 'self-upgrade', 'install',
                                'v0.7.0', '--confirm', self.review.token)
        self.assertEqual(code, 1)
        self.discard.assert_called_once_with(self.prepare.return_value)
        self.assertNotIn('private detail', out)

    def test_nonroot_never_prepares(self):
        with patch('netbox_ops.self_upgrade_ui.os.geteuid', return_value=1000):
            self.assertEqual(execute_review(self.review, self.review.token,
                                           Translator('en'), lambda _: None), 3)
        self.prepare.assert_not_called()

    def test_status_distinguishes_intermediate_and_target(self):
        data = {'format': 1, 'installed': 'v0.5.0', 'step': 'v0.6.0',
                'target': 'v0.7.0', 'phase': 'completed'}
        with patch('netbox_ops.self_upgrade_ui.read_result', return_value=data):
            code, lines = status_lines(Translator('en'))
            self.assertEqual(code, 4)
            self.assertIn('intermediate step', ' '.join(lines))
        with patch('netbox_ops.self_upgrade_ui.read_result',
                   return_value={**data, 'step': 'v0.7.0'}):
            self.assertEqual(status_lines(Translator('en'))[0], 0)
        for phase, expected in [('submitted', 4), ('applying', 4), ('rolled_back', 1),
                                ('recovery_required', 1), ('failed', 1)]:
            with patch('netbox_ops.self_upgrade_ui.read_result', return_value={**data, 'phase': phase}):
                self.assertEqual(status_lines(Translator('pt'))[0], expected)

    def test_tui_confirmation_and_exit(self):
        menu = CursesMenu(FakeScreen(), Translator('en'))
        catalogue = MagicMock()
        catalogue.decision_for.return_value = self.review.decision
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=[release('v0.7.0')]),
              patch('netbox_ops.self_upgrade_ui.compatibility_catalogue', return_value=catalogue),
              patch.object(menu, '_choose', side_effect=[0, 0]),
              patch.object(menu, '_show_lines'),
              patch.object(menu, '_confirm_token', return_value=True) as confirm,
              patch.object(menu, '_frame', return_value=None)):
            self.assertEqual(menu._self_upgrade(), HANDOFF_EXIT)
        self.assertEqual(confirm.call_args.args[2], self.review.token)
        self.launch.assert_called_once()

    def test_tui_cancel_does_not_prepare(self):
        menu = CursesMenu(FakeScreen(), Translator('pt'))
        catalogue = MagicMock()
        catalogue.decision_for.return_value = self.review.decision
        with (patch('netbox_ops.self_upgrade_releases.discover_releases',
                    return_value=[release('v0.7.0')]),
              patch('netbox_ops.self_upgrade_ui.compatibility_catalogue', return_value=catalogue),
              patch.object(menu, '_choose', side_effect=[0, 0, None]),
              patch.object(menu, '_show_lines'),
              patch.object(menu, '_confirm_token', return_value=False)):
            self.assertIsNone(menu._self_upgrade())
        self.prepare.assert_not_called()
