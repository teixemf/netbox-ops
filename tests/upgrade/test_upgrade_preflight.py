"""Offline plan dependency review and immutable approval regression tests."""
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.i18n import Translator
from netbox_ops.plugin_compatibility import DependencyPreflight, PluginCompatibility, PluginReview
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.plugin_upgrade import bind_reviewed_dependencies, prepare_plugin_upgrade
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_git import PinnedStage
from netbox_ops.upgrade_plan import UpgradePlanError, build_upgrade_plan
from netbox_ops.upgrade_preflight import review_plan_dependencies, target_requirements
from netbox_ops.upgrade_runtime import LtreeEvidence, probe_ltree
from netbox_ops.upgrade_ui import summary_lines


class DependencyReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.live = GitNetBoxInstallation(
            self.root, '4.6.1', 'a' * 40, 'v4.6.1', 'HEAD',
            'https://github.com/netbox-community/netbox.git',
            self.root / 'venv/bin/python', '3.12.3', ('local_requirements.txt',), (),
        )
        self.local = b'# keep\ndisabled-plugin==1.0\n'
        self.core = b'Django==5.2.1'
        self.installed = (InstalledPackage('django', '5.2.1', ()),
                          InstalledPackage('disabled-plugin', '1.0', ()))
        (self.root / 'requirements.txt').write_bytes(self.core)
        (self.root / 'local_requirements.txt').write_bytes(self.local)
        self.pin = Mock(return_value=(PinnedStage('4.7.3', 'b' * 40),))
        self.read = Mock(return_value=b'Django==6.1.0')
        self.resolver = Mock(return_value=DependencyPreflight(
            'passed', 'resolved', True, (('django', '6.1.0'), ('disabled-plugin', '1.0'),
                                         ('transitive', '2.0')),
        ))
        self.download = Mock()
        self.ltree = Mock(return_value=LtreeEvidence('available'))
        self.cfg = Configuration(DEFAULT_UNITS)

    def reviewer(self, package, stages, runtimes, *, version, preflights):
        chosen = version or '2.0'
        return PluginReview(package, chosen, None, tuple(
            PluginCompatibility(package, chosen, stage, runtime, 'compatible', (), ())
            for stage, runtime in zip(stages, runtimes)
        ), (), release_status='verified')

    def build(self, target='4.7.3', selected=None, *, inventory_packages=None, enabled=()):
        local = (self.root / 'local_requirements.txt').read_bytes()
        inventory = PluginInventory(
            self.root, hashlib.sha256(local).hexdigest(), (),
            self.installed if inventory_packages is None else inventory_packages, enabled, (), (),
        )
        return build_upgrade_plan(
            self.live, inventory, target_netbox=target, selected_plugins=selected,
            available_tags=('v4.7.3',), postgres_version='16.2', redis_version='7.2',
            reviewer=self.reviewer,
        )

    def review(self, plan):
        return review_plan_dependencies(
            plan, self.cfg, pin=self.pin, core_reader=self.read, resolver=self.resolver,
            download=self.download, ltree_probe=self.ltree,
        )

    def test_netbox_only_checks_disabled_local_package_and_core(self):
        plan = self.review(self.build())
        self.assertTrue(plan.can_execute)
        review = plan.dependency_reviews[0]
        self.assertEqual(review.commit, 'b' * 40)
        self.assertEqual(dict(review.exact_pins), {'django': '6.1.0', 'disabled-plugin': '1.0'})
        self.assertIn(('django', '5.2.1', '6.1.0'), review.changes)
        self.assertIn(('transitive', None, '2.0'), review.changes)
        self.assertIn('  django: 5.2.1 -> 6.1.0', summary_lines(plan, Translator('en')))
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), self.local)
        self.assertEqual((self.root / 'requirements.txt').read_bytes(), self.core)
        self.ltree.assert_called_once_with(self.cfg, self.live)
        self.assertFalse(self.download.called)

    def test_new_core_package_with_identical_local_pin_is_resolved_and_retained(self):
        self.read.return_value += b'\nDisabled_Plugin==1.0'
        plan = self.review(self.build())
        self.assertTrue(plan.can_execute)
        review = plan.dependency_reviews[0]
        self.assertEqual(review.overlaps[0].status, 'redundant')
        self.assertIn('disabled-plugin==1.0', self.resolver.call_args.args[0])
        self.assertIn('Disabled_Plugin==1.0', self.resolver.call_args.args[1])
        self.assertIn('Redundant identical pin', '\n'.join(summary_lines(plan, Translator('en'))))
        self.assertIn('Pin redundante', '\n'.join(summary_lines(plan, Translator('pt'))))
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), self.local)
        # Approval binds the overlapping evidence, not just a package version.
        pins = prepare_plugin_upgrade(plan, self.local, self.read.return_value, stage_index=0)
        with self.assertRaisesRegex(UpgradeExecutionError, 'changed after plan approval'):
            bind_reviewed_dependencies(plan, 0, 'b' * 40, self.read.return_value,
                                       replace(pins, overlaps=()))
        # A redundant pin does not waive the complete dependency resolver.
        self.resolver.return_value = DependencyPreflight('blocked', 'conflict', True)
        self.assertFalse(self.review(self.build()).can_execute)

    def test_conflict_blocks_resolver_but_reports_independent_ltree(self):
        self.read.return_value += b'\nDisabled_Plugin==2.0'
        self.ltree.return_value = LtreeEvidence('permission')
        plan = self.review(self.build())
        self.assertFalse(plan.can_execute)
        review = plan.dependency_reviews[0]
        self.assertEqual(review.overlaps[0].status, 'conflict')
        self.assertEqual(review.resolution.status, 'not-run')
        self.assertEqual(review.ltree, 'permission')
        self.resolver.assert_not_called()
        self.download.assert_not_called()
        self.ltree.assert_called_once()
        summary = '\n'.join(summary_lines(plan, Translator('en')))
        self.assertIn('local disabled-plugin==1.0; core Disabled_Plugin==2.0', summary)
        self.assertIn('not run', summary)
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), self.local)

    def test_unpinned_disabled_requirement_blocks_before_resolver(self):
        (self.root / 'local_requirements.txt').write_bytes(b'disabled-plugin\n')
        plan = self.review(self.build())
        self.assertFalse(plan.can_execute)
        self.assertIn('Pin unselected local packages', ' '.join(plan.blockers))
        self.resolver.assert_not_called()
        item, = plan.dependency_reviews[0].local_diagnostics
        self.assertEqual((item.name, item.declaration, item.installed, item.stage, item.reason, item.action),
                         ('disabled-plugin', 'disabled-plugin', '1.0', '4.7.3', 'unpinned', 'review-pin'))
        for language in ('en', 'pt'):
            summary = '\n'.join(summary_lines(plan, Translator(language)))
            self.assertIn('disabled-plugin', summary)
            self.assertIn('1.0', summary)
            self.assertIn('4.7.3', summary)

    def test_local_diagnostics_name_every_blocker_without_leaking_url(self):
        local = b'disabled-plugin==2.0\nmissing-package\nprivate @ https://user:secret@example.invalid/p.whl\n'
        (self.root / 'local_requirements.txt').write_bytes(local)
        plan = self.review(self.build())
        self.assertFalse(plan.can_execute)
        diagnostics = {item.name: item for item in plan.dependency_reviews[0].local_diagnostics}
        self.assertEqual(diagnostics['disabled-plugin'].reason, 'mismatched')
        self.assertEqual(diagnostics['missing-package'].reason, 'missing')
        self.assertEqual(diagnostics['private'].reason, 'ambiguous')
        self.assertTrue(all(item.action == 'manual-review' for item in diagnostics.values()))
        self.assertNotIn('secret', '\n'.join(summary_lines(plan, Translator('en'), details=True)))
        self.resolver.assert_not_called()
        self.download.assert_not_called()
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), local)

    def test_compatibility_block_keeps_independent_local_diagnostics_without_resolver(self):
        local = b'disabled-plugin\n'
        (self.root / 'local_requirements.txt').write_bytes(local)
        base = self.build()
        blocked = replace(base, blockers=('Plugin compatibility conflict',))
        plan = self.review(blocked)
        self.assertFalse(plan.can_execute)
        review = plan.dependency_reviews[0]
        self.assertEqual(review.resolution.status, 'not-run')
        self.assertEqual(review.local_diagnostics[0].name, 'disabled-plugin')
        self.ltree.assert_called_once()
        self.resolver.assert_not_called()
        self.download.assert_not_called()
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), local)

    def test_combined_and_plugin_only_review_exact_selection(self):
        local = self.local + b'configured-plugin==1.0\n'
        (self.root / 'local_requirements.txt').write_bytes(local)
        packages = self.installed + (InstalledPackage('configured-plugin', '1.0', ('configured_plugin',)),)
        # Same local package is redundantly pinned in both current and target core.
        (self.root / 'requirements.txt').write_bytes(self.core + b'\ndisabled-plugin==1.0')
        self.read.return_value += b'\ndisabled-plugin==1.0'
        for target, django in ((None, '5.2.1'), ('4.7.3', '6.1.0')):
            with self.subTest(target=target):
                self.resolver.return_value = DependencyPreflight(
                    'passed', 'resolved', True, (('django', django), ('disabled-plugin', '1.0'),
                                                 ('configured-plugin', '2.0')),
                )
                plan = self.review(self.build(target, {'configured-plugin': '2.0'},
                                              inventory_packages=packages, enabled=('configured_plugin',)))
                self.assertTrue(plan.can_execute)
                review = plan.dependency_reviews[0]
                self.assertIn(('configured-plugin', '1.0', '2.0'), review.changes)
                self.assertEqual(review.overlaps[0].status, 'redundant')
                self.assertEqual(dict(review.exact_pins)['disabled-plugin'], '1.0')
                self.assertIn('disabled-plugin==1.0', self.resolver.call_args.args[0])
                self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), local)
        self.assertEqual(self.pin.call_count, 1)

    def test_disabled_plugin_cannot_be_selected_for_dependency_review(self):
        packages = (self.installed[0], replace(self.installed[1], modules=('disabled_plugin',)))
        for target in (None, '4.7.3'):
            with self.subTest(target=target):
                with self.assertRaisesRegex(UpgradePlanError, 'configured plugins'):
                    self.build(target, {'disabled-plugin': '2.0'}, inventory_packages=packages)
        self.pin.assert_not_called()
        self.resolver.assert_not_called()

    def test_conflict_and_unknown_are_distinct_nonexecutable_plans(self):
        self.resolver.return_value = DependencyPreflight('blocked', 'conflict', True)
        blocked = self.review(self.build())
        self.assertFalse(blocked.can_execute)
        self.assertIn('confirmed a conflict', ' '.join(blocked.blockers))
        self.download.assert_not_called()
        self.resolver.return_value = DependencyPreflight('unknown', 'network')
        self.download.side_effect = UpgradeExecutionError('network unavailable')
        unknown = self.review(self.build())
        self.assertFalse(unknown.can_execute)
        self.assertIn('uncertain', ' '.join(unknown.warnings))
        self.assertNotIn('confirmed a conflict', ' '.join(unknown.blockers))

    def test_wheel_only_fallback_resolves_without_live_install(self):
        resolved = self.resolver.return_value
        self.resolver.side_effect = [DependencyPreflight('unknown', 'wheel unavailable'), resolved]
        plan = self.review(self.build())
        self.assertTrue(plan.can_execute)
        self.download.assert_called_once()
        self.assertIn('wheelhouse', self.resolver.call_args.kwargs)

    def test_dependency_drift_invalidates_fingerprint_and_artifacts(self):
        original = self.review(self.build())
        self.resolver.return_value = replace(self.resolver.return_value,
            packages=(('django', '6.1.0'), ('disabled-plugin', '1.0'), ('transitive', '2.1')))
        changed = self.review(self.build())
        self.assertNotEqual(original.fingerprint, changed.fingerprint)
        pins = prepare_plugin_upgrade(original, self.local, self.read.return_value, stage_index=0)
        bound = bind_reviewed_dependencies(original, 0, 'b' * 40, self.read.return_value, pins)
        self.assertIn(('transitive', '2.0'), bound.expected)
        with self.assertRaisesRegex(UpgradeExecutionError, 'changed after plan approval'):
            bind_reviewed_dependencies(original, 0, 'c' * 40, self.read.return_value, pins)

    def test_ltree_missing_or_unverified_blocks_before_approval(self):
        for status in ('missing', 'permission', 'unknown', 'unusable', 'mismatch'):
            with self.subTest(status=status):
                self.ltree.return_value = LtreeEvidence(status)
                plan = self.review(self.build())
                self.assertFalse(plan.can_execute)
                self.assertEqual(plan.dependency_reviews[0].ltree, status)

    def test_target_requirements_use_private_git_scratch(self):
        git = Mock(side_effect=['', '', 'b' * 40, 'Django==6.1.0'])
        scratch = self.root / 'scratch'
        data = target_requirements(self.pin.return_value[0], scratch, git=git)
        self.assertEqual(data, b'Django==6.1.0')
        self.assertTrue(all(call.args[0] == scratch for call in git.call_args_list))
        self.assertIn('--depth=1', git.call_args_list[1].args[1])
        self.assertEqual((self.root / 'requirements.txt').read_bytes(), self.core)


