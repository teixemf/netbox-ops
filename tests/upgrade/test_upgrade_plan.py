"""Read-only plan tests: paths, plugin stages, edits and conservative failures."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netbox_ops.plugin_compatibility import (
    CompatibilityEvidence, DependencyPreflight, PluginCompatibility, PluginReview,
    ReleaseCatalog,
)
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_plan import (
    UpgradePlanError, build_upgrade_plan, edit_upgrade_plan, required_netbox_path,
)


ROOT = Path('/opt/netbox')
ORIGIN = 'https://github.com/netbox-community/netbox.git'


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.installation = GitNetBoxInstallation(
            ROOT, '4.5.0', 'a' * 40, 'v4.5.0', 'HEAD', ORIGIN,
            ROOT / 'venv/bin/python', '3.12.3', (), (),
        )
        self.inventory = PluginInventory(
            ROOT, 'abc', (), (InstalledPackage('sample-plugin', '1.0.0', ('sample_plugin',)),
                              InstalledPackage('unused-addon', '8.0.0', ('unused_addon',))),
            ('sample_plugin',), (), (),
        )
        self.tags = ('v4.5.0', 'v4.5.2', 'v4.6.0', 'v4.6.2', 'v4.7.3')

    def reviewer(self, package, stages, python_versions, *, version, preflights):
        self.calls.append((package, tuple(stages), tuple(python_versions), version, tuple(preflights)))
        chosen = version or '2.0.0'
        checks = tuple(PluginCompatibility(package, chosen, stage, runtime,
                                           'compatible', (CompatibilityEvidence(
                                               'fixture', 'confirmed', 'supports'),), ())
                       for stage, runtime in zip(stages, python_versions))
        return PluginReview(package, chosen, ReleaseCatalog(package, (), '2.0.0', ()),
                            checks, ())

    def build(self, **kwargs):
        options = dict(available_tags=self.tags, postgres_version='15.2', redis_version='7.0',
                       reviewer=self.reviewer)
        options.update(kwargs)
        return build_upgrade_plan(self.installation, self.inventory, **options)

    def test_direct_minor_upgrade_needs_only_target_stage(self):
        plan = self.build(target_netbox='4.6.2')
        self.assertEqual([s.netbox_version for s in plan.stages], ['4.6.2'])
        self.assertEqual(plan.stages[0].kind, 'netbox')
        self.assertEqual(plan.stages[0].plugin_versions, (('sample-plugin', '1.0.0'),))
        self.assertIn('version-4.6/', plan.release_notes[0])
        self.assertEqual(plan.status, 'needs_confirmation')

    def test_patch_only_notes_link_present(self):
        plan = self.build(target_netbox='4.5.2')
        self.assertIn('version-4.5/', plan.release_notes[0])

    def test_no_selection_is_not_executable(self):
        plan = self.build()
        self.assertEqual(plan.stages, ())
        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.status, 'blocked')

    def test_plugin_only_uses_shared_stage_without_netbox_checkout(self):
        plan = self.build(selected_plugins={'sample-plugin': None})
        self.assertEqual(len(plan.stages), 1)
        self.assertEqual(plan.stages[0].kind, 'plugins')
        self.assertEqual(plan.plugins[0].proposed_version, '2.0.0')
        self.assertEqual(plan.stages[0].netbox_version, '4.5.0')
        self.assertEqual(plan.release_notes, ())

    def test_selected_explicit_version_is_used(self):
        plan = self.build(selected_plugins={'sample_plugin': '1.8.2'})
        self.assertEqual(plan.plugins[0].proposed_version, '1.8.2')
        self.assertEqual(plan.selected_plugins, (('sample-plugin', '1.8.2'),))

    def test_only_selected_plugin_is_changed(self):
        plan = self.build(selected_plugins={'unused-addon': '9.0.0'})
        versions = {p.package: (p.installed_version, p.proposed_version) for p in plan.plugins}
        self.assertEqual(versions['unused-addon'], ('8.0.0', '9.0.0'))
        self.assertEqual(versions['sample-plugin'], ('1.0.0', '1.0.0'))

    def test_missing_target_tag_fails_before_network(self):
        with self.assertRaisesRegex(UpgradePlanError, 'verified Git'):
            self.build(target_netbox='4.6.9')
        self.assertEqual(self.calls, [])

    def test_missing_intermediate_tag_is_not_guessed(self):
        with self.assertRaisesRegex(UpgradePlanError, '3.7.x'):
            required_netbox_path('3.5.2', '4.5.2', ['v4.5.2'])

    def test_major_boundary_picks_latest_verified_patch(self):
        versions = required_netbox_path('3.5.2', '4.5.2',
                                         ['v3.7.0', 'v3.7.9', 'v3.7.8', 'v4.5.2'])
        self.assertEqual(versions, ('3.7.9', '4.5.2'))

    def test_already_final_minor_major_uses_direct_target(self):
        self.assertEqual(required_netbox_path('3.7.2', '4.5.2', ('v4.5.2',)),
                         ('4.5.2',))

    def test_unreviewed_future_major_fails_closed(self):
        with self.assertRaisesRegex(UpgradePlanError, 'No reviewed'):
            required_netbox_path('4.7.3', '5.0.0', ('v5.0.0',))

    def test_downgrade_refused_even_if_tag_present(self):
        with self.assertRaisesRegex(UpgradePlanError, 'downgrades'):
            required_netbox_path('4.5.2', '4.5.0', self.tags)

    def test_prerequisites_checked_at_each_intermediate_version(self):
        old = self.installation
        self.installation = GitNetBoxInstallation(
            old.root, '3.6.2', old.commit, old.tag, old.reference, old.origin,
            old.python, '3.10.9', old.local_paths, old.warnings,
        )
        plan = self.build(target_netbox='4.5.2',
                          available_tags=('v3.7.6', 'v4.5.2'),
                          python_by_stage={'4.5.2': '3.12.3'})
        self.assertEqual([stage.netbox_version for stage in plan.stages],
                         ['3.7.6', '4.5.2'])
        self.assertEqual([check.status for check in plan.stages[0].prerequisites[:1]], ['ok'])
        self.assertEqual([check.status for check in plan.stages[1].prerequisites[:1]], ['ok'])
        self.assertEqual(plan.stages[0].plugin_versions, (('sample-plugin', '1.0.0'),))
        self.assertEqual(plan.stages[1].plugin_versions, (('sample-plugin', '1.0.0'),))

    def test_missing_external_service_versions_need_confirmation(self):
        plan = self.build(target_netbox='4.5.2', postgres_version=None, redis_version=None)
        self.assertTrue(plan.requires_strong_confirmation)
        self.assertTrue(any('PostgreSQL' in w for w in plan.warnings))
        self.assertTrue(plan.can_execute)

    def test_known_incompatible_dependency_blocks_entire_plan(self):
        plan = self.build(target_netbox='4.7.3', postgres_version='14.1')
        self.assertFalse(plan.can_execute)
        self.assertTrue(any('PostgreSQL' in blocker for blocker in plan.blockers))

    def test_plugin_proven_conflict_blocks(self):
        def blocked(package, stages, python_versions, *, version, preflights):
            checks = tuple(PluginCompatibility(package, version or '2.0.0', stage, runtime,
                                               'blocked', (CompatibilityEvidence(
                                                   'pip', 'ResolutionImpossible', 'blocks'),), ())
                           for stage, runtime in zip(stages, python_versions))
            return PluginReview(package, version or '2.0.0', None, checks, ())
        plan = self.build(target_netbox='4.6.2', reviewer=blocked)
        self.assertEqual(plan.status, 'blocked')
        self.assertIn('sample-plugin', plan.blockers[0])

    def test_selected_plugin_old_version_evaluated_at_intermediate_stages(self):
        old = self.installation
        self.installation = GitNetBoxInstallation(
            old.root, '3.6.2', old.commit, old.tag, old.reference, old.origin,
            old.python, '3.10.9', old.local_paths, old.warnings,
        )
        plan = self.build(target_netbox='4.5.2',
                          available_tags=('v3.7.9', 'v4.5.2'),
                          python_by_stage={'4.5.2': '3.12.1'},
                          selected_plugins={'sample-plugin': '2.0.0'})
        self.assertEqual(plan.stages[0].plugin_versions, (('sample-plugin', '1.0.0'),))
        self.assertEqual(plan.stages[1].plugin_versions, (('sample-plugin', '2.0.0'),))
        self.assertIn(('sample-plugin', ('3.7.9',), ('3.10.9',), '1.0.0', (None,)), self.calls)
        self.assertIn(('sample-plugin', ('4.5.2',), ('3.12.1',), '2.0.0', (None,)), self.calls)

    def test_invalid_plugin_or_version_is_rejected(self):
        with self.assertRaises(UpgradePlanError):
            self.build(selected_plugins={'missing': '2.0.0'})
        with self.assertRaises(UpgradePlanError):
            self.build(selected_plugins={'sample-plugin': '2.0.0rc1'})
        with self.assertRaises(UpgradePlanError):
            self.build(selected_plugins={'sample-plugin': '2.0.0', 'sample_plugin': None})

    def test_edit_rechecks_compatibility_and_changes_fingerprint(self):
        first = self.build(target_netbox='4.6.2', selected_plugins={'sample-plugin': '1.5.0'})
        count_before = len(self.calls)
        second = edit_upgrade_plan(first, selected_plugins={'sample-plugin': '2.0.0'}, reviewer=self.reviewer)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.plugins[0].proposed_version, '1.5.0')
        self.assertEqual(second.plugins[0].proposed_version, '2.0.0')
        self.assertGreater(len(self.calls), count_before)

    def test_edits_must_not_reuse_old_dependency_resolver_success(self):
        passed = DependencyPreflight('passed', 'resolved', True)
        first = self.build(target_netbox='4.6.2',
                           selected_plugins={'sample-plugin': '1.5.0'},
                           preflights={('sample-plugin', '4.6.2'): passed})
        self.assertIs(first.source.preflights[0][2], passed)
        second = edit_upgrade_plan(first, selected_plugins={'sample-plugin': '2.0.0'}, reviewer=self.reviewer)
        self.assertEqual(second.source.preflights, ())
        self.assertEqual(self.calls[-2][4], (None,))

    def test_edit_target_rebuilds_stages(self):
        first = self.build(target_netbox='4.6.2')
        second = edit_upgrade_plan(first, target_netbox='4.7.3', reviewer=self.reviewer)
        self.assertEqual(second.stages[-1].netbox_version, '4.7.3')
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_read_only_build_does_not_write_to_installation(self):
        with patch('os.replace', side_effect=AssertionError('no writes allowed')), \
             patch('subprocess.run', side_effect=AssertionError('no external processes')), \
             patch('pathlib.Path.write_text', side_effect=AssertionError('no writes')):
            plan = self.build(target_netbox='4.6.2')
        self.assertEqual(plan.stages[0].netbox_version, '4.6.2')

    def test_inventory_from_other_root_refused(self):
        bad = PluginInventory(Path('/other/netbox'), self.inventory.requirements_hash,
                              self.inventory.requirements, self.inventory.packages,
                              self.inventory.enabled, self.inventory.proposals, ())
        with self.assertRaisesRegex(UpgradePlanError, 'another NetBox'):
            build_upgrade_plan(self.installation, bad, reviewer=self.reviewer)

    def test_unverified_enabled_plugins_are_not_assumed_compatible(self):
        inventory = PluginInventory(ROOT, 'abc', (), self.inventory.packages, None, (), ())
        plan = build_upgrade_plan(self.installation, inventory, available_tags=self.tags,
                                  target_netbox='4.6.2', reviewer=self.reviewer)
        self.assertTrue(plan.requires_strong_confirmation)
        self.assertTrue(any('enabled PLUGINS' in w for w in plan.warnings))
        self.assertEqual(plan.stages[0].plugin_checks, ())

    def test_no_verified_plugin_release_needs_manual_review(self):
        def unavailable(package, stages, python_versions, *, version, preflights):
            return PluginReview(package, None, None, (), ('PyPI unavailable',))
        plan = self.build(selected_plugins={'sample-plugin': None}, reviewer=unavailable)
        self.assertTrue(plan.requires_strong_confirmation)
        self.assertTrue(any('not verified' in w for w in plan.warnings))


if __name__ == '__main__':
    unittest.main()
