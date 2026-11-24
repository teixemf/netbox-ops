"""Combined upgrade tests with no network, migrations or service mutations."""
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.plugin_compatibility import DependencyPreflight, PluginCompatibility, PluginReview
from netbox_ops.plugin_inventory import InstalledPackage, PluginInventory
from netbox_ops.upgrade_combined import (
    prepare_combined_stages, recheck_combined_stage, run_combined_stage,
    probe_server_versions,
)
from netbox_ops.upgrade_discovery import GitNetBoxInstallation
from netbox_ops.upgrade_execution import (
    ExecutionApproval, UpgradeExecutionError, validate_execution_approval,
)
from netbox_ops.upgrade_git import PinnedStage
from netbox_ops.upgrade_plan import build_upgrade_plan

LOCAL = b'# preserve\nexample-plugin==1.0.0\nother-plugin==2.0.0\n'
CORE = 'Django==4.2.15\npsycopg[c,pool]==3.2.1'


class CombinedStageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.install = GitNetBoxInstallation(
            self.root, '3.6.2', 'a' * 40, 'v3.6.2', 'HEAD',
            'https://github.com/netbox-community/netbox.git',
            self.root / 'venv/bin/python', '3.10.12', ('local_requirements.txt',), (),
        )
        self.packages = (
            InstalledPackage('example-plugin', '1.0.0', ('example_plugin',)),
            InstalledPackage('other-plugin', '2.0.0', ('other_plugin',)),
        )
        self.inventory = PluginInventory(
            self.root, hashlib.sha256(LOCAL).hexdigest(), (), self.packages,
            ('example_plugin', 'other_plugin'), (), (),
        )
        (self.root / 'local_requirements.txt').write_bytes(LOCAL)
        self.plan = build_upgrade_plan(
            self.install, self.inventory, target_netbox='4.0.1',
            selected_plugins={'example-plugin': '1.5.0'},
            available_tags=('v3.7.8', 'v4.0.1'), postgres_version='15.2',
            redis_version='7.2', reviewer=self.reviewer,
        )
        self.approval = ExecutionApproval(self.plan.fingerprint, self.install.commit,
                                          str(self.root), True)
        self.stages = (PinnedStage('3.7.8', 'b' * 40), PinnedStage('4.0.1', 'c' * 40))
        self.fetch = Mock()
        self.git = Mock(return_value=CORE)
        self.resolver = Mock(return_value=DependencyPreflight('passed', 'resolved', True))
        self.probe = Mock(return_value=('15.2', '7.2'))

    def reviewer(self, package, stages, python_versions, *, version, preflights):
        return PluginReview(package, version, None, tuple(
            PluginCompatibility(package, version, stage, runtime, 'compatible', (), ())
            for stage, runtime in zip(stages, python_versions)
        ), ())

    def prepare(self):
        return prepare_combined_stages(
            Mock(), self.plan, self.approval, self.stages,
            fetch=self.fetch, git=self.git, resolver=self.resolver, probe=self.probe,
        )

    def test_required_intermediate_and_final_pins(self):
        stages = self.prepare()
        self.assertEqual(stages[0].pins.requirements, LOCAL)
        self.assertEqual(stages[0].pins.selected, ())
        self.assertIn('example-plugin==1.0.0', stages[0].pins.local_pins)
        self.assertEqual(stages[1].pins.selected, (('example-plugin', '1.5.0'),))
        self.assertIn('example-plugin==1.5.0', stages[1].pins.local_pins)
        self.assertTrue(stages[1].pins.requirements.endswith(b'other-plugin==2.0.0\n'))
        self.assertEqual(self.resolver.call_count, 2)
        self.assertIn('psycopg[c,pool]==3.2.1', self.resolver.call_args.args[1])
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), LOCAL)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.git.call_args.args[1], ['show', 'c' * 40 + ':requirements.txt'])

    def test_approval_rejects_skipping_mandatory_release(self):
        inspector = Mock(return_value=self.install)
        validate_execution_approval(self.plan, self.approval, operation='combined',
                                    fresh_plan=self.plan, inspector=inspector)
        invalid = replace(self.plan, stages=(replace(self.plan.stages[-1],
                                                    previous_netbox=self.plan.current_netbox),))
        with self.assertRaisesRegex(UpgradeExecutionError, 'required release stage'):
            validate_execution_approval(invalid, self.approval, operation='combined',
                                        fresh_plan=invalid, inspector=inspector)

    def test_final_dependency_conflict_blocks_all_mutations(self):
        self.resolver.side_effect = [DependencyPreflight('passed', 'ok', True),
                                     DependencyPreflight('blocked', 'conflict', True)]
        with self.assertRaisesRegex(UpgradeExecutionError, 'conflicting Python'):
            self.prepare()
        self.assertEqual((self.root / 'local_requirements.txt').read_bytes(), LOCAL)

    def test_recheck_rejects_new_unknown_resolution(self):
        prepared = self.prepare()[0]
        self.resolver.return_value = DependencyPreflight('unknown', 'network', False)
        with self.assertRaisesRegex(UpgradeExecutionError, 'became uncertain'):
            recheck_combined_stage(Mock(), self.install, self.plan.stages[0], prepared,
                                   self.approval, resolver=self.resolver, probe=self.probe,
                                   reviewer=self.reviewer)

    def test_recheck_rejects_new_release_warning(self):
        prepared = self.prepare()[0]

        def warning(*args, **kwargs):
            return replace(self.reviewer(*args, **kwargs), warnings=('New release warning',))

        with self.assertRaisesRegex(UpgradeExecutionError, 'new unvalidated plugin warnings'):
            recheck_combined_stage(Mock(), self.install, self.plan.stages[0], prepared,
                                   self.approval, resolver=self.resolver, probe=self.probe,
                                   reviewer=warning)

    def test_recheck_rejects_new_plugin_conflict(self):
        prepared = self.prepare()[0]

        def blocked(*args, **kwargs):
            review = self.reviewer(*args, **kwargs)
            return replace(review, stages=(replace(review.stages[0], status='blocked'),))

        with self.assertRaisesRegex(UpgradeExecutionError, 'incompatible plugin'):
            recheck_combined_stage(Mock(), self.install, self.plan.stages[0], prepared,
                                   self.approval, resolver=self.resolver, probe=self.probe,
                                   reviewer=blocked)

    def test_recheck_reads_real_server_prerequisites(self):
        prepared = self.prepare()[0]
        self.probe.return_value = ('10.0', '7.2')
        with self.assertRaisesRegex(UpgradeExecutionError, 'incompatible server'):
            recheck_combined_stage(Mock(), self.install, self.plan.stages[0], prepared,
                                   self.approval, resolver=self.resolver, probe=self.probe,
                                   reviewer=self.reviewer)

    def test_stage_applies_final_pins_before_script_with_constraints(self):
        prepared = self.prepare()[1]
        final = replace(self.install, commit='c' * 40, version='4.0.1')
        (self.root / 'requirements.txt').write_text(CORE + '\n')
        inventory = replace(
            self.inventory,
            requirements_hash=hashlib.sha256(prepared.pins.requirements).hexdigest(),
            packages=(replace(self.packages[0], version='1.5.0'), self.packages[1],
                      InstalledPackage('Django', '4.2.15', ()),
                      InstalledPackage('psycopg', '3.2.1', ())),
        )
        systemd = Mock()
        systemd.status.return_value = [
            SimpleNamespace(name=name, loaded='loaded', active='inactive')
            for name in ('netbox', 'netbox-rq')
        ]
        reader = Mock(side_effect=[self.inventory, inventory])
        runner = Mock(return_value=SimpleNamespace(
            returncode=0, stdout='{"example_plugin":"1.5.0","other_plugin":"2.0.0"}',
        ))
        with patch('netbox_ops.upgrade_combined._apply_requirements') as apply:
            def stage_runner(live, pinned, **kwargs):
                self.assertEqual(pinned, prepared.pinned)
                env = kwargs['prepare_script'](final)
                apply.assert_called_once_with(self.root, prepared.pins)
                constraints = Path(env['PIP_CONSTRAINT']).read_text()
                self.assertIn('example-plugin==1.5.0', constraints)
                self.assertIn('other-plugin==2.0.0', constraints)
                self.assertIn('django==4.2.15', constraints)
                self.assertEqual(env['PIP_ONLY_BINARY'], ':all:')
                return final
            result, loaded = run_combined_stage(
                self.install, self.plan, prepared, Path('/usr/bin/python3.10'),
                systemd=systemd, stage_runner=stage_runner, inventory_reader=reader,
                runner=runner,
            )
        self.assertEqual(result, final)
        self.assertEqual(loaded, (('example_plugin', '1.5.0'), ('other_plugin', '2.0.0')))
        self.assertEqual(runner.call_args_list[0].args[0][-1], 'check')

    def test_changed_plugin_before_stage_blocks_checkout(self):
        prepared = self.prepare()[0]
        systemd = Mock()
        systemd.status.return_value = [
            SimpleNamespace(name=name, loaded='loaded', active='inactive')
            for name in ('netbox', 'netbox-rq')
        ]
        reader = Mock(return_value=replace(
            self.inventory, packages=(replace(self.packages[0], version='9.0.0'), self.packages[1]),
        ))
        stage_runner = Mock()
        with self.assertRaises(UpgradeExecutionError):
            run_combined_stage(self.install, self.plan, prepared, Path('/usr/bin/python3.10'),
                               systemd=systemd, stage_runner=stage_runner, inventory_reader=reader)
        stage_runner.assert_not_called()

    def test_probe_uses_lowest_redis_version_and_nonroot_user(self):
        runtime = SimpleNamespace(python=self.install.python,
                                  working_directory=self.root / 'netbox',
                                  username='netbox', environment={'SECRET': 'hidden'})
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='["15.2", "7.2.1", "6.2.5"]'))
        with patch('netbox_ops.upgrade_combined.pwd.getpwnam',
                   return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name='netbox')), patch(
            'netbox_ops.upgrade_combined.os.getgrouplist', return_value=[1001],
        ):
            result = probe_server_versions(Mock(), self.install, runner=runner,
                                           discover=Mock(return_value=runtime))
        self.assertEqual(result, ('15.2', '6.2.5'))
        self.assertEqual(runner.call_args.kwargs['user'], 1001)


if __name__ == '__main__':
    unittest.main()
