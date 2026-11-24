"""Offline failure-injection tests for the self-upgrade swap transaction."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops import self_upgrade_transaction as tx
from netbox_ops.self_upgrade_transaction import (
    SwapError, begin_swap, cleanup_swap, finish_swap, rollback_swap,
)


class SwapTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o700)
        self.active = root / 'netbox-ops'
        self.staged = root / '.netbox-ops-stage-test'
        self.backup = root / '.netbox-ops-backup'
        self.state = root / '.netbox-ops-transaction.json'
        self.active.mkdir()
        self.staged.mkdir()
        (self.active / 'version').write_text('old')
        (self.staged / 'version').write_text('new')

    def swap(self):
        begin_swap(self.active, self.staged, self.backup, self.state)

    def rollback(self):
        rollback_swap(self.active, self.staged, self.backup, self.state)

    def finish(self):
        finish_swap(self.active, self.staged, self.backup, self.state)

    def cleanup(self):
        cleanup_swap(self.active, self.staged, self.backup, self.state)

    def test_swap_retains_old_tree_until_validation(self):
        self.swap()
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertEqual((self.backup / 'version').read_text(), 'old')
        self.finish()
        with self.assertRaises(SwapError):
            self.rollback()
        self.assertTrue(self.backup.is_dir())

    def test_cleanup_only_after_validation(self):
        self.swap()
        with self.assertRaises(SwapError):
            self.cleanup()
        self.assertTrue(self.backup.exists())
        self.finish()
        self.cleanup()
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.state.exists())

    def test_rollback_restores_old_installation(self):
        self.swap()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertEqual((self.staged / 'version').read_text(), 'new')
        self.assertFalse(self.state.exists())

    def test_interrupted_before_first_rename_can_recover(self):
        with patch.object(tx.os, 'rename', side_effect=OSError('power loss')):
            with self.assertRaises(OSError):
                self.swap()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_interrupted_after_first_rename_can_recover(self):
        original = os.rename
        calls = []

        def interrupted(source, destination):
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError('power loss')
            return original(source, destination)

        with patch.object(tx.os, 'rename', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.swap()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_interrupted_before_recording_old_move_can_recover(self):
        original = tx._write_state
        calls = []

        def interrupted(path, data):
            calls.append(data['phase'])
            if len(calls) == 2:
                raise OSError('power loss')
            return original(path, data)

        with patch.object(tx, '_write_state', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.swap()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_interrupted_before_recording_new_active_can_recover(self):
        original = tx._write_state

        def interrupted(path, data):
            if data['phase'] == 'new_active':
                raise OSError('power loss')
            return original(path, data)

        with patch.object(tx, '_write_state', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.swap()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertEqual((self.staged / 'version').read_text(), 'new')

    def test_recovery_can_resume_interrupted_rollback(self):
        self.swap()
        original = os.rename
        count = []

        def interrupted(source, destination):
            count.append((source, destination))
            if len(count) == 2:
                raise OSError('power loss')
            return original(source, destination)

        with patch.object(tx.os, 'rename', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rollback()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertFalse(self.backup.exists())

    def test_recovery_after_backup_was_already_restored(self):
        self.swap()
        original = tx._sync_parent
        count = []

        def interrupted(path):
            count.append(path)
            if len(count) == 2:
                raise OSError('power loss')
            return original(path)

        with patch.object(tx, '_sync_parent', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rollback()
        self.rollback()
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertFalse(self.state.exists())

    def test_reject_second_transaction(self):
        self.swap()
        with self.assertRaises(SwapError):
            self.swap()

    def test_reject_symlinked_staging(self):
        (self.staged / 'version').unlink()
        self.staged.rmdir()
        self.staged.symlink_to(self.active, target_is_directory=True)
        with self.assertRaises(SwapError):
            self.swap()

    def test_reject_unsafe_parent(self):
        self.active.parent.chmod(0o777)
        with self.assertRaises(SwapError):
            self.swap()

    def test_reject_unknown_installation_layout(self):
        with self.assertRaises(SwapError):
            begin_swap(self.active.with_name('netbox'), self.staged,
                       self.backup, self.state)

    def test_reject_corrupted_transaction_state(self):
        self.swap()
        self.state.write_text('{"phase":"unknown"}')
        with self.assertRaises(SwapError):
            self.rollback()
        self.assertTrue(self.backup.exists())

    def test_reject_modified_backup_identity(self):
        self.swap()
        replacement = self.backup.with_name('replacement')
        self.backup.rename(replacement)
        self.backup.mkdir()
        with self.assertRaises(SwapError):
            self.rollback()
        self.assertTrue(replacement.exists())

    def test_refuse_occupied_staging_path_during_rollback(self):
        self.swap()
        self.staged.mkdir()
        with self.assertRaises(SwapError):
            self.rollback()
        self.assertTrue(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
