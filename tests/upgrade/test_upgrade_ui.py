"""Offline tests for the shared upgrade interface."""
from __future__ import annotations

import curses
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.cli import build_parser, dispatch
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import LANGUAGES, Translator
from netbox_ops.plugin_compatibility import PluginCompatibility, PluginReview, ReleaseChoice
from netbox_ops.plugin_inventory import DeclaredRequirement, InstalledPackage, PinProposal, PluginInventory
from netbox_ops.tui import CursesMenu
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_plan import build_upgrade_plan
from netbox_ops.upgrade_tui import netbox_overview
from netbox_ops.upgrade_ui import (
    UpgradeContext, alternative_release_choices, execute_plan, make_plan, official_tags, parse_plugins, summary_lines,
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
            root, 'hash', (DeclaredRequirement(1, 'django', 'pinned', '5.2.6'),), (
                InstalledPackage('example-plugin', '1.0.0', ('example_plugin',)),
                InstalledPackage('django', '5.2.6', ('django',)),
            ), ('example_plugin',), (), (),
        )
        self.context = UpgradeContext(self.installation, self.inventory,
                                      ('v4.4.2', 'v4.4.1', 'v4.4.0'), '15.2', '7.2')
        self.cfg = Configuration(DEFAULT_UNITS)
        self.t = Translator('en')
        self.enterContext(patch('netbox_ops.upgrade_ui.review_plan_dependencies', side_effect=lambda plan, cfg: plan))

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

    def test_non_plugin_alternative_release_discovery_is_rejected(self):
        with patch('netbox_ops.upgrade_ui.fetch_pypi_json') as fetch:
            with self.assertRaisesRegex(UpgradeExecutionError, 'configured plugin'):
                alternative_release_choices(self.context, 'django')
        fetch.assert_not_called()

    def test_dynamic_and_ambiguous_plugin_configuration_is_reported(self):
        for inventory in (
            replace(self.inventory, enabled=None),
            replace(self.inventory, packages=self.inventory.packages + (
                InstalledPackage('ambiguous', '1.0.0', ('example_plugin',)),)),
        ):
            context = replace(self.context, inventory=inventory)
            self.assertTrue(context.plugin_selection_incomplete)
            self.assertEqual(context.plugins, ())
            with self.assertRaisesRegex(UpgradeExecutionError, 'configured plugins'):
                make_plan(context, 'plugins', None, {'example-plugin': None})

    def test_plan_fingerprint_binds_runtime_and_inventory(self):
        first = self.build()
        self.context = replace(self.context, installation=replace(
            self.installation, python=Path('/opt/netbox/venv/bin/python3'),
        ))
        second = self.build()
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_alternative_versions_are_read_only_published_candidates(self):
        project = {'releases': {
            '3.0.0': [{'packagetype': 'bdist_wheel', 'requires_python': '>=3.13'}],
            '2.0.0': [{'packagetype': 'bdist_wheel', 'requires_python': '>=3.12'}],
            '2.1.0': [{'packagetype': 'bdist_wheel', 'yanked': True}],
        }}
        with patch('netbox_ops.upgrade_ui.fetch_pypi_json', return_value=project):
            choices = alternative_release_choices(self.context, 'example-plugin')
        self.assertEqual([(c.version, c.status) for c in choices], [('3.0.0', 'blocked'), ('2.0.0', 'ok')])

    def test_wrong_fingerprint_or_unknown_confirmation_never_executes(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        with patch('netbox_ops.upgrade_ui.execute_netbox_upgrade') as executor:
            for fingerprint, unknowns in (('wrong', True), (plan.fingerprint, False)):
                with self.subTest(fingerprint=fingerprint), self.assertRaises(UpgradeExecutionError):
                    execute_plan(self.cfg, 'netbox', plan, fingerprint=fingerprint,
                                 acknowledge_unknowns=unknowns, health_url='https://host/api/status/')
            executor.assert_not_called()

    def test_unverified_target_is_displayed_but_never_executed(self):
        def unavailable(*args, **kwargs):
            return replace(self.reviewer(*args, **kwargs), release_status='unknown')

        with patch('netbox_ops.upgrade_ui.build_upgrade_plan',
                   side_effect=lambda *args, **kwargs: build_upgrade_plan(
                       *args, **kwargs, reviewer=unavailable)):
            plan = make_plan(self.context, 'plugins', None, {'example-plugin': '9.0.0'})
        self.assertIn('example-plugin: 1.0.0 -> 9.0.0', summary_lines(plan, self.t))
        with patch('netbox_ops.upgrade_ui.execute_plugin_upgrade') as executor:
            with self.assertRaises(UpgradeExecutionError):
                execute_plan(self.cfg, 'plugins', plan, fingerprint=plan.fingerprint,
                             acknowledge_unknowns=True, health_url='https://host/api/status/')
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

    def test_cli_select_all_uses_only_configured_plugins(self):
        cases = (
            (['plugins', '--all'], 'plugins', None),
            (['stack', '--netbox-version', '4.4.2', '--plugins', 'all'], 'stack', '4.4.2'),
        )
        for argv, operation, target in cases:
            with self.subTest(operation=operation):
                args = build_parser().parse_args(['upgrade', *argv])
                plan = self.build(operation, target, {'example-plugin': None})
                with patch('netbox_ops.cli.load_config', return_value=self.cfg), patch(
                    'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
                ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan) as build, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(dispatch(args, self.t), 0)
                build.assert_called_once_with(self.context, operation, target, {'example-plugin': None})

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

    def test_tui_validation_uses_exact_eligible_plugin_target(self):
        from netbox_ops.upgrade_dashboard import UpgradeDashboard
        from netbox_ops.upgrade_targets import TargetModel, TargetSelection

        menu = CursesMenu(FakeScreen(), self.t)
        dashboard = UpgradeDashboard(menu)
        dashboard.model = TargetModel(self.context)
        dashboard.model.selection = TargetSelection('4.4.0', (('example-plugin', '2.0.0'),))
        plan = self.build('plugins', None, {'example-plugin': '2.0.0'})
        with patch('netbox_ops.upgrade_dashboard.make_plan', return_value=plan) as build, patch.object(
            menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()),
        ):
            dashboard._validate()
        build.assert_called_once_with(self.context, 'plugins', None, {'example-plugin': '2.0.0'})
        self.assertTrue(dashboard._enabled('execute'))

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

    def test_alternative_selection_rebuilds_plan(self):
        menu = CursesMenu(FakeScreen(), self.t)
        choices = (ReleaseChoice('1.8.0', '>=3.12', 'ok'),)
        plan = self.build('plugins', None, {'example-plugin': '1.8.0'})
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan) as build, patch(
            'netbox_ops.upgrade_ui.alternative_release_choices', return_value=choices,
        ), patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())), patch.object(
            menu, '_choose', side_effect=[0, 2, 3, 0, None, 1, None],
        ), patch.object(menu, '_show_lines'):
            menu._upgrade('plugins')
        self.assertEqual(build.call_args.args[3], {'example-plugin': '1.8.0'})

    def test_overview_uses_actual_versions_and_explicit_unverified_states(self):
        for language in LANGUAGES:
            t = Translator(language)
            for key in ('upgrade_status_not_checked', 'upgrade_overview_counts',
                        'upgrade_overview_more', 'upgrade_overview_hint',
                        'upgrade_overview_details', 'upgrade_refresh', 'upgrade_review_hint'):
                self.assertIn(key, t.messages)
            for operation, target, selected in (
                ('netbox', '4.4.2', {}),
                ('plugins', None, {'example-plugin': '2.0.0'}),
                ('stack', '4.4.2', {'example-plugin': '2.0.0'}),
            ):
                with self.subTest(language=language, operation=operation):
                    rows = netbox_overview(self.context, target, selected, None, t, operation=operation)
                    lines = [line for line, _ in rows]
                    self.assertIn(t('upgrade_plan_status', status=t('upgrade_status_not_checked')), lines)
                    self.assertIn('4.4.0', lines[0])
                    self.assertIn('example-plugin: 1.0.0', ' '.join(lines))
                    self.assertNotIn('django:', ' '.join(lines))
                    self.assertNotIn('success', [style for _, style in rows])
                    plan = self.build(operation, target, selected)
                    checked = netbox_overview(self.context, target, selected, plan, t, operation=operation)
                    self.assertIn(t('upgrade_dependency_not-run'), ' '.join(line for line, _ in checked))
                    self.assertIn(t('upgrade_stage', index=1, total=len(plan.stages),
                                    version=plan.stages[0].netbox_version), [line for line, _ in checked])

    def test_blocked_plan_never_offers_execute(self):
        menu = CursesMenu(FakeScreen(), self.t)
        plan = replace(self.build(), blockers=('incompatible dependency',))
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', return_value=plan), patch.object(
            menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()),
        ), patch.object(menu, '_choose', side_effect=[1, 2, None]) as choose, patch.object(
            menu, '_show_lines',
        ) as show, patch('netbox_ops.upgrade_ui.execute_plan') as execute:
            menu._upgrade('netbox')
        self.assertNotIn(self.t('upgrade_execute'), [label for label, _ in choose.call_args_list[1].args[1]])
        self.assertIn(self.t('upgrade_status_blocked'), str(choose.call_args_list[1].kwargs['overview']))
        self.assertIn('incompatible dependency', str(show.call_args_list))
        execute.assert_not_called()

    def test_refresh_invalidates_review_and_unavailable_target(self):
        menu = CursesMenu(FakeScreen(), self.t)
        fresh = replace(self.context, tags=('v4.4.1',))
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', side_effect=[self.context, fresh],
        ) as discover, patch('netbox_ops.upgrade_ui.make_plan', return_value=self.build()), patch.object(
            menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()),
        ), patch.object(menu, '_choose', side_effect=[1, 5, None]) as choose, patch(
            'netbox_ops.upgrade_ui.execute_plan',
        ) as execute:
            menu._upgrade('netbox')
        self.assertEqual(discover.call_count, 2)
        self.assertIn(self.t('upgrade_target', target='?'), [label for label, _ in choose.call_args.args[1]])
        self.assertNotIn(self.t('upgrade_execute'), [label for label, _ in choose.call_args.args[1]])
        execute.assert_not_called()

    def test_failed_recalculation_does_not_reuse_previous_review(self):
        menu = CursesMenu(FakeScreen(), self.t)
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', side_effect=[self.build(), None]), patch.object(
            menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()),
        ), patch.object(menu, '_choose', side_effect=[1, 1, None]) as choose, patch(
            'netbox_ops.upgrade_ui.execute_plan',
        ) as execute:
            menu._upgrade('netbox')
        self.assertNotIn(self.t('upgrade_execute'), [label for label, _ in choose.call_args.args[1]])
        execute.assert_not_called()

    def test_escape_from_full_review_never_requests_token_or_url(self):
        menu = CursesMenu(FakeScreen(), self.t)
        with patch('netbox_ops.tui.load_config', return_value=self.cfg), patch(
            'netbox_ops.upgrade_ui.discover_context', return_value=self.context,
        ), patch('netbox_ops.upgrade_ui.make_plan', return_value=self.build()), patch.object(
            menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()),
        ), patch.object(menu, '_choose', side_effect=[1, 3, None]), patch.object(
            menu, '_show_lines', return_value=False,
        ) as show, patch.object(menu, '_upgrade_input') as prompt, patch.object(
            menu, '_confirm_token',
        ) as confirm, patch('netbox_ops.upgrade_ui.execute_plan') as execute:
            menu._upgrade('netbox')
        self.assertIn('manual recovery', str(show.call_args_list))
        prompt.assert_not_called()
        confirm.assert_not_called()
        execute.assert_not_called()

    def test_minimum_terminal_overview_remains_visible_while_actions_scroll(self):
        for language in LANGUAGES:
            t = Translator(language)
            screen = FakeScreen([curses.KEY_DOWN, curses.KEY_END, 10], dimensions=(16, 50))
            menu = CursesMenu(screen, t)
            overview = netbox_overview(self.context, '4.4.2', {}, self.build(), t, operation='netbox')
            entries = [(f'Action {i}', '') for i in range(12)]
            self.assertEqual(menu._choose('Upgrade', entries, t('upgrade_overview_hint'),
                                          overview=overview), 11)
            for frame in screen.frames:
                self.assertTrue(any('4.4.0' in line and '4.4.2' in line for _, _, line, _ in frame))
                self.assertTrue(any(t('upgrade_plan_status', status='').strip() in line
                                    for _, _, line, _ in frame))
            self.assertTrue(any('Action 11' in line and attr == curses.A_REVERSE
                                for _, _, line, attr in screen.frames[-1]))

    def test_pin_review_cancel_does_not_write(self):
        menu = CursesMenu(FakeScreen(), self.t)
        inventory = replace(self.inventory, proposals=(
            PinProposal('example-plugin', '1.0.0', 1, 'example-plugin\\n', 'example-plugin==1.0.0\\n'),
        ))
        with patch.object(menu, '_choose', side_effect=[0, 2]), patch.object(
            menu, '_show_lines',
        ) as show, patch.object(menu, '_confirm_token', return_value=False), patch(
            'netbox_ops.plugin_pin_ui.apply_pin_review',
        ) as apply:
            self.assertFalse(menu._review_plugin_pins('Pins', inventory))
        apply.assert_not_called()
        self.assertIn('example-plugin==1.0.0', str(show.call_args_list))

    def test_pin_review_applies_only_selected_proposal(self):
        menu = CursesMenu(FakeScreen(), self.t)
        inventory = replace(self.inventory, proposals=(
            PinProposal('example-plugin', '1.0.0', 1, 'example-plugin\\n', 'example-plugin==1.0.0\\n'),
            PinProposal('other-plugin', '2.0.0', None, '', 'other-plugin==2.0.0\\n'),
        ))
        with patch.object(menu, '_choose', side_effect=[4, 2]), patch.object(
            menu, '_show_lines',
        ), patch.object(menu, '_confirm_token', return_value=True), patch(
            'netbox_ops.plugin_pin_ui.apply_pin_review',
        ) as apply:
            self.assertTrue(menu._review_plugin_pins('Pins', inventory))
        review, fingerprint = apply.call_args.args
        self.assertEqual(review.selected, ('other-plugin',))
        self.assertEqual(fingerprint, review.fingerprint)

    def test_authorization_input_is_not_echoed(self):
        screen = FakeScreen([*(ord(c) for c in 'secret'), 10])
        menu = CursesMenu(screen, self.t)
        self.assertEqual(menu._upgrade_input('Upgrade', 'Auth', secret=True), 'secret')
        self.assertFalse(any('secret' in entry[2] for entry in screen.writes))


if __name__ == '__main__':
    unittest.main()
