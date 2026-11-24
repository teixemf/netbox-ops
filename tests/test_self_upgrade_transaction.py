"""Offline failure-injection tests for the self-upgrade swap transaction."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_transaction import (
    SwapError, begin_swap, finish_swap, rollback_swap,
)


class SwapTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o700)
        self.active = root / 'netbox-ops'
        self.staged = root / '.netbox-ops-stage'
        self.backup = root / '.netbox-ops-backup'
        self.state = root / '.netbox-ops-transaction.json'
        self.active.mkdir()
        self.staged.mkdir()
        (self.active / 'version').write_text('old')
        (self.staged / 'version').write_text('new')

    def test_swap_retains_old_tree_until_validation(self):
        begin_swap(self.active, self.staged, self.backup, self.state)
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertEqual((self.backup / 'version').read_text(), 'old')
        finish_swap(self.active, self.staged, self.backup, self.state)
        with self.assertRaises(SwapError):
            rollback_swap(self.active, self.staged, self.backup, self.state)

    def test_rollback_restores_old_installation(self):
        begin_swap(self.active, self.staged, self.backup, self.state)
        rollback_swap(self.active, self.staged, self.backup, self.state)
        self.assertEqual((self.active / 'version').read_text(), 'old')
        self.assertEqual((self.staged / 'version').read_text(), 'new')
        self.assertFalse(self.state.exists())

    def test_interrupted_after_first_rename_can_recover(self):
        original = __import__('os').rename
        calls = []

        def interrupted(source, destination):
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError('simulated interruption')
            return original(source, destination)

        with patch('netbox_ops.self_upgrade_transaction.os.rename', side_effect=interrupted):
            with self.assertRaises(OSError):
                begin_swap(self.active, self.staged, self.backup, self.state)
        rollback_swap(self.active, self.staged, self.backup, self.state)
        self.assertEqual((self.active / 'version').read_text(), 'old')

    def test_reject_second_transaction(self):
        begin_swap(self.active, self.staged, self.backup, self.state)
        with self.assertRaises(SwapError):
            begin_swap(self.active, self.staged, self.backup, self.state)

    def test_reject_symlinked_staging(self):
        self.staged.rmdir()
        self.staged.symlink_to(self.active, target_is_directory=True)
        with self.assertRaises(SwapError):
            begin_swap(self.active, self.staged, self.backup, self.state)


if __name__ == '__main__':
    unittest.main()
