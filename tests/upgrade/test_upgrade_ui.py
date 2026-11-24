"""Offline tests for the shared upgrade interface."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.cli import build_parser, dispatch
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.plugin_compatibility import PluginCompatibility, PluginReview
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.tui import CursesMenu
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_plan import build_upgrade_plan
from netbox_ops.upgrade_ui import (
    UpgradeContext, execute_plan, make_plan, official_tags, parse_plugins,
)
from tests.helpers.fake_screen import FakeScreen


class InterfaceFixture(unittest.TestCase):
    def setUp(self):
        root = Path('/opt/netbox')
        self.installation = GitNetBoxInstallation(
            root, '4.4.0', 'a' * 40, 'v4.4.0', 'HEAD',
            'https://github.com/netbox-community/netbox.git',
            root / 'venv/bin/python', '3.12.3', (), (),
        )
        self.inventory = PluginInventory(
            root, 'hash', (), (
                InstalledPackage('example-plugin', '1.0.0', ('example_plugin',)),
                InstalledPackage('django', '5.2.6', ('django',)),
            ), ('example_plugin',), (), (),
        )
        self.context = UpgradeContext(self.installation, self.inventory,
                                      ('v4.4.2', 'v4.4.1', 'v4.4.0'), '15.2', '7.2')
        self.cfg = Configuration(DEFAULT_UNITS)
        self.t = Translator('en')

    def reviewer(self, package, stages, python_versions, *, version, preflights):
        version = version or '2.0.0'
        return PluginReview(package, version, None, tuple(
            PluginCompatibility(package, version, stage, runtime, 'compatible', (), ())
            for stage, runtime in zip(stages, python_versions)
        ), (), release_status='verified')

    def build(self, operation='netbox', target='4.4.2', selected=None):
        with patch('netbox_ops.upgrade_ui.build_upgrade_plan',
                   side_effect=lambda *args, **kwargs: build_upgrade_plan(
                       *args, **kwargs, reviewer=self.reviewer)):
            return make_plan(self.context, operation, target, selected or {})


class SharedInterfaceTests(InterfaceFixture):
    def test_stable_official_tags_are_sorted_and_prereleases_excluded(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='\n'.join(
            f'{"a" * 40}\trefs/tags/{tag}' for tag in
            ('v4.4.2', 'v4.4.10', 'v4.5.0-beta1', 'other', 'v4.4.2')
        )))
        self.assertEqual(official_tags(runner=runner), ('v4.4.10', 'v4.4.2'))
        self.assertIn('ls-remote', runner.call_args.args[0])

    def test_plugin_selection_excludes_unrelated_installed_packages(self):
        self.assertEqual(self.context.plugins, (('example-plugin', '1.0.0'),))
        self.assertEqual(parse_plugins(['example_plugin==2.0.0']), {'example-plugin': '2.0.0'})
        with self.assertRaises(UpgradeExecutionError):
            parse_plugins(['example-plugin', 'example_plugin'])
        with self.assertRaises(UpgradeExecutionError):
            self.build('plugins', None, {'django': '6.0'})

    def test_plan_fingerprint_binds_runtime_and_inventory(self):
        first = self.build()
        self.context = replace(self.context, installation=replace(
            self.installation, python=Path('/opt/netbox/venv/bin/python3'),
        ))
        second = self.build()
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_wrong_fingerprint_or_unknown_confirmation_never_executes(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        with patch('netbox_ops.upgrade_ui.execute_netbox_upgrade') as executor:
            for fingerprint, unknowns in (('wrong', True), (plan.fingerprint, False)):
                with self.subTest(fingerprint=fingerprint), self.assertRaises(UpgradeExecutionError):
                    execute_plan(self.cfg, 'netbox', plan, fingerprint=fingerprint,
                                 acknowledge_unknowns=unknowns, health_url='https://host/api/status/')
            executor.assert_not_called()

    def test_dispatches_all_three_executors_and_rebuilds_exact_selection(self):
        for operation, target, selected, name in (
            ('netbox', '4.4.2', {}, 'execute_netbox_upgrade'),
            ('plugins', None, {'example-plugin': '2.0.0'}, 'execute_plugin_upgrade'),
            ('stack', '4.4.2', {'example-plugin': '2.0.0'}, 'execute_combined_upgrade'),
        ):
            plan = self.build(operation, target, selected)
            with patch('netbox_ops.upgrade_ui.' + name) as executor, patch(
                'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
            ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan) as rebuild:
                execute_plan(self.cfg, operation, plan, fingerprint=plan.fingerprint,
                             acknowledge_unknowns=True, health_url='https://host/api/status/')
                args, kwargs = executor.call_args
                self.assertEqual(args[2].fingerprint, plan.fingerprint)
                self.assertIs(kwargs['rebuild_plan'](), plan)
                self.assertEqual(rebuild.call_args.args[1:], (operation, target, selected))


class UpgradeCliTests(InterfaceFixture):
    def invoke(self, argv, plan):
        args = build_parser().parse_args(['upgrade', *argv])
        with patch('netbox_ops.cli.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan), patch(
            'netbox_ops.upgrade_ui.execute_plan',
            return_value=SimpleNamespace(directory=Path('/backup/pre-upgrade-test')),
        ) as execute, redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            result = dispatch(args, self.t)
        return result, out.getvalue(), execute

    def test_preview_default_and_details_are_read_only(self):
        plan = self.build()
        result, output, execute = self.invoke(['netbox', '--version', '4.4.2', '--details'], plan)
        self.assertEqual(result, 0)
        self.assertIn(plan.fingerprint, output)
        self.assertIn('4.4.0 -> 4.4.2', output)
        execute.assert_not_called()

    def test_execute_requires_exact_fingerprint_and_url(self):
        plan = self.build()
        for options in ([], ['--approve-plan', 'wrong', '--health-url', 'https://host/api/status/']):
            result, _, execute = self.invoke(['netbox', '--execute', *options], plan)
            self.assertEqual(result, 1)
            execute.assert_not_called()

    def test_blocked_and_uncertain_plan_cannot_be_implicitly_approved(self):
        for plan in (replace(self.build(), blockers=('conflict',)),
                     replace(self.build(), requires_strong_confirmation=True)):
            result, _, execute = self.invoke([
                'netbox', '--execute', '--approve-plan', plan.fingerprint,
                '--health-url', 'https://host/api/status/',
            ], plan)
            self.assertEqual(result, 1)
            execute.assert_not_called()

    def test_explicit_execution_uses_the_displayed_plan(self):
        plan = self.build()
        result, output, execute = self.invoke([
            'netbox', '--execute', '--approve-plan', plan.fingerprint,
            '--accept-unknowns', '--health-url', 'https://host/api/status/',
        ], plan)
        self.assertEqual(result, 0)
        self.assertIn('/backup/pre-upgrade-test', output)
        self.assertIs(execute.call_args.args[2], plan)
        self.assertEqual(execute.call_args.kwargs['fingerprint'], plan.fingerprint)


class UpgradeTuiTests(InterfaceFixture):
    def run_menu(self, plan, choices, confirmations):
        menu = CursesMenu(FakeScreen(), self.t)
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan) as build, patch(
            'netbox_ops.upgrade_ui.execute_plan',
            return_value=SimpleNamespace(directory=Path('/backup/pre-upgrade-test')),
        ) as execute, patch.object(menu, '_upgrade_job',
                                    side_effect=lambda title, worker: worker(Mock())), patch.object(
            menu, '_choose', side_effect=choices,
        ), patch.object(menu, '_show_lines'), patch.object(
            menu, '_upgrade_input', side_effect=['https://host/api/status/', ''],
        ), patch.object(menu, '_confirm_token', side_effect=confirmations) as confirm:
            menu._upgrade('netbox')
        return build, execute, confirm

    def test_cancel_confirmation_never_executes(self):
        _, execute, confirm = self.run_menu(self.build(), [1, 3, None], [False])
        confirm.assert_called_once()
        execute.assert_not_called()

    def test_uncertain_plan_needs_second_confirmation(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        _, execute, confirm = self.run_menu(plan, [1, 3, None], [True, False])
        self.assertEqual([call.args[2] for call in confirm.call_args_list], ['UPGRADE', 'UNCERTAIN'])
        execute.assert_not_called()

    def test_approved_plan_executes_once(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        _, execute, confirm = self.run_menu(plan, [1, 3], [True, True])
        self.assertEqual(confirm.call_count, 2)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs['fingerprint'], plan.fingerprint)

    def test_target_edit_invalidates_old_plan(self):
        plan = self.build()
        build, execute, _ = self.run_menu(plan, [1, 0, 1, 1, None], [])
        self.assertEqual(build.call_count, 2)
        self.assertEqual(build.call_args.args[2], '4.4.1')
        execute.assert_not_called()

    def test_authorization_input_is_not_echoed(self):
        screen = FakeScreen([*(ord(c) for c in 'secret'), 10])
        menu = CursesMenu(screen, self.t)
        self.assertEqual(menu._upgrade_input('Upgrade', 'Auth', secret=True), 'secret')
        self.assertFalse(any('secret' in entry[2] for entry in screen.writes))


if __name__ == '__main__':
    unittest.main()