class LtreeProbeTests(unittest.TestCase):
    def test_catalog_probe_distinguishes_extension_and_privileges(self):
        root = Path('/opt/netbox')
        live = SimpleNamespace(root=root, python=root / 'venv/bin/python')
        runtime = SimpleNamespace(python=live.python, working_directory=root / 'netbox',
                                  username='nobody', environment={})
        discover = Mock(return_value=runtime)
        runner = Mock()
        for values, status in (
            ([True, True, True, False], 'installed'),
            ([True, False, True, True], 'unusable'),
            ([False, False, True, True], 'available'),
            ([False, False, True, False], 'permission'),
            ([False, False, False, True], 'missing'),
            ([False, False, 'invalid', True], 'unknown'),
        ):
            with self.subTest(status=status):
                identity = 'netbox|5432|1234'
                row = [identity, '160015', 'netbox', 'netbox', 'public',
                       'public', 'public' if values[0] else '', *values, True]
                runner.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(row))
                self.assertEqual(probe_ltree(Configuration(DEFAULT_UNITS), live, runner=runner,
                                             discover=discover, sql=Mock(return_value=identity)).status, status)
                self.assertNotEqual(runner.call_args.kwargs['user'], 0)
                self.assertEqual(runner.call_args.kwargs['cwd'], root / 'netbox')
                self.assertNotIn('CREATE EXTENSION', runner.call_args.args[0][-1])
                self.assertIn('SET TRANSACTION READ ONLY', runner.call_args.args[0][-1])
        runner.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(
            ['other|5432|1234', '160015', 'netbox', 'netbox', 'public', 'public', '',
             False, False, True, True, True]))
        self.assertEqual(probe_ltree(Configuration(DEFAULT_UNITS), live, runner=runner,
                                     discover=discover, sql=Mock(return_value=identity)).status, 'mismatch')
