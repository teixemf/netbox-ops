"""Isolated persistence tests for self-upgrade results."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_result import read_result, result_for, write_result
from netbox_ops.self_upgrade_transaction import SwapError


class ResultTests(unittest.TestCase):
    record = {'installed': {'version': {'major': 0, 'minor': 5, 'patch': 0}},
              'steps': ['v0.6.0', 'v0.7.0'], 'target': 'v0.7.0'}

    def test_invalid_phase_or_target_refused(self):
        with self.assertRaises(SwapError):
            result_for(self.record, 'unknown')
        with self.assertRaises(SwapError):
            result_for({**self.record, 'target': 'v0.4.0'}, 'completed')

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned isolated files required')
    def test_round_trip_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'result.json'
            self.assertIsNone(read_result(path=path))
            write_result(self.record, 'submitted', path=path)
            self.assertEqual(read_result(path=path), result_for(self.record, 'submitted'))
            write_result(self.record, 'completed', path=path)
            self.assertEqual(read_result(path=path)['phase'], 'completed')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned isolated files required')
    def test_failed_replace_preserves_previous_result(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'result.json'
            write_result(self.record, 'submitted', path=path)
            with patch('netbox_ops.self_upgrade_result.os.replace', side_effect=OSError):
                with self.assertRaises(OSError):
                    write_result(self.record, 'completed', path=path)
            self.assertEqual(read_result(path=path)['phase'], 'submitted')

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned isolated files required')
    def test_unsafe_result_refused(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'result.json'
            write_result(self.record, 'submitted', path=path)
            path.chmod(0o666)
            with self.assertRaises(SwapError):
                read_result(path=path)
            path.unlink()
            path.symlink_to('/dev/null')
            with self.assertRaises(OSError):
                read_result(path=path)
