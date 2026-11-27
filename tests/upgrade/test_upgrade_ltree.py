"""Exercise explicit local ltree approval and backup gates without real mutations."""
import hashlib
import os
import sys
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops import upgrade_ltree as ltree
from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_runtime import LtreeEvidence, _ltree_admin_sql


class LtreeInstallTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Configuration(DEFAULT_UNITS)
        self.live = SimpleNamespace(root=Path('/opt/netbox'), version='4.4.10', commit='a' * 40,
                                    python=Path('/opt/netbox/venv/bin/python'), python_version='3.12.3')
        self.identity = 'netbox|5432|1234'
        self.digest = hashlib.sha256(self.identity.encode()).hexdigest()
        self.evidence = LtreeEvidence('permission', ('public schema, local role',), self.digest, 5432)
        self.calls = Mock()
        for name in ('inspect_git_installation', 'probe_ltree', 'upgrade_recovery_pending',
                     'BackupManager', '_private_directory', '_private_file', '_sql',
                     '_ltree_admin_sql', 'write_audit'):
            self.enterContext(patch.object(ltree, name, getattr(self.calls, name)))
        self.enterContext(patch.object(ltree, 'ExclusiveLock', nullcontext))
        self.enterContext(patch.object(ltree.os, 'geteuid', return_value=0))
        self.calls.inspect_git_installation.return_value = self.live
        self.calls.probe_ltree.return_value = self.evidence
        self.calls.upgrade_recovery_pending.return_value = False
        self.calls._sql.return_value = self.identity
        self.calls._ltree_admin_sql.return_value = 'installed'
        self.backup = self.cfg.backup_directory / 'netbox-db-test.dump'
        self.manager = self.calls.BackupManager.return_value
        self.manager._backup.return_value = SimpleNamespace(kind='db', path=self.backup)
        self.manager.verify.return_value = {'kind': 'db', 'database': self.cfg.database,
                                            'netbox_version': self.live.version}
        self.review = ltree.build_ltree_review(self.cfg)

    def apply(self):
        return ltree.apply_ltree_review(self.cfg, self.review, self.review.fingerprint)

    def test_read_only_review_and_exact_approval(self):
        self.assertTrue(self.review.fingerprint)
        self.manager._backup.assert_not_called()
        self.calls._ltree_admin_sql.assert_not_called()
        with self.assertRaisesRegex(UpgradeExecutionError, 'exact ltree installation approval'):
            ltree.apply_ltree_review(self.cfg, self.review, 'wrong')
        self.manager._backup.assert_not_called()

    def test_unverified_admin_not_eligible(self):
        self.calls.probe_ltree.return_value = replace(self.evidence, admin_port=None)
        self.assertIsNone(ltree.build_ltree_review(self.cfg).fingerprint)
        with self.assertRaisesRegex(UpgradeExecutionError, 'changed after approval'):
            self.apply()
        self.manager._backup.assert_not_called()

    def test_recovery_pending_blocks_mutation(self):
        self.calls.upgrade_recovery_pending.return_value = True
        with self.assertRaisesRegex(UpgradeExecutionError, 'recovery is pending'):
            self.apply()
        self.manager._backup.assert_not_called()

    def test_unverified_backup_never_creates_extension(self):
        self.manager.verify.return_value = {'kind': 'db', 'database': 'other'}
        with self.assertRaisesRegex(UpgradeExecutionError, 'backup:'):
            self.apply()
        self.calls._ltree_admin_sql.assert_not_called()

    def test_changed_database_after_backup_never_creates_extension(self):
        self.calls.probe_ltree.side_effect = [self.evidence, replace(self.evidence, identity='other')]
        with self.assertRaisesRegex(UpgradeExecutionError, 'backup:'):
            self.apply()
        self.calls._ltree_admin_sql.assert_not_called()

    def test_success_requires_verified_backup_and_post_install_probe(self):
        installed = LtreeEvidence('installed', (), self.digest)
        self.calls.probe_ltree.side_effect = [self.evidence, self.evidence, installed]
        self.assertEqual(self.apply(), self.backup)
        self.calls._ltree_admin_sql.assert_called_once_with(self.cfg, self.identity, 5432, install=True)
        names = [call[0] for call in self.calls.mock_calls]
        ordered = ['BackupManager()._backup', 'BackupManager().verify', '_ltree_admin_sql']
        self.assertEqual([names.index(n) for n in ordered], sorted(names.index(n) for n in ordered))
        self.assertEqual(self.calls.probe_ltree.call_count, 4)

    def test_unusable_post_install_reports_backup_no_rollback(self):
        self.calls.probe_ltree.side_effect = [
            self.evidence, self.evidence, LtreeEvidence('unusable', (), self.digest),
        ]
        with self.assertRaisesRegex(UpgradeExecutionError, 'no automatic rollback'):
            self.apply()
        self.calls._ltree_admin_sql.assert_called_once()


class LocalAdminSqlTests(unittest.TestCase):
    def test_fixed_local_connection_and_read_only_review(self):
        cfg = Configuration(DEFAULT_UNITS, postgres_port=5432)
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='t'))
        with patch.dict(os.environ, {}, clear=True), patch('netbox_ops.upgrade_runtime.pg_binary', return_value=sys.executable):
            self.assertEqual(_ltree_admin_sql(cfg, 'netbox|5432|1234', 5432, runner=runner), 't')
            command = runner.call_args.args[0]
            self.assertIn('/var/run/postgresql', command)
            self.assertIn('nbo_identity=netbox|5432|1234', command)
            self.assertIn('BEGIN READ ONLY', runner.call_args.kwargs['input'])
            self.assertNotIn('CREATE EXTENSION', runner.call_args.kwargs['input'])
            runner.return_value.stdout = 'installed'
            self.assertEqual(_ltree_admin_sql(cfg, 'netbox|5432|1234', 5432, install=True, runner=runner), 'installed')
            script = runner.call_args.kwargs['input']
            self.assertIn('CREATE EXTENSION IF NOT EXISTS ltree WITH SCHEMA public;', script)
            self.assertIn('has_schema_privilege', script)
            self.assertNotIn('GRANT ', script)
            self.assertNotIn('DROP EXTENSION', script)

    def test_remote_environment_or_wrong_identity_never_runs_psql(self):
        cfg = Configuration(DEFAULT_UNITS, postgres_port=5432)
        runner = Mock()
        with patch.dict(os.environ, {'PGHOST': 'other-server'}, clear=True):
            with self.assertRaises(UpgradeExecutionError):
                _ltree_admin_sql(cfg, 'netbox|5432|1234', 5432, install=True, runner=runner)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(UpgradeExecutionError):
                _ltree_admin_sql(cfg, 'other|5432|1234', 5432, install=True, runner=runner)
        runner.assert_not_called()
