"""Exercise policy installation and actual rotation in an isolated directory."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from netbox_ops.logrotate_policy import install_policy, policy_text
from netbox_ops.services import write_audit


@unittest.skipUnless(os.geteuid() == 0, 'root-owned policy required')
class LogrotateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy_dir = self.root / 'logrotate.d'
        self.policy_dir.mkdir(mode=0o700)
        self.log = self.root / 'logs' / 'operations.log'
        self.policy = self.policy_dir / 'netbox-ops'

    def test_missing_dependency_is_explicit(self):
        with patch('netbox_ops.logrotate_policy.shutil.which', return_value=None):
            with self.assertRaisesRegex(OSError, 'logrotate is unavailable'):
                install_policy(self.policy_dir, self.log)
        self.assertFalse(self.policy.exists())

    def test_idempotent_and_preserves_existing_policy(self):
        with patch('netbox_ops.logrotate_policy.shutil.which', return_value='/usr/sbin/logrotate'), \
             patch('netbox_ops.logrotate_policy.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertTrue(install_policy(self.policy_dir, self.log))
            self.assertEqual(self.policy.read_text(), policy_text(self.log))
            self.assertEqual(self.policy.stat().st_mode & 0o777, 0o600)
            self.assertFalse(install_policy(self.policy_dir, self.log))
            run.assert_called_once()
            self.policy.write_text('administrator configuration')
            with self.assertRaises(OSError):
                install_policy(self.policy_dir, self.log)
            self.assertEqual(self.policy.read_text(), 'administrator configuration')
            self.policy.unlink()
            self.policy.symlink_to(self.log)
            with self.assertRaises(OSError):
                install_policy(self.policy_dir, self.log)
            self.assertTrue(self.policy.is_symlink())

    def test_invalid_policy_not_published(self):
        with patch('netbox_ops.logrotate_policy.shutil.which', return_value='/usr/sbin/logrotate'), \
             patch('netbox_ops.logrotate_policy.subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = 'invalid policy'
            with self.assertRaisesRegex(OSError, 'validation failed'):
                install_policy(self.policy_dir, self.log)
        self.assertFalse(self.policy.exists())
        self.assertEqual(list(self.policy_dir.iterdir()), [])

    @unittest.skipUnless(shutil.which('logrotate'), 'logrotate executable required')
    def test_real_rotation_preserves_audit_entries(self):
        write_audit('before rotation', self.log)
        self.assertTrue(install_policy(self.policy_dir, self.log))
        state = self.root / 'state'
        subprocess.run(['logrotate', '-f', '-s', str(state), str(self.policy)], check=True)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        write_audit('after rotation', self.log)
        self.assertIn('before rotation', self.log.with_suffix('.log.1').read_text())
        self.assertIn('after rotation', self.log.read_text())
