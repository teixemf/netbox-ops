"""Test maintenance markers in isolated temporary directories."""
import os
import tempfile
import unittest
from pathlib import Path

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_maintenance import begin_maintenance, finish_maintenance


@unittest.skipUnless(os.geteuid() == 0, 'Root ownership required')
class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.marker = self.root / 'state' / 'upgrade.pending'

    def test_pending_marker_blocks_second_upgrade(self):
        begin_maintenance(self.marker)
        original = self.marker.read_bytes()
        with self.assertRaises(UpgradeExecutionError):
            begin_maintenance(self.marker)
        self.assertEqual(self.marker.read_bytes(), original)
        finish_maintenance(self.marker)
        self.assertFalse(self.marker.exists())

    def test_symlink_marker_is_not_followed(self):
        self.marker.parent.mkdir(mode=0o700)
        target = self.root / 'target'
        target.write_text('keep')
        self.marker.symlink_to(target)
        with self.assertRaises(UpgradeExecutionError):
            begin_maintenance(self.marker)
        with self.assertRaises(UpgradeExecutionError):
            finish_maintenance(self.marker)
        self.assertEqual(target.read_text(), 'keep')

    def test_writable_directory_is_rejected(self):
        self.marker.parent.mkdir()
        self.marker.parent.chmod(0o777)
        with self.assertRaises(UpgradeExecutionError):
            begin_maintenance(self.marker)
        self.assertFalse(self.marker.exists())

    def test_hardlinked_marker_is_not_removed(self):
        begin_maintenance(self.marker)
        os.link(self.marker, self.root / 'alias')
        with self.assertRaises(UpgradeExecutionError):
            finish_maintenance(self.marker)
        self.assertTrue(self.marker.exists())
