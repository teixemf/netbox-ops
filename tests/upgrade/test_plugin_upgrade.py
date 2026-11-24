"""Plugin-only upgrade tests; no package installs, migrations or service changes."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.plugin_compatibility import (
    DependencyPreflight, PluginCompatibility, PluginReview,
)
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.plugin_upgrade import (
    _apply_requirements, preflight_plugin_upgrade, prepare_plugin_upgrade,
    run_plugin_stage,
)
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
)
from netbox_ops.upgrade_plan import build_upgrade_plan


LOCAL = b'# keep comment\r\nexample-plugin[extra] == 1.0.0 # selected\r\nother-plugin==2.0.0\r\n'
CORE = b'Django==5.2.6\npsycopg[c,pool]==3.2.10\n'


class PluginUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installation = GitNetBoxInstallation(
            self.root, '4.4.1', 'a' * 40, 'v4.4.1', 'HEAD',
            'https://github.com/netbox-community/netbox.git',
            self.root / 'venv/bin/python', '3.12.3', ('local_requirements.txt',), (),
        )
        self.local = LOCAL
        self.packages = (
            InstalledPackage('example-plugin', '1.0.0', ('example_plugin',)),
            InstalledPackage('other-plugin', '2.0.0', ('other_plugin',)),
            InstalledPackage('Django', '5.2.6', ('django',)),
            InstalledPackage('psycopg', '3.2.10', ('psycopg',)),
        )
        self.inventory = PluginInventory(
            self.root, hashlib.sha256(self.local).hexdigest(), (), self.packages,
            ('example_plugin', 'other_plugin'), (), (),
        )
        self.plan = self.build()
        self.approval = ExecutionApproval(self.plan.fingerprint, self.installation.commit,
                                          str(self.root), True)

    def review(self, package, stages, python_versions, *, version, preflights):
        chosen = version or '1.5.0'
        return PluginReview(package, chosen, None, tuple(
            PluginCompatibility(package, chosen, stage, runtime, 'compatible', (), ())
            for stage, runtime in zip(stages, python_versions)
        ), ())

    def build(self, selection=None, reviewer=None):
        return build_upgrade_plan(
            self.installation, self.inventory,
            selected_plugins=selection or {'example-plugin': None},
            postgres_version='15.2', redis_version='7.2',
            reviewer=reviewer or self.review,
        )

    def prepare(self, plan=None, local=LOCAL, core=CORE):
        return prepare_plugin_upgrade(plan or self.plan, local, core)

    def test_latest_selection_retains_its_own_compatibility_evidence(self):
        checks = {item.package: item.version for item in self.plan.stages[0].plugin_checks}
        self.assertEqual(checks['example-plugin'], '1.5.0')
        self.assertEqual(checks['other-plugin'], '2.0.0')
        prepared = self.prepare()
        self.assertEqual(prepared.selected, (('example-plugin', '1.5.0'),))
        self.assertIn(b'example-plugin[extra]==1.5.0 # selected\r\n', prepared.requirements)
        self.assertTrue(prepared.requirements.endswith(b'other-plugin==2.0.0\r\n'))
        self.assertTrue(prepared.requirements.startswith(b'# keep comment\r\n'))
        self.assertIn('psycopg[c,pool]==3.2.10', prepared.core_pins)

    def test_explicit_multiple_selection(self):
        plan = self.build({'example-plugin': '1.2.0', 'other-plugin': '2.3.0'})
        prepared = self.prepare(plan)
        self.assertEqual(dict(prepared.selected), {'example-plugin': '1.2.0', 'other-plugin': '2.3.0'})
        self.assertIn(b'other-plugin==2.3.0', prepared.requirements)

    def test_selected_conflict_is_not_overwritten_by_old_compatible_version(self):
        def review(package, stages, python_versions, *, version, preflights):
            outcome = self.review(package, stages, python_versions, version=version, preflights=preflights)
            if package == 'example-plugin' and outcome.selected_version == '1.5.0':
                return replace(outcome, stages=tuple(replace(x, status='blocked') for x in outcome.stages))
            return outcome
        plan = self.build(reviewer=review)
        self.assertFalse(plan.can_execute)
        with self.assertRaises(UpgradeExecutionError):
            self.prepare(plan)

    def test_stale_requirements_never_prepare(self):
        with self.assertRaises(UpgradeExecutionError):
            self.prepare(local=LOCAL + b'# changed\n')

    def test_core_dependency_cannot_be_selected(self):
        plan = self.build({'django': '5.2.7'})
        with self.assertRaisesRegex(UpgradeExecutionError, 'core dependencies'):
            self.prepare(plan)

    def test_core_versions_must_match_installed_release(self):
        with self.assertRaises(UpgradeExecutionError):
            self.prepare(core=b'Django==5.2.7\n')

    def test_unselected_bare_or_unsafe_requirements_rejected(self):
        for raw in (b'other-plugin\n', b'other-plugin==2.0.0\n-r secret.txt\n',
                    b'other-plugin==2.0.0\nother-plugin==2.0.0\n',
                    b'other-plugin @ https://example.org/package.whl\n'):
            with self.subTest(raw=raw):
                inventory = replace(self.inventory, requirements_hash=hashlib.sha256(raw).hexdigest())
                plan = replace(self.plan, source=replace(self.plan.source, inventory=inventory))
                with self.assertRaises(UpgradeExecutionError):
                    self.prepare(plan, local=raw)

    def test_append_selected_package_without_rewriting_unselected_pin(self):
        raw = b'other-plugin==2.0.0\n'
        inventory = replace(self.inventory, requirements_hash=hashlib.sha256(raw).hexdigest())
        plan = replace(self.plan, source=replace(self.plan.source, inventory=inventory))
        prepared = self.prepare(plan, local=raw)
        self.assertEqual(prepared.requirements, raw + b'example-plugin==1.5.0\n')

    def test_plugin_only_approval_preserves_checkout(self):
        inspector = Mock(return_value=self.installation)
        validate_execution_approval(self.plan, self.approval, operation='plugins',
                                    inspector=inspector, fresh_plan=self.plan)
        with self.assertRaises(UpgradeExecutionError):
            validate_execution_approval(self.plan, self.approval, inspector=inspector,
                                        fresh_plan=self.plan)
        stale = replace(self.plan, source=replace(
            self.plan.source, inventory=replace(self.inventory, enabled=('other_plugin',)),
        ))
        with self.assertRaises(UpgradeExecutionError):
            validate_execution_approval(self.plan, self.approval, operation='plugins',
                                        inspector=inspector, fresh_plan=stale)

    def test_joint_resolution_and_unknown_confirmation(self):
        (self.root / 'requirements.txt').write_bytes(CORE)
        (self.root / 'local_requirements.txt').write_bytes(LOCAL)
        resolver = Mock(return_value=DependencyPreflight('blocked', 'conflict', True))
        with self.assertRaisesRegex(UpgradeExecutionError, 'confirmed a conflict'):
            preflight_plugin_upgrade(self.plan, self.approval, resolver=resolver)
        resolver.return_value = DependencyPreflight('unknown', 'network', False)
        with self.assertRaisesRegex(UpgradeExecutionError, 'Explicit confirmation'):
            preflight_plugin_upgrade(self.plan, replace(self.approval, approved_unknowns=False),
                                     resolver=resolver)
        prepared = preflight_plugin_upgrade(self.plan, self.approval, resolver=resolver)
        self.assertEqual(resolver.call_args.args[:2], (prepared.local_pins, prepared.core_pins))
        self.assertEqual(resolver.call_args.kwargs['interpreter'], self.installation.python)

    @unittest.skipUnless(os.geteuid() == 0, 'Root ownership required for atomic pin update')
    def test_atomic_write_and_stale_source(self):
        path = self.root / 'local_requirements.txt'
        path.write_bytes(LOCAL)
        prepared = self.prepare()
        _apply_requirements(self.root, prepared)
        self.assertEqual(path.read_bytes(), prepared.requirements)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(UpgradeExecutionError):
            _apply_requirements(self.root, prepared)


class PluginStageTests(PluginUpgradeTests):
    def setUp(self):
        super().setUp()
        self.prepared = self.prepare()
        self.systemd = Mock()
        self.systemd.status.return_value = [
            SimpleNamespace(name=name, loaded='loaded', active='inactive')
            for name in ('netbox', 'netbox-rq')
        ]
        final = replace(self.inventory, requirements_hash=hashlib.sha256(self.prepared.requirements).hexdigest(),
                        packages=(replace(self.packages[0], version='1.5.0'), *self.packages[1:]))
        self.inventory_reader = Mock(side_effect=[self.inventory, final])
        self.inspector = Mock(return_value=self.installation)
        self.runner = Mock(side_effect=self.respond)
        self.apply = self.enterContext(patch('netbox_ops.plugin_upgrade._apply_requirements'))

    def respond(self, command, **kwargs):
        return SimpleNamespace(returncode=0, stdout='{"example_plugin":"1.5.0","other_plugin":"2.0.0"}')

    def run_stage(self):
        return run_plugin_stage(
            self.installation, self.plan, self.prepared, Path('/usr/bin/python3.12'),
            systemd=self.systemd, runner=self.runner, inspector=self.inspector,
            inventory_reader=self.inventory_reader,
        )

    def test_install_migrate_and_load_without_checkout(self):
        loaded = self.run_stage()
        self.assertEqual(loaded, (('example_plugin', '1.5.0'), ('other_plugin', '2.0.0')))
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertIn('example-plugin[extra]==1.5.0', commands[0])
        self.assertIn('other-plugin==2.0.0', commands[0])
        self.assertIn('Django==5.2.6', commands[0])
        self.assertIn('--only-binary=:all:', commands[0])
        self.assertEqual(commands[1][-1], 'check')
        self.assertEqual(commands[3][-2:], ['migrate', '--no-input'])
        self.assertEqual(commands[4][-2:], ['collectstatic', '--no-input'])
        self.assertEqual(commands[5][-2:], ['migrate', '--check'])
        self.assertTrue(all(cmd[0] == str(self.installation.python) for cmd in commands))
        self.systemd.start.assert_not_called()

    def test_failed_install_never_migrates(self):
        self.runner.side_effect = None
        self.runner.return_value = SimpleNamespace(returncode=1, stdout='secret')
        with self.assertRaisesRegex(UpgradeExecutionError, 'installation failed') as caught:
            self.run_stage()
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(self.runner.call_count, 1)
        self.systemd.start.assert_not_called()

    def test_unselected_plugin_drift_blocks_migrations(self):
        final = replace(self.inventory, requirements_hash=hashlib.sha256(self.prepared.requirements).hexdigest(),
                        packages=(replace(self.packages[0], version='1.5.0'),
                                  replace(self.packages[1], version='9.0.0'), *self.packages[2:]))
        self.inventory_reader.side_effect = [self.inventory, final]
        with self.assertRaisesRegex(UpgradeExecutionError, 'differ from approved pins'):
            self.run_stage()
        self.assertEqual(self.runner.call_count, 2)

    def test_active_writer_blocks_pin_update(self):
        self.systemd.status.return_value[0].active = 'active'
        with self.assertRaises(UpgradeExecutionError):
            self.run_stage()
        self.apply.assert_not_called()
        self.runner.assert_not_called()

    def test_timeout_does_not_expose_package_output(self):
        self.runner.side_effect = subprocess.TimeoutExpired('secret', 120)
        with self.assertRaises(UpgradeExecutionError) as caught:
            self.run_stage()
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
