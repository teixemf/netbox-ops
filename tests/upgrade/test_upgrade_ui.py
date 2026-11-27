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
from netbox_ops.plugin_compatibility import PluginCompatibility, PluginReview
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

    def test_plugin_plan_fingerprint_matches_locked_rebuild_without_netbox_tags(self):
        selection = {'example-plugin': '2.0.0'}
        context = self.context
        with patch('netbox_ops.upgrade_ui.build_upgrade_plan',
                   side_effect=lambda *args, **kwargs: build_upgrade_plan(
                       *args, **kwargs, reviewer=self.reviewer)):
            reviewed = make_plan(context, 'plugins', None, selection)
            fresh = replace(context, tags=())
            with (patch('netbox_ops.upgrade_ui.discover_context', return_value=fresh) as discover,
                  patch('netbox_ops.upgrade_ui.execute_plugin_upgrade') as executor):
                execute_plan(self.cfg, 'plugins', reviewed, fingerprint=reviewed.fingerprint,
                             acknowledge_unknowns=True, health_url='https://host/api/status/')
                rebuilt = executor.call_args.kwargs['rebuild_plan']()
                discover.assert_called_once_with(self.cfg, releases=False)
            self.assertTrue(reviewed.can_execute)
            self.assertEqual(reviewed.fingerprint, rebuilt.fingerprint)
            self.assertEqual(reviewed.source.available_tags, ())
            self.assertEqual(reviewed.selected_plugins, tuple(selection.items()))
            changed = replace(fresh, inventory=replace(fresh.inventory, requirements_hash='changed'))
            self.assertNotEqual(reviewed.fingerprint,
                                make_plan(changed, 'plugins', None, selection).fingerprint)
            # Real NetBox upgrades still bind verified release tags.
            for operation, pins in (('netbox', {}), ('stack', selection)):
                first = make_plan(context, operation, '4.4.2', pins)
                later = make_plan(replace(context, tags=(*context.tags, 'v4.4.3')),
                                  operation, '4.4.2', pins)
                self.assertNotEqual(first.fingerprint, later.fingerprint)

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
    def _target_model(self):
        from netbox_ops.upgrade_targets import TargetModel

        file = {'packagetype': 'bdist_wheel', 'requires_python': '>=3.12'}
        def fetch(name, version=None):
            self.assertEqual(name, 'example-plugin')
            if version is None:
                return {'releases': {v: [file] for v in ('1.0.0', '2.0.0')}}
            return {'info': {'name': name, 'version': version, 'requires_python': '>=3.12',
                             'requires_dist': ['netbox>=4.4,<4.5']}, 'urls': [file]}
        return TargetModel(self.context, fetcher=fetch, docs_fetcher=lambda *_: None)

    def _dashboard(self, keys=(), dimensions=(24, 100), language='en'):
        from netbox_ops.upgrade_dashboard import UpgradeDashboard

        menu = CursesMenu(FakeScreen(keys, dimensions=dimensions), Translator(language))
        dashboard = UpgradeDashboard(menu)
        dashboard.cfg = self.cfg
        dashboard.model = self._target_model()
        return dashboard, menu

    def _run_dashboard(self, dashboard, menu):
        from concurrent.futures import Future

        def submit(fn, *args, **kwargs):
            future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

        pool = Mock()
        pool.submit.side_effect = submit
        model = dashboard.model
        dashboard.model = None
        with (patch('netbox_ops.upgrade_dashboard.load_config', return_value=self.cfg),
              patch('netbox_ops.upgrade_dashboard.discover_target_context', return_value=self.context),
              patch('netbox_ops.upgrade_dashboard.TargetModel', return_value=model),
              patch('netbox_ops.upgrade_dashboard.ThreadPoolExecutor', return_value=pool),
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()))):
            dashboard.run()
        pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_dashboard_initial_targets_and_compact_table_in_both_languages(self):
        for language in LANGUAGES:
            for dimensions in ((16, 50), (34, 140)):
                with self.subTest(language=language, dimensions=dimensions):
                    dashboard, menu = self._dashboard([27], dimensions, language)
                    self._run_dashboard(dashboard, menu)
                    self.assertEqual(dashboard.model.selection.netbox, '4.4.2')
                    self.assertEqual(dict(dashboard.model.selection.plugins), {'example-plugin': '2.0.0'})
                    self.assertIsNone(dashboard.plan)
                    self.assertFalse(dashboard._enabled('execute'))
                    screen = menu.stdscr
                    frame = screen.frames[-1]
                    header = ' '.join(text for y, _, text, _ in frame if y == 4)
                    self.assertIn(menu.t('ut_group_core'), header)
                    self.assertNotIn(menu.t('ut_col_component'), header)
                    for key in ('current', 'target', 'status'):
                        self.assertIn(menu.t('ut_col_' + key), header)
                    self.assertIn('*4.4.2', ' '.join(text for _, _, text, _ in frame))
                    self.assertNotIn('django', ' '.join(text for _, _, text, _ in frame))
                    self.assertTrue(any(attr == curses.A_REVERSE for _, _, _, attr in frame))
                    self.assertEqual(screen.wait_ms, -1)

    def test_grouped_dashboard_uses_content_width_and_keeps_group_navigation(self):
        from netbox_ops.upgrade_targets import TargetAssessment, TargetRow

        for language in LANGUAGES:
            dashboard, menu = self._dashboard(dimensions=(45, 140), language=language)
            rows = (TargetRow(None, '4.4.0', '4.4.2', TargetAssessment('supported', 'verified')),
                    TargetRow('example-plugin', '1.0.0', '2.0.0',
                              TargetAssessment('warning', 'verified')))
            with patch.object(dashboard, '_rows', return_value=rows):
                dashboard._draw()
                frame = menu.stdscr.frames[-1]
                heading = next(y for y, _, text, _ in frame if text == menu.t('ut_group_plugins'))
                netbox = next(y for y, _, text, _ in frame if text == '  NetBox')
                plugin = next(y for y, _, text, _ in frame if text == '  example-plugin')
                self.assertEqual((heading, plugin), (netbox + 2, netbox + 3))
                counts = next(y for y, _, text, _ in frame if menu.t('ut_counts', count=2, changed=2) in text)
                self.assertEqual(counts, plugin + 1)
                self.assertTrue(any(y == netbox and attr == curses.A_REVERSE and len(text) < 100
                                    for y, _, text, attr in frame))
                self.assertLess(next(y for y, _, text, _ in frame if text == menu.t('ut_operations')), 15)
                menu.stdscr.dimensions = (16, 50)
                dashboard.cursor = 1
                dashboard._draw()
                frame = menu.stdscr.frames[-1]
                self.assertTrue(any(y == 4 and text == menu.t('ut_group_plugins') for y, _, text, _ in frame))
                self.assertTrue(any(text.strip().startswith('example') for _, _, text, _ in frame))
                self.assertEqual(dashboard.cursor, 1)
                self.assertTrue(any(menu.t('ut_operations') in text for _, _, text, _ in frame))

    def test_dashboard_scrolls_many_plugins_without_selecting_group_labels(self):
        from netbox_ops.upgrade_targets import TargetAssessment, TargetRow

        dashboard, menu = self._dashboard(dimensions=(24, 100))
        rows = tuple(TargetRow(None if i == 0 else f'plugin-{i:02}', '1.0.0', '2.0.0',
                               TargetAssessment('unchecked', 'unknown')) for i in range(25))
        with patch.object(dashboard, '_rows', return_value=rows):
            dashboard.cursor = 24
            dashboard._draw()
            frame = menu.stdscr.frames[-1]
            self.assertTrue(any('plugin-24' in text for _, _, text, _ in frame))
            self.assertTrue(any(y == 4 and text == menu.t('ut_group_plugins') for y, _, text, _ in frame))
            dashboard.cursor = 0
            dashboard._draw()
            self.assertEqual(dashboard.scroll, 0)
            self.assertTrue(any(text == '  NetBox' for _, _, text, _ in menu.stdscr.frames[-1]))
            with patch.object(dashboard, '_rows', return_value=rows[:1]):
                dashboard._draw()
                self.assertFalse(any(text == menu.t('ut_group_plugins')
                                     for _, _, text, _ in menu.stdscr.frames[-1]))

    def test_initial_discovery_panel_keeps_unknown_versions_and_disabled_actions(self):
        from concurrent.futures import Future

        for language in LANGUAGES:
            for dimensions in ((16, 50), (34, 140)):
                with self.subTest(language=language, dimensions=dimensions):
                    dashboard, menu = self._dashboard(dimensions=dimensions, language=language)
                    dashboard.model = None
                    pool = Mock()
                    pool.submit.return_value = Future()
                    dashboard._discover(pool, 'suggest')
                    self.assertTrue(dashboard._draw())
                    frame = menu.stdscr.frames[-1]
                    self.assertTrue(any(text == menu.t('ut_discovery_heading')
                                        for _, _, text, _ in frame))
                    self.assertNotIn('0/0', ' '.join(text for _, _, text, _ in frame))
                    self.assertEqual(dashboard._rows()[0].current, '?')
                    self.assertEqual(dashboard._rows()[0].target, '?')
                    self.assertFalse(any(dashboard._enabled(action)
                                         for action in ('suggest', 'reset', 'validate', 'refresh', 'execute')))
                    dashboard._discovery_event('inventory', 'done', self.context)
                    dashboard._discovery_event('releases', 'running', None)
                    dashboard._poll(pool)
                    self.assertEqual(dashboard._rows()[0].current, '4.4.0')
                    self.assertEqual(dashboard._rows()[0].target, '?')
                    self.assertEqual(dashboard._rows()[1].component, 'example-plugin')
                    self.assertEqual(dashboard._discovery_states['inventory'], 'done')
                    self.assertEqual(dashboard._discovery_states['releases'], 'running')

    def test_failed_inventory_refresh_disables_stale_targets_and_allows_retry(self):
        from concurrent.futures import Future

        dashboard, menu = self._dashboard(dimensions=(16, 50))
        dashboard.plan = self.build()
        dashboard.plan_selection = dashboard.model.selection
        old = dashboard.model
        pool = Mock()
        future = Future()
        pool.submit.return_value = future
        dashboard._discover(pool)
        future.set_exception(UpgradeExecutionError('inventory unavailable'))
        dashboard._poll(pool)
        self.assertIs(dashboard.model, old)
        self.assertIsNone(dashboard.plan)
        self.assertFalse(dashboard._inventory_valid)
        self.assertEqual(dashboard._rows()[0].current, '?')
        self.assertEqual(dashboard._discovery_states['inventory'], 'failed')
        self.assertTrue(dashboard._enabled('refresh'))
        self.assertFalse(dashboard._enabled('validate'))
        self.assertFalse(dashboard._enabled('execute'))
        self.assertTrue(dashboard._draw())
        self.assertTrue(any('Failed' in text for _, _, text, _ in menu.stdscr.frames[-1]))

    def test_completed_discovery_hides_panel_but_partial_discovery_does_not(self):
        from concurrent.futures import Future
        from netbox_ops.upgrade_targets import TargetSnapshot

        dashboard, menu = self._dashboard()
        for partial in (False, True):
            request = dashboard.model.request('assess')
            result = TargetSnapshot(request, request.selection, dashboard._rows(), 'supported', (), partial)
            dashboard._discovery_states = {'inventory': 'done', 'releases': 'done',
                                           'suggestions': 'not_applicable', 'assessment': 'done'}
            dashboard.future = Future()
            dashboard.future.set_result(result)
            dashboard._poll()
            self.assertEqual(bool(dashboard._discovery_states), partial)
            if partial:
                self.assertEqual(dashboard._discovery_states['assessment'], 'partial')
            self.assertTrue(dashboard._draw())
            self.assertEqual(any(text == menu.t('ut_discovery_heading')
                                 for _, _, text, _ in menu.stdscr.frames[-1]), partial)
            self.assertFalse(dashboard._enabled('execute'))

    def test_picker_scrolls_continuous_catalogue_and_keeps_status_colors(self):
        from concurrent.futures import Future
        from netbox_ops.upgrade_targets import TargetModel

        for language in LANGUAGES:
            for dimensions in ((16, 50), (34, 140)):
                with self.subTest(language=language, dimensions=dimensions):
                    dashboard, menu = self._dashboard(
                        [curses.KEY_END, curses.KEY_HOME, curses.KEY_NPAGE, 27],
                        dimensions, language)
                    model = dashboard.model
                    context = replace(self.context, tags=tuple(f'v4.4.{n}' for n in range(32)))
                    dashboard.model = TargetModel(context, fetcher=model._fetcher,
                                                  docs_fetcher=model._docs_fetcher)
                    menu.colors.update(selected=101, warning=102, failure=103, muted=104, title=105)

                    def submit(fn, *args, **kwargs):
                        future = Future()
                        future.set_result(fn(*args, **kwargs))
                        return future

                    pool = Mock()
                    pool.submit.side_effect = submit
                    self.assertIsNone(dashboard._picker(None, pool))
                    frames = menu.stdscr.frames
                    self.assertTrue(any('32/32' in text for frame in frames for _, _, text, _ in frame))
                    self.assertTrue(any('4.4.31' in text for frame in frames for _, _, text, _ in frame))
                    self.assertTrue(any('4.4.1' == text.strip() for frame in frames for _, _, text, _ in frame))
                    self.assertTrue(any(menu.t('ut_keep_installed_short') in text
                                        or menu.t('ut_keep_installed') in text
                                        for frame in frames for _, _, text, _ in frame))
                    self.assertTrue(any(y == dimensions[0] - 1 and text == 'F5' and attr == 105
                                        for frame in frames for y, _, text, attr in frame))
                    self.assertTrue(all(len(text) < dimensions[1] - 10
                                        for frame in frames for _, _, text, attr in frame if attr == 101))
                    self.assertGreaterEqual(pool.submit.call_count, 3)

    def test_picker_notes_prioritize_real_blocking_reason(self):
        from netbox_ops.plugin_compatibility import CompatibilityEvidence
        from netbox_ops.upgrade_targets import TargetAssessment, TargetRow

        dashboard, _ = self._dashboard()
        assessment = TargetAssessment('conflict', 'verified', (
            CompatibilityEvidence('PyPI', 'metadata missing', 'unknown'),
            CompatibilityEvidence('Python', 'requires >=3.14', 'blocks'),
        ))
        row = TargetRow(None, '4.4.0', '4.4.2', assessment)
        notes = dashboard._picker_notes(row, '4.4.2', None, '4.4.2')
        self.assertTrue(notes.startswith('Python: requires >=3.14'))
        self.assertIn(dashboard.t('ut_note_latest'), notes)

    def test_dashboard_picker_cancel_and_confirm_preserve_other_targets(self):
        for picker_keys, expected in (([27], '4.4.2'), ([curses.KEY_DOWN, curses.KEY_DOWN, 10], '4.4.1'), ([10], '4.4.0')):
            with self.subTest(expected=expected):
                dashboard, menu = self._dashboard([10, *picker_keys, 27], (16, 50))
                self._run_dashboard(dashboard, menu)
                self.assertEqual(dashboard.model.selection.netbox, expected)
                self.assertEqual(dict(dashboard.model.selection.plugins), {'example-plugin': '2.0.0'})
                self.assertEqual(dashboard.cursor, 0)
                self.assertIsNone(dashboard.plan)

    def test_dashboard_reset_and_noop_disable_validation_and_execution(self):
        dashboard, menu = self._dashboard([9, curses.KEY_RIGHT, 10, 27, 27])
        self._run_dashboard(dashboard, menu)
        self.assertEqual(dashboard.model.selection, dashboard.model.installed)
        self.assertFalse(dashboard._enabled('validate'))
        self.assertFalse(dashboard._enabled('execute'))
        self.assertIsNone(dashboard.plan)
        self.assertIn(menu.t('ut_no_changes'), [line for line, _ in dashboard._plan_lines()])

    def test_dashboard_stale_worker_result_does_not_replace_reset_intent(self):
        from concurrent.futures import Future

        dashboard, _ = self._dashboard()
        result = dashboard.model.evaluate(dashboard.model.request('suggest'))
        dashboard.model.reset()
        dashboard.future = Future()
        dashboard.future.set_result(result)
        dashboard._poll()
        self.assertEqual(dashboard.model.selection, dashboard.model.installed)
        self.assertIsNone(dashboard.snapshot)
        self.assertEqual(dashboard._discovery_states['assessment'], 'not_run')
        self.assertTrue(dashboard.error)
        self.assertFalse(dashboard._enabled('execute'))

    def test_dashboard_validates_exact_modes_and_disables_stale_or_blocked_plan(self):
        from netbox_ops.upgrade_targets import TargetSelection

        for operation, target, pins in (
            ('netbox', '4.4.2', {}), ('plugins', None, {'example-plugin': '2.0.0'}),
            ('stack', '4.4.2', {'example-plugin': '2.0.0'}),
        ):
            with self.subTest(operation=operation):
                dashboard, menu = self._dashboard()
                dashboard.model.selection = TargetSelection(
                    target or '4.4.0', (('example-plugin', pins.get('example-plugin', '1.0.0')),))
                plan = self.build(operation, target, pins)
                with (patch('netbox_ops.upgrade_dashboard.make_plan', return_value=plan) as build,
                      patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()))):
                    dashboard._validate()
                build.assert_called_once_with(self.context, operation, target, pins)
                self.assertEqual(dashboard._enabled('execute'), plan.can_execute)
                dashboard.plan = replace(plan, blockers=('Blocked',))
                self.assertFalse(dashboard._enabled('execute'))
                dashboard.plan = plan
                dashboard.model.reset()
                self.assertFalse(dashboard._enabled('execute'))

    def test_reviewed_execution_preserves_tokens_and_exact_fingerprint(self):
        plan = replace(self.build('plugins', None, {'example-plugin': '2.0.0'}),
                       requires_strong_confirmation=True)
        menu = CursesMenu(FakeScreen(), self.t)
        for confirm, expected in (([True, False], False), ([True, True], True)):
            with (self.subTest(confirm=confirm),
                patch.object(menu, '_show_lines', return_value=True),
                patch.object(menu, '_upgrade_input', side_effect=['https://netbox.example/api/status/', 'token']),
                patch.object(menu, '_confirm_token', side_effect=confirm) as tokens,
                patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
                patch('netbox_ops.upgrade_ui.execute_plan', return_value=SimpleNamespace(directory='/recovery')) as execute,
            ):
                self.assertEqual(menu._execute_reviewed_upgrade('Upgrade', self.cfg, 'plugins', plan), expected)
                self.assertEqual([call.args[2] for call in tokens.call_args_list], ['UPGRADE', 'UNCERTAIN'])
                if expected:
                    self.assertEqual(execute.call_args.args, (self.cfg, 'plugins', plan))
                    self.assertEqual(execute.call_args.kwargs['fingerprint'], plan.fingerprint)
                    self.assertTrue(execute.call_args.kwargs['acknowledge_unknowns'])
                else:
                    execute.assert_not_called()

    def run_menu(self, plan, confirmations):
        menu = CursesMenu(FakeScreen(), self.t)
        with (patch('netbox_ops.upgrade_ui.execute_plan',
                    return_value=SimpleNamespace(directory=Path('/backup/pre-upgrade-test'))) as execute,
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
              patch.object(menu, '_show_lines', return_value=True),
              patch.object(menu, '_upgrade_input', side_effect=['https://host/api/status/', '']),
              patch.object(menu, '_confirm_token', side_effect=confirmations) as confirm):
            menu._execute_reviewed_upgrade('Upgrade', self.cfg, 'netbox', plan)
        return execute, confirm

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
        execute, confirm = self.run_menu(self.build(), [False])
        confirm.assert_called_once()
        execute.assert_not_called()

    def test_uncertain_plan_needs_second_confirmation(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        execute, confirm = self.run_menu(plan, [True, False])
        self.assertEqual([call.args[2] for call in confirm.call_args_list], ['UPGRADE', 'UNCERTAIN'])
        execute.assert_not_called()

    def test_approved_plan_executes_once(self):
        plan = replace(self.build(), requires_strong_confirmation=True)
        execute, confirm = self.run_menu(plan, [True, True])
        self.assertEqual(confirm.call_count, 2)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs['fingerprint'], plan.fingerprint)

    def test_target_edit_invalidates_old_plan(self):
        from netbox_ops.upgrade_targets import TargetSelection

        dashboard, menu = self._dashboard()
        first = self.build()
        second = self.build(target='4.4.1')
        dashboard.model.selection = TargetSelection('4.4.2', dashboard.model.installed.plugins)
        with (patch('netbox_ops.upgrade_dashboard.make_plan', side_effect=[first, second]) as build,
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
              patch('netbox_ops.upgrade_ui.execute_plan') as execute):
            dashboard._validate()
            self.assertTrue(dashboard._enabled('execute'))
            dashboard.model.select(None, '4.4.1')
            self.assertFalse(dashboard._enabled('execute'))
            dashboard._validate()
            self.assertTrue(dashboard._enabled('execute'))
        self.assertEqual(build.call_count, 2)
        self.assertEqual(build.call_args.args[2], '4.4.1')
        execute.assert_not_called()

    def test_alternative_selection_rebuilds_plan(self):
        dashboard, menu = self._dashboard([curses.KEY_DOWN, 10])
        dashboard.model.reset()
        plan = self.build('plugins', None, {'example-plugin': '2.0.0'})
        with (patch('netbox_ops.upgrade_dashboard.make_plan', return_value=plan) as build,
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock()))):
            from concurrent.futures import Future

            def submit(fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

            pool = Mock()
            pool.submit.side_effect = submit
            target = dashboard._picker('example-plugin', pool)
            self.assertEqual(target, '2.0.0')
            dashboard.model.select('example-plugin', target)
            dashboard._validate()
        build.assert_called_once_with(self.context, 'plugins', None, {'example-plugin': '2.0.0'})

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
        dashboard, menu = self._dashboard()
        plan = replace(self.build(), blockers=('incompatible dependency',))
        with (patch('netbox_ops.upgrade_dashboard.make_plan', return_value=plan),
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
              patch.object(menu, '_show_lines') as show,
              patch('netbox_ops.upgrade_ui.execute_plan') as execute):
            dashboard._validate()
            self.assertFalse(dashboard._enabled('execute'))
            self.assertIn(self.t('upgrade_status_blocked'), str(dashboard._plan_lines()))
            dashboard._details(dashboard._rows()[0])
        self.assertIn('incompatible dependency', str(show.call_args_list))
        execute.assert_not_called()

    def test_refresh_invalidates_review_and_unavailable_target(self):
        dashboard, menu = self._dashboard()
        fresh = replace(self.context, tags=('v4.4.1',))
        with (patch('netbox_ops.upgrade_dashboard.load_config', return_value=self.cfg),
              patch('netbox_ops.upgrade_dashboard.discover_target_context', return_value=fresh) as discover,
              patch('netbox_ops.upgrade_dashboard.make_plan', return_value=self.build()),
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
              patch('netbox_ops.upgrade_ui.execute_plan') as execute):
            dashboard._validate()
            self.assertTrue(dashboard._enabled('execute'))
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                dashboard._discover(pool)
                self.assertFalse(dashboard._enabled('execute'))
                dashboard.future.result()
                dashboard._poll(pool)
                if dashboard.future is not None:
                    dashboard.future.result()
                    dashboard._poll(pool)
            result = dashboard.snapshot
        discover.assert_called_once()
        self.assertEqual(discover.call_args.args, (self.cfg,))
        self.assertEqual(dashboard.model.selection.netbox, '4.4.2')
        self.assertEqual(result.rows[0].assessment.availability, 'unavailable')
        self.assertIsNone(dashboard.plan)
        self.assertFalse(dashboard._enabled('execute'))
        execute.assert_not_called()

    def test_contextual_pin_review_filters_manual_blockers_and_invalidates_plan(self):
        from netbox_ops.plugin_compatibility import DependencyPreflight
        from netbox_ops.upgrade_plan import LocalRequirementDiagnostic, StageDependencyReview

        dashboard, menu = self._dashboard()
        plan = self.build()
        items = (LocalRequirementDiagnostic('example-plugin', 'example-plugin', '1.0.0',
                                            '4.4.2', 'unpinned', 'review-pin'),
                 LocalRequirementDiagnostic('django', 'django==5.2.6', '5.2.6',
                                            '4.4.2', 'overlap', 'manual-review'))
        report = StageDependencyReview('4.4.2', 'b' * 40, 'hash', (),
                                       DependencyPreflight('not-run', 'Blocked'), (), 'not-required',
                                       local_diagnostics=items)
        dashboard.plan = replace(plan, blockers=('Review requirements',), dependency_reviews=(report,))
        dashboard.plan_selection = dashboard.model.selection
        self.assertEqual(dashboard._correctable_pins(), {'example-plugin'})
        self.assertIn('pins', dashboard._actions())
        self.assertTrue(dashboard._enabled('pins'))
        inventory = replace(self.inventory, local_pin_names=('example-plugin',))
        pool = Mock()
        with (patch('netbox_ops.upgrade_dashboard.inventory_plugins', return_value=inventory) as read,
              patch.object(menu, '_review_plugin_pins', return_value=False) as review,
              patch.object(dashboard, '_discover') as discover):
            dashboard._review_plan_pins(pool)
            review.assert_called_once_with(dashboard.title, inventory, eligible={'example-plugin'})
            read.assert_called_once_with(self.installation, local_pin_names=('example-plugin',))
            discover.assert_not_called()
            self.assertIsNotNone(dashboard.plan)
            review.return_value = True
            dashboard._review_plan_pins(pool)
            discover.assert_called_once_with(pool)
            self.assertTrue(dashboard._revalidate_after_pins)
        self.assertFalse(dashboard._enabled('execute'))

    def test_plan_review_prioritizes_blockers_and_keeps_evidence_separate(self):
        for language in LANGUAGES:
            for dimensions in ((16, 50), (34, 140)):
                dashboard, menu = self._dashboard(dimensions=dimensions, language=language)
                dashboard.plan = replace(self.build(), blockers=('Incompatible plugin',),
                                         warnings=('Advisory range',))
                dashboard.plan_selection = dashboard.model.selection
                self.assertTrue(dashboard._plan_matches())
                lines = dashboard._plan_lines()
                self.assertEqual(lines[0][1], 'failure')
                self.assertLess(lines.index(('Incompatible plugin', 'failure')),
                                lines.index(('Advisory range', 'warning')))
                self.assertFalse(dashboard._enabled('execute'))
                self.assertTrue(dashboard._draw())
                with patch.object(menu, '_show_lines', return_value=False) as show:
                    dashboard._details(dashboard._rows()[0])
                    overview = show.call_args.args[1]
                    self.assertIn(('Incompatible plugin', 'failure'), overview)
                    self.assertNotIn(dashboard.plan.fingerprint, str(overview))
                    dashboard._evidence(dashboard._rows()[0])
                    self.assertIn(dashboard.plan.fingerprint, str(show.call_args.args[1]))
                dashboard.model.reset()
                self.assertFalse(dashboard._enabled('execute'))
                self.assertNotIn(('Incompatible plugin', 'failure'), dashboard._plan_lines())

    def test_adaptive_dashboard_reserves_operations_readiness_and_primary_blocker(self):
        from netbox_ops.upgrade_targets import TargetAssessment, TargetRow

        for language in LANGUAGES:
            dashboard, menu = self._dashboard(language=language)
            dashboard.plan = replace(self.build(), blockers=('Incompatible plugin: exact runtime gate',))
            dashboard.plan_selection = dashboard.model.selection
            rows = tuple(TargetRow(None if i == 0 else f'plugin-{i:02}-very-long-name',
                                   '1.0.0', '2.0.0', TargetAssessment('warning', 'verified'))
                         for i in range(25))
            with patch.object(dashboard, '_rows', return_value=rows):
                dashboard.cursor = 24
                for dimensions in ((45, 160), (30, 120), (24, 80), (16, 50), (45, 160)):
                    menu.stdscr.dimensions = dimensions
                    self.assertTrue(dashboard._draw())
                    frame = menu.stdscr.frames[-1]
                    ops = next(y for y, _, text, _ in frame if text == menu.t('ut_operations'))
                    readiness = next(y for y, _, text, _ in frame if menu.t('upgrade_status_blocked') in text)
                    self.assertLess(ops, readiness)
                    self.assertLess(readiness, dimensions[0] - 2)
                    self.assertTrue(any('Incompatible plugin:' in text for _, _, text, _ in frame))
                    self.assertTrue(any('plugin-24' in text for _, _, text, _ in frame))
                    self.assertEqual(dashboard.cursor, 24)
                    self.assertFalse(dashboard._enabled('execute'))

    def test_real_curses_dashboard_capture(self):
        import errno
        import json
        import os
        import pty
        import select
        import subprocess
        import sys
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            for language, monochrome in (('en', False), ('pt', True)):
                with self.subTest(language=language, monochrome=monochrome):
                    output = Path(tmp) / f'{language}.json'
                    master, slave = pty.openpty()
                    env = dict(os.environ, TERM='xterm-256color', LINES='45', COLUMNS='160')
                    env.pop('NO_COLOR', None)
                    if monochrome:
                        env['NO_COLOR'] = '1'
                    proc = None
                    terminal_output = bytearray()
                    try:
                        proc = subprocess.Popen(
                            [sys.executable, '-m', 'tests.upgrade.terminal_dashboard_capture',
                             str(output), language], stdin=slave, stdout=slave, stderr=slave, env=env)
                        os.close(slave)
                        slave = None
                        deadline = time.monotonic() + 20
                        while proc.poll() is None:
                            self.assertLess(time.monotonic(), deadline, 'curses capture timed out')
                            if select.select([master], [], [], 0.1)[0]:
                                try:
                                    terminal_output.extend(os.read(master, 65536))
                                except OSError as exc:
                                    if exc.errno != errno.EIO:
                                        raise
                        self.assertEqual(proc.returncode, 0, terminal_output.decode(errors='replace'))
                    finally:
                        if proc is not None and proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        if slave is not None:
                            os.close(slave)
                        os.close(master)
                    frames = json.loads(output.read_text())
                    t = Translator(language)
                    self.assertEqual([(f['height'], f['width']) for f in frames],
                                     [(45, 160), (30, 120), (24, 80), (16, 50), (45, 160)])
                    for frame in frames:
                        self.assertEqual(frame['color'], not monochrome)
                        lines = frame['lines']
                        operations = next(i for i, line in enumerate(lines) if t('ut_operations') in line)
                        readiness = next(i for i, line in enumerate(lines)
                                         if t('upgrade_status_blocked') in line)
                        self.assertLess(operations, readiness)
                        self.assertLess(readiness, frame['height'] - 2)
                        self.assertTrue(any('Incompatible plugin:' in line for line in lines))
                        self.assertTrue(any('plugin-24' in line for line in lines))
                        if frame['width'] in (50, 80, 120, 160):
                            print(f"Actual curses capture: {language} {frame['width']}x{frame['height']}"
                                  f" color={frame['color']}\n" + '\n'.join(lines))

    def test_dashboard_validation_stays_on_screen_and_rejects_stale_result(self):
        from concurrent.futures import Future

        dashboard, menu = self._dashboard(dimensions=(16, 50))
        pool = Mock()
        future = Future()
        pool.submit.return_value = future
        dashboard._validate(pool)
        self.assertIs(dashboard.future, future)
        self.assertFalse(dashboard._enabled('execute'))
        self.assertTrue(dashboard._draw())
        self.assertTrue(any(menu.t('ut_validating') in text for _, _, text, _ in menu.stdscr.frames[-1]))
        dashboard.model.reset()
        future.set_result(self.build())
        dashboard._poll(pool)
        self.assertIsNone(dashboard.plan)
        self.assertFalse(dashboard._enabled('execute'))
        self.assertEqual(dashboard.error, menu.t('ut_plan_mismatch'))

    def test_failed_recalculation_does_not_reuse_previous_review(self):
        dashboard, menu = self._dashboard()
        with (patch('netbox_ops.upgrade_dashboard.make_plan', side_effect=[self.build(), None]),
              patch.object(menu, '_upgrade_job', side_effect=lambda title, worker: worker(Mock())),
              patch('netbox_ops.upgrade_ui.execute_plan') as execute):
            dashboard._validate()
            self.assertTrue(dashboard._enabled('execute'))
            dashboard._validate()
        self.assertIsNone(dashboard.plan)
        self.assertFalse(dashboard._enabled('execute'))
        execute.assert_not_called()

    def test_escape_from_full_review_never_requests_token_or_url(self):
        menu = CursesMenu(FakeScreen(), self.t)
        with (patch.object(menu, '_show_lines', return_value=False) as show,
              patch.object(menu, '_upgrade_input') as prompt,
              patch.object(menu, '_confirm_token') as confirm,
              patch('netbox_ops.upgrade_ui.execute_plan') as execute):
            self.assertFalse(menu._execute_reviewed_upgrade('Upgrade', self.cfg, 'netbox', self.build()))
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
