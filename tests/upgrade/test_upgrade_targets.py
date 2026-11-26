"""Offline preliminary target selection; never invoke the dependency resolver."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from netbox_ops.plugin_compatibility import DependencyPreflight, assess_compatibility
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_plan import UpgradePlanError
from netbox_ops.upgrade_targets import (
    MAX_PICKER_ROWS, TargetModel, TargetSelection, discover_target_context,
)
from netbox_ops.upgrade_ui import UpgradeContext


def published(**kwargs):
    return {'packagetype': 'bdist_wheel', 'requires_python': '>=3.12',
            'yanked': False, **kwargs}


class TargetModelTests(unittest.TestCase):
    def setUp(self):
        root = Path('/opt/netbox')
        installation = GitNetBoxInstallation(
            root, '4.4.0', 'a' * 40, 'v4.4.0', 'HEAD',
            'https://github.com/netbox-community/netbox.git',
            root / 'venv/bin/python', '3.12.3', (), (),
        )
        inventory = PluginInventory(root, 'hash', (), (
            InstalledPackage('demo-plugin', '1.0.0', ('demo_plugin',)),
            InstalledPackage('django', '5.2.6', ('django',)),
        ), ('demo_plugin',), (), ())
        self.context = UpgradeContext(installation, inventory,
                                      ('v4.6.2', 'v4.5.0', 'v4.4.0'), '15.2', '7.2')
        self.versions = {
            'demo-plugin': {
                '3.0.0': ('netbox>=4.4,<4.6', published()),
                '2.0.0': ('netbox>=4.6,<4.7', published()),
                '1.0.0': ('netbox>=4.4,<4.6', published()),
                '0.9.0': ('netbox>=4.4', published()),
                '4.0.0rc1': ('netbox>=4.4', published()),
                '5.0.0': ('netbox>=4.4', published(yanked=True)),
            },
        }
        self.exact = {}
        self.fetcher = Mock(side_effect=self.fetch)
        self.docs = Mock(return_value=None)
        self.enterContext(patch('netbox_ops.plugin_compatibility.isolated_dependency_preflight',
                                side_effect=AssertionError('Navigation must not resolve dependencies')))

    def fetch(self, name, version=None):
        releases = self.versions[name]
        if version is None:
            return {'releases': {number: [file] for number, (_, file) in releases.items()}}
        if (name, version) in self.exact:
            result = self.exact[name, version]
            if isinstance(result, Exception):
                raise result
            return result
        requirement, file = releases[version]
        return {'info': {'name': name, 'version': version, 'requires_python': '>=3.12',
                         'requires_dist': [requirement] if requirement else []},
                'urls': [file]}

    def model(self, **kwargs):
        return TargetModel(self.context, fetcher=self.fetcher, docs_fetcher=self.docs, **kwargs)

    def run_request(self, model, action='assess', component=None, **kwargs):
        result = model.evaluate(model.request(action, component, **kwargs))
        self.assertTrue(model.accept(result))
        return result

    def test_cancelled_evaluation_does_not_fetch_remote_metadata(self):
        from concurrent.futures import CancelledError

        model = self.model()
        with self.assertRaises(CancelledError):
            model.evaluate(model.request('suggest'), cancelled=lambda: True)
        self.fetcher.assert_not_called()
        self.docs.assert_not_called()

    def test_initial_latest_netbox_and_highest_supported_plugin_not_downgrades(self):
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(model.selection.netbox, '4.6.2')
        self.assertEqual(dict(model.selection.plugins), {'demo-plugin': '2.0.0'})
        self.assertEqual([r.component for r in result.rows], [None, 'demo-plugin'])
        self.assertEqual([r.assessment.status for r in result.rows], ['supported', 'supported'])
        self.assertFalse(result.partial)
        self.assertEqual(model.selection.plan_arguments(model.installed),
                         ('stack', '4.6.2', {'demo-plugin': '2.0.0'}))
        self.assertEqual([call.args for call in self.fetcher.call_args_list],
                         [('demo-plugin',), ('demo-plugin', '3.0.0'), ('demo-plugin', '2.0.0')])

    def test_uncertain_newer_candidate_does_not_claim_latest_compatible(self):
        self.versions['demo-plugin']['3.0.0'] = ('', published())
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(dict(result.selection.plugins), {'demo-plugin': '2.0.0'})
        self.assertTrue(any('newer releases were not fully assessed' in note for note in result.warnings))

    def test_keep_supported_installed_version_when_newer_candidates_uncertain(self):
        self.versions['demo-plugin']['3.0.0'] = ('', published())
        self.versions['demo-plugin']['2.0.0'] = ('', published())
        self.versions['demo-plugin']['1.0.0'] = ('netbox>=4.4,<4.7', published())
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(result.selection.plugins, model.installed.plugins)
        self.assertEqual(result.rows[1].assessment.status, 'supported')

    def test_verified_uncertain_release_may_be_suggested_but_never_green(self):
        self.versions['demo-plugin']['3.0.0'] = ('', published())
        self.versions['demo-plugin']['2.0.0'] = ('netbox<4.6', published())
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(dict(result.selection.plugins), {'demo-plugin': '3.0.0'})
        self.assertEqual(result.rows[1].assessment.status, 'warning')
        self.assertEqual(result.rows[1].assessment.availability, 'verified')

    def test_no_updates_or_missing_release_still_shows_installed_version(self):
        self.context = replace(self.context, tags=('v4.4.0',))
        self.versions['demo-plugin'] = {'0.9.0': ('netbox>=4.4', published())}
        self.exact['demo-plugin', '1.0.0'] = OSError('network unavailable')
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(model.selection, model.installed)
        self.assertEqual(model.selection.plan_arguments(model.installed), (None, None, {}))
        self.assertEqual(result.rows[1].assessment.status, 'warning')
        self.assertEqual(result.rows[1].assessment.availability, 'installed')
        picker = self.run_request(model, 'catalogue', 'demo-plugin')
        self.assertEqual([row.target for row in picker.rows], ['1.0.0'])

    def test_exact_release_yanked_not_suggested_and_not_mislabeled_conflict(self):
        self.exact['demo-plugin', '2.0.0'] = {
            'info': {'name': 'demo-plugin', 'version': '2.0.0',
                     'requires_python': '>=3.12', 'requires_dist': ['netbox>=4.6']},
            'urls': [published(yanked=True)],
        }
        model = self.model()
        result = self.run_request(model, 'suggest')
        self.assertEqual(result.selection.plugins, model.installed.plugins)
        picker = self.run_request(model, 'catalogue', 'demo-plugin')
        row = next(row for row in picker.rows if row.target == '2.0.0')
        self.assertEqual(row.assessment.availability, 'unavailable')
        self.assertEqual(row.assessment.status, 'warning')
        with self.assertRaisesRegex(UpgradePlanError, 'no longer available'):
            model.select('demo-plugin', '2.0.0')

    def test_target_edits_reassess_unchanged_plugins_and_preserve_explicit_choices(self):
        inventory = replace(self.context.inventory, enabled=('demo_plugin', 'other_plugin'),
                            packages=self.context.inventory.packages + (
                                InstalledPackage('other-plugin', '1.0.0', ('other_plugin',)),))
        self.context = replace(self.context, inventory=inventory)
        self.versions['other-plugin'] = {'1.0.0': ('netbox>=4.6,<4.7', published())}
        model = self.model()
        picker = self.run_request(model, 'catalogue', 'demo-plugin')
        self.assertIn('2.0.0', [row.target for row in picker.rows])
        model.select('demo-plugin', '2.0.0')
        model.select(None, '4.5.0')
        result = self.run_request(model)
        self.assertEqual(dict(model.selection.plugins),
                         {'demo-plugin': '2.0.0', 'other-plugin': '1.0.0'})
        self.assertEqual([row.assessment.status for row in result.rows], ['conflict'] * 3)
        self.assertEqual(model.selection.plan_arguments(model.installed),
                         ('stack', '4.5.0', {'demo-plugin': '2.0.0'}))
        model.select(None, model.installed.netbox)
        self.assertEqual(model.selection.plan_arguments(model.installed),
                         ('plugins', None, {'demo-plugin': '2.0.0'}))
        model.reset()
        model.select(None, '4.5.0')
        self.assertEqual(model.selection.plan_arguments(model.installed), ('netbox', '4.5.0', {}))

    def test_pickers_assess_target_context_and_installed_prerequisites(self):
        model = self.model()
        plugin = self.run_request(model, 'catalogue', 'demo-plugin')
        self.assertEqual([r.target for r in plugin.rows], ['3.0.0', '2.0.0', '1.0.0'])
        self.assertEqual([r.assessment.status for r in plugin.rows],
                         ['conflict', 'supported', 'conflict'])
        model.select('demo-plugin', '2.0.0')
        picker = self.run_request(model, 'catalogue', None, limit=2)
        self.assertEqual(picker.total, 3)
        self.assertEqual([r.target for r in picker.rows], ['4.6.2', '4.5.0'])
        self.assertEqual([r.assessment.status for r in picker.rows], ['supported', 'conflict'])
        page = self.run_request(model, 'catalogue', None, offset=2)
        self.assertEqual([r.target for r in page.rows], ['4.4.0'])
        with self.assertRaises(ValueError):
            model.request('catalogue', limit=MAX_PICKER_ROWS + 1)
        model = TargetModel(replace(self.context, postgres='12.0'),
                            fetcher=self.fetcher, docs_fetcher=self.docs)
        result = self.run_request(model, 'suggest')
        self.assertEqual(result.rows[0].assessment.status, 'conflict')

    def test_stale_results_rejected_after_edit_request_or_refresh(self):
        model = self.model()
        first = model.evaluate(model.request('suggest'))
        model.select(None, '4.5.0')
        self.assertFalse(model.accept(first))
        self.assertEqual(model.selection.netbox, '4.5.0')
        second = model.evaluate(model.request())
        third = model.request('suggest')
        self.assertFalse(model.accept(second))
        third_result = model.evaluate(third)
        refreshed = model.refreshed(replace(self.context, tags=('v4.4.0',)))
        self.assertFalse(refreshed.accept(third_result))
        self.assertEqual(refreshed.selection.netbox, '4.5.0')
        fresh = self.run_request(refreshed)
        self.assertEqual(fresh.rows[0].assessment.availability, 'unavailable')
        self.assertEqual(fresh.rows[0].assessment.status, 'warning')

    def test_bounded_queries_cache_and_partial_continuation(self):
        model = self.model(max_remote_lookups=3)
        for _ in range(6):
            before = self.fetcher.call_count + 2 * self.docs.call_count
            result = self.run_request(model, 'suggest')
            after = self.fetcher.call_count + 2 * self.docs.call_count
            self.assertLessEqual(after - before, 3)
            if not result.partial:
                break
        self.assertFalse(result.partial)
        self.assertEqual(dict(result.selection.plugins), {'demo-plugin': '2.0.0'})
        calls = self.fetcher.call_count + self.docs.call_count
        self.run_request(model)
        self.assertEqual(self.fetcher.call_count + self.docs.call_count, calls)

    def test_long_completed_search_is_not_replayed_during_other_plugin_continuation(self):
        inventory = replace(self.context.inventory, enabled=('demo_plugin', 'other_plugin'),
                            packages=self.context.inventory.packages + (
                                InstalledPackage('other-plugin', '1.0.0', ('other_plugin',)),))
        self.context = replace(self.context, inventory=inventory)
        # More candidates than the exact assessment LRU can retain.
        for name, count in (('demo-plugin', 140), ('other-plugin', 16)):
            self.versions[name] = {
                f'{major}.0.0': ('netbox<4.6', published())
                for major in range(2, count + 2)
            }
            self.versions[name]['1.0.0'] = ('netbox>=4.6,<4.7', published())
        model = self.model()
        for _ in range(60):
            before = self.fetcher.call_count + 2 * self.docs.call_count
            result = self.run_request(model, 'suggest')
            self.assertLessEqual(self.fetcher.call_count + 2 * self.docs.call_count - before, 12)
            if not result.partial:
                break
        self.assertFalse(result.partial)
        self.assertEqual(result.selection.plugins, model.installed.plugins)
        first = [call.args for call in self.fetcher.call_args_list
                 if call.args == ('demo-plugin', '141.0.0')]
        self.assertEqual(len(first), 1)

    def test_failed_listing_retry_and_unknown_mapping(self):
        context = replace(self.context, inventory=replace(
            self.context.inventory, enabled=('demo_plugin', 'unmapped_plugin')))
        model = TargetModel(context, fetcher=self.fetcher, docs_fetcher=self.docs)
        self.fetcher.side_effect = OSError('private error')
        result = self.run_request(model, 'suggest')
        self.assertEqual(result.status, 'warning')
        self.assertTrue(any('not be reliably mapped' in note for note in result.warnings))
        self.assertNotIn('private error', repr(result))
        self.fetcher.side_effect = self.fetch
        retry = self.run_request(model, 'suggest')
        self.assertEqual(dict(retry.selection.plugins), {'demo-plugin': '2.0.0'})

    def test_reject_noneligible_downgrade_or_unpublished_selection(self):
        model = self.model()
        self.run_request(model, 'catalogue', 'demo-plugin')
        for component, version in ((None, '4.3.9'), ('django', '6.0'),
                                   ('demo-plugin', '0.9.0'), ('demo-plugin', '5.0.0'),
                                   ('demo-plugin', '4.0.0rc1')):
            with self.subTest(component=component, version=version), self.assertRaises(UpgradePlanError):
                model.select(component, version)
        with self.assertRaises(UpgradePlanError):
            TargetSelection('4.4.0', (('demo-plugin', '0.9.0'),)).plan_arguments(model.installed)

    def test_discovery_failure_keeps_installed_inventory(self):
        with (patch('netbox_ops.upgrade_targets.discover_context', return_value=self.context),
              patch('netbox_ops.upgrade_targets.official_tags',
                    side_effect=UpgradeExecutionError('discovery failed'))):
            context = discover_target_context(Mock())
        self.assertEqual(context.tags, ())
        self.assertEqual(context.plugins, self.context.plugins)
        model = TargetModel(context, fetcher=self.fetcher, docs_fetcher=self.docs)
        result = self.run_request(model)
        self.assertEqual(model.selection.netbox, model.installed.netbox)
        self.assertTrue(any('Official NetBox' in note for note in result.warnings))

    def test_preliminary_support_never_changes_full_compatibility_contract(self):
        meta = self.fetch('demo-plugin', '2.0.0')
        args = ('demo-plugin', '2.0.0', '4.6.2', '3.12.3', meta)
        result = assess_compatibility(*args, published_files=meta['urls'])
        self.assertEqual((result.preliminary_status, result.status), ('supported', 'uncertain'))
        full = assess_compatibility(*args, published_files=meta['urls'],
                                    preflight=DependencyPreflight('passed', 'ok', True))
        self.assertEqual(full.status, 'compatible')
        malformed = {**meta, 'info': {**meta['info'], 'requires_python': {'unexpected': 1}}}
        unknown = assess_compatibility(*args[:-1], malformed, published_files=meta['urls'])
        self.assertEqual((unknown.preliminary_status, unknown.status), ('warning', 'uncertain'))
