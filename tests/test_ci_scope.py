"""Regression checks for selective CI coverage."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci import test_scope as scope


class TestScope(unittest.TestCase):
    def test_docs_only(self):
        self.assertEqual(scope.select(['README.md', 'docs/NETBOX_UPGRADE.md',
                                       'tests/README.md', 'CONTRIBUTING.md']), set())

    def test_translations_and_unit_tests(self):
        self.assertEqual(scope.select(['netbox_ops/i18n/pt.json', 'tests/cli/test_cli.py']), {'unit'})

    def test_upgrade_domains_are_independent(self):
        self.assertEqual(scope.select(['netbox_ops/upgrade_preflight.py']), {'unit', 'netbox'})
        self.assertEqual(scope.select(['netbox_ops/self_upgrade_handoff.py']),
                         {'unit', 'package', 'self_upgrade'})
        self.assertEqual(scope.select(['netbox_ops/plugin_inventory.py',
                                       'netbox_ops/self_upgrade_archive.py']),
                         {'unit', 'package', 'netbox', 'self_upgrade'})

    def test_backup_and_integration_paths(self):
        self.assertEqual(scope.select(['netbox_ops/backups.py']),
                         {'unit', 'postgres', 'netbox', 'baseline'})
        self.assertEqual(scope.select(['tests/integration/run_postgres.sh']), {'unit', 'postgres'})

    def test_shared_and_unknown_paths_run_everything(self):
        for path in ('netbox_ops/services.py', 'netbox_ops/config.py', 'install.sh',
                     'netbox_ops/__init__.py', 'netbox_ops/new_module.py',
                     'scripts/ci/test_scope.py', '.github/workflows/unknown.yml'):
            with self.subTest(path=path):
                self.assertEqual(scope.select([path]), scope.ALL)

    def test_fixture_paths_do_not_trigger_unrelated_vms(self):
        self.assertEqual(scope.select(['scripts/ci/netbox-vm-upgrade-e2e.sh']), {'unit', 'netbox'})
        self.assertEqual(scope.select(['scripts/ci/netbox-vm-self-upgrade.py']),
                         {'unit', 'self_upgrade'})
        self.assertEqual(scope.select(['scripts/ci/netbox-vm-host.sh']), scope.VMS | {'unit'})

    def test_release_and_main_events_are_full(self):
        for event, payload, ref in (
            ('workflow_dispatch', {}, 'refs/heads/dev'),
            ('workflow_call', {}, 'refs/heads/main'),
            ('push', {}, 'refs/heads/main'),
            ('pull_request', {'pull_request': {'base': {'ref': 'main'}}}, ''),
        ):
            with self.subTest(event=event):
                self.assertEqual(scope.event_scope(event, payload, ref), scope.ALL)

    def test_dev_pr_and_push_use_actual_base(self):
        with patch.object(scope, 'changed_paths', return_value=['README.md']) as diff:
            self.assertEqual(scope.event_scope('pull_request', {
                'pull_request': {'base': {'ref': 'dev', 'sha': 'a' * 40}},
            }, ''), set())
            diff.assert_called_once_with('a' * 40)
            self.assertEqual(scope.event_scope('push', {'before': 'b' * 40}, 'refs/heads/dev'), set())
            diff.assert_called_with('b' * 40)

    def test_unavailable_diff_runs_all_suites(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / 'event.json'
            event.write_text('{"pull_request":{"base":{"ref":"dev","sha":"invalid"}}}',
                             encoding='utf-8')
            output = Path(temp) / 'output'
            with patch.dict(os.environ, {
                'GITHUB_EVENT_PATH': str(event),
                'GITHUB_EVENT_NAME': 'pull_request',
                'GITHUB_REF': 'refs/pull/1/merge',
                'GITHUB_OUTPUT': str(output),
            }), patch('sys.stdout', new_callable=io.StringIO):
                scope.main()
            self.assertEqual(set(output.read_text().splitlines()),
                             {f'{name}=true' for name in scope.ALL})

    def test_rename_and_unusual_path_classification(self):
        with patch.object(scope.subprocess, 'run'), patch.object(
            scope.subprocess, 'check_output',
            return_value=b'netbox_ops/upgrade_plan.py\0docs/file with spaces.md\0',
        ) as git:
            paths = scope.changed_paths('a' * 40)
            self.assertEqual(scope.select(paths), {'unit', 'netbox'})
            self.assertIn('--no-renames', git.call_args.args[0])
        with self.assertRaises(ValueError):
            scope.changed_paths('0' * 40)

    def test_new_modules_with_known_prefixes_default_to_full(self):
        for path in ('netbox_ops/upgrade_new.py', 'netbox_ops/plugin_new.py',
                     'netbox_ops/self_upgrade_new.py'):
            with self.subTest(path=path):
                self.assertEqual(scope.file_scope(path), scope.ALL)

    def test_shared_english_translation_runtime_runs_vm_suites(self):
        for path in ('netbox_ops/i18n.py', 'netbox_ops/i18n/en.json'):
            with self.subTest(path=path):
                self.assertEqual(scope.file_scope(path), scope.VMS | {'unit'})
        self.assertEqual(scope.file_scope('netbox_ops/i18n/pt.json'), {'unit'})

    def test_explanation_includes_each_changed_path(self):
        paths = ['docs/NETBOX_UPGRADE.md', 'netbox_ops/upgrade_plan.py']
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(scope.explain_selection(paths), {'unit', 'netbox'})
        self.assertIn("Changed path 'docs/NETBOX_UPGRADE.md': lightweight checks only",
                      output.getvalue())
        self.assertIn("Changed path 'netbox_ops/upgrade_plan.py': netbox, unit",
                      output.getvalue())
