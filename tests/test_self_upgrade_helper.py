"""Offline tests for the locked, audited self-upgrade coordinator."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_helper import (
    recover_interrupted_upgrade, run_approved_swap, wait_for_initiator_exit,
)
from netbox_ops.self_upgrade_transaction import SwapError, begin_swap, finish_swap


class HelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o700)
        self.active = root / 'netbox-ops'
        self.staged = root / '.netbox-ops-stage-test'
        self.active.mkdir()
        self.staged.mkdir()
        (self.active / 'version').write_text('old')
        (self.staged / 'version').write_text('new')
        self.log = []
        waiter = patch('netbox_ops.self_upgrade_helper.wait_for_initiator_exit')
        self.waiter = waiter.start()
        self.addCleanup(waiter.stop)

    def run_helper(self, preflight=None, validate=None):
        from netbox_ops import self_upgrade_helper as helper
        self.log = []
        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock') as lock,
              patch.object(helper, 'write_audit', side_effect=lambda msg, **kw: self.log.append(msg))):
            run_approved_swap(
                self.staged,
                initiator_pid=12345,
                preflight=preflight if preflight is not None else lambda current, candidate: None,
                validate=validate if validate is not None else lambda path: None,
                active=self.active,
            )
            lock.assert_called_once()

    def test_success_requires_preflight_and_validation(self):
        checks = []

        def before(current, candidate):
            checks.append(('before', (current / 'version').read_text(),
                           (candidate / 'version').read_text()))

        def after(path):
            checks.append(('after', (path / 'version').read_text()))

        self.run_helper(preflight=before, validate=after)
        self.assertEqual(checks, [('before', 'old', 'new'), ('after', 'new')])
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertFalse((self.active.parent / '.netbox-ops-backup').exists())
        self.assertIn('SELF-UPGRADE finished', self.log)

    def test_failed_preflight_never_moves_installation(self):
        def deny(current, candidate):
            raise ValueError('incompatible release')

        with self.assertRaises(ValueError):
            self.run_helper(preflight=deny)
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertTrue(self.staged.exists())
        self.assertFalse((self.active.parent / '.netbox-ops-transaction.json').exists())
        self.assertTrue(any('preflight failed' in line for line in self.log))

    def test_failed_validation_rolls_back(self):
        def reject(path):
            raise ValueError('invalid candidate')

        with self.assertRaises(ValueError):
            self.run_helper(validate=reject)
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertEqual((self.staged / 'version').read_text(), 'new')
        self.assertFalse((self.active.parent / '.netbox-ops-transaction.json').exists())
        self.assertIn('SELF-UPGRADE rollback completed', self.log)

    def test_unprivileged_helper_fails_closed(self):
        from netbox_ops import self_upgrade_helper as helper
        with patch.object(helper.os, 'geteuid', return_value=1000):
            with self.assertRaises(SwapError):
                run_approved_swap(self.staged, initiator_pid=12345, preflight=lambda current, candidate: None,
                                  validate=lambda path: None, active=self.active)
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_existing_transaction_is_not_overwritten(self):
        state = self.active.parent / '.netbox-ops-transaction.json'
        state.write_text('existing transaction')
        with self.assertRaises(SwapError):
            self.run_helper()
        self.assertEqual(state.read_text(), 'existing transaction')
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_no_service_operation_in_coordinator(self):
        from netbox_ops import self_upgrade_helper as helper
        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit')):
            self.run_helper()
        self.assertEqual((self.active / 'version').read_text(), 'new')

    def test_pidfd_wait_rejects_invalid_values(self):
        for pid in (0, -1, os.getpid(), True):
            with self.subTest(pid=pid), self.assertRaises(SwapError):
                wait_for_initiator_exit(pid)
        with self.assertRaises(SwapError):
            wait_for_initiator_exit(12345, timeout_seconds=-1)

    def test_pidfd_wait_accepts_already_exited_process(self):
        from netbox_ops import self_upgrade_helper as helper
        with patch.object(helper.os, 'pidfd_open', side_effect=ProcessLookupError):
            wait_for_initiator_exit(99999999)

    def test_pidfd_wait_rejects_timeout(self):
        from netbox_ops import self_upgrade_helper as helper
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        with patch.object(helper.os, 'pidfd_open', return_value=reader):
            with self.assertRaises(SwapError):
                wait_for_initiator_exit(12345, timeout_seconds=1)

    def test_audit_failure_prevents_any_swap(self):
        from netbox_ops import self_upgrade_helper as helper
        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit', side_effect=OSError('log unavailable'))):
            with self.assertRaises(OSError):
                run_approved_swap(self.staged, initiator_pid=12345, preflight=lambda current, candidate: None,
                                  validate=lambda path: None, active=self.active)
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertFalse((self.active.parent / '.netbox-ops-transaction.json').exists())

    def test_audit_failure_after_swap_rolls_back(self):
        from netbox_ops import self_upgrade_helper as helper

        def audit(message, **kwargs):
            if 'validation pending' in message:
                raise OSError('log unavailable')
            self.log.append(message)

        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit', side_effect=audit)):
            with self.assertRaises(OSError):
                run_approved_swap(self.staged, initiator_pid=12345, preflight=lambda current, candidate: None,
                                  validate=lambda path: None, active=self.active)
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertIn('SELF-UPGRADE rollback completed', self.log)

    def test_failed_rollback_retains_transaction_for_recovery(self):
        from netbox_ops import self_upgrade_helper as helper

        def invalid(path):
            raise ValueError('rejected')

        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit'),
              patch.object(helper, 'rollback_swap', side_effect=OSError('disk error'))):
            with self.assertRaises(SwapError):
                run_approved_swap(self.staged, initiator_pid=12345, preflight=lambda current, candidate: None,
                                  validate=invalid, active=self.active)
        self.assertTrue((self.active.parent / '.netbox-ops-backup').exists())
        self.assertTrue((self.active.parent / '.netbox-ops-transaction.json').exists())

    def test_cleanup_failure_keeps_validated_installation(self):
        from netbox_ops import self_upgrade_helper as helper
        from netbox_ops.self_upgrade_transaction import cleanup_swap

        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit'),
              patch.object(helper, 'cleanup_swap', side_effect=OSError('disk error'))):
            with self.assertRaises(OSError):
                run_approved_swap(self.staged, initiator_pid=12345, preflight=lambda current, candidate: None,
                                  validate=lambda path: None, active=self.active)
        backup = self.active.parent / '.netbox-ops-backup'
        state = self.active.parent / '.netbox-ops-transaction.json'
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertTrue(backup.exists())
        self.assertTrue(state.exists())
        cleanup_swap(self.active, self.staged, backup, state)
        self.assertFalse(state.exists())


    def test_recovery_uses_exclusive_lock_and_audit(self):
        from netbox_ops import self_upgrade_helper as helper
        backup = self.active.parent / '.netbox-ops-backup'
        state = self.active.parent / '.netbox-ops-transaction.json'
        begin_swap(self.active, self.staged, backup, state)
        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock') as lock,
              patch.object(helper, 'write_audit', side_effect=lambda msg, **kw: self.log.append(msg))):
            self.assertEqual(recover_interrupted_upgrade(active=self.active), 'rollback')
            lock.assert_called_once()
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertIn('SELF-UPGRADE recovery finished result=rollback', self.log)

    def test_recovery_audit_failure_prevents_mutation(self):
        from netbox_ops import self_upgrade_helper as helper
        backup = self.active.parent / '.netbox-ops-backup'
        state = self.active.parent / '.netbox-ops-transaction.json'
        begin_swap(self.active, self.staged, backup, state)
        finish_swap(self.active, self.staged, backup, state)
        with (patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'ExclusiveLock'),
              patch.object(helper, 'write_audit', side_effect=OSError('log unavailable'))):
            with self.assertRaises(OSError):
                recover_interrupted_upgrade(active=self.active)
        self.assertTrue(backup.exists())
        self.assertTrue(state.exists())

    def test_recovery_refuses_non_root(self):
        from netbox_ops import self_upgrade_helper as helper
        with patch.object(helper.os, 'geteuid', return_value=1000):
            with self.assertRaises(SwapError):
                recover_interrupted_upgrade(active=self.active)


if __name__ == '__main__':
    unittest.main()
