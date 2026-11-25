"""Verify bounded diagnostics without exposing subprocess content."""
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from netbox_ops.upgrade_diagnostics import CommandFailure, run_checked
from netbox_ops.upgrade_process import ManagedOutputError, run_managed


class CommandDiagnosticsTests(unittest.TestCase):
    def test_exit_status_and_signal_are_safe(self):
        for code, detail in ((7, 'exit status 7'), (-9, 'signal 9')):
            with self.subTest(code=code):
                result = subprocess.CompletedProcess(['secret'], code, 'secret' * 10000, 'password')
                with self.assertRaises(CommandFailure) as failure:
                    run_checked('plugin.migration', ['secret'], runner=Mock(return_value=result), timeout=30)
                message = str(failure.exception)
                self.assertIn(detail, message)
                self.assertIn('database migration state', message)
                self.assertNotIn('secret', message)
                self.assertNotIn('password', message)
                self.assertLess(len(message), 400)

    def test_exceptions_do_not_expose_untrusted_text(self):
        for error, detail in (
            (subprocess.TimeoutExpired(['secret'], 30, output='secret'), 'timed out after 30s'),
            (OSError('secret'), 'unavailable'),
            (ManagedOutputError('secret'), 'safety limits'),
        ):
            with self.subTest(error=type(error)):
                with self.assertRaises(CommandFailure) as failure:
                    run_checked('secret', ['secret'], runner=Mock(side_effect=error), timeout=30)
                self.assertIn(detail, str(failure.exception))
                self.assertNotIn('secret', str(failure.exception))
                self.assertIsNone(failure.exception.__cause__)

    def test_actual_capture_is_bounded(self):
        with patch('netbox_ops.upgrade_process._MAX_CAPTURE_BYTES', 4096):
            with self.assertRaises(ManagedOutputError) as failure:
                run_managed([sys.executable, '-c', "print('secret' * 10000)"],
                            timeout=10, capture_output=True)
        self.assertNotIn('secret', str(failure.exception))
