"""Exercise actual harmless Linux subprocess groups, not mock timeouts."""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch

from netbox_ops.upgrade_process import (
    ManagedProcessError, UpgradeInterrupted, _group_active, run_managed, upgrade_signals,
)


class ManagedProcessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ready = self.root / 'ready'
        self.changed = self.root / 'changed'
        self.child = (
            "import pathlib,signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"pathlib.Path({str(self.ready)!r}).touch(); "
            "time.sleep(4); "
            f"pathlib.Path({str(self.changed)!r}).touch()"
        )

    def command(self, linger):
        code = (
            "import pathlib,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{self.child!r}]); "
            f"p=pathlib.Path({str(self.ready)!r}); "
            "exec('while not p.exists(): time.sleep(0.01)'); "
            f"time.sleep({linger})"
        )
        return [sys.executable, '-c', code]

    def test_normal_exit_and_output(self):
        result = run_managed([sys.executable, '-c', 'print("hello")'],
                             timeout=5, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, 'hello\n')
        self.assertEqual(result.returncode, 0)

    def test_timeout_terminates_term_resistant_descendant(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_managed(self.command(60), timeout=1, stdout=subprocess.DEVNULL)
        self.assertTrue(self.ready.exists())
        time.sleep(1.2)
        self.assertFalse(self.changed.exists())

    def test_successful_leader_cannot_leave_background_writer(self):
        with self.assertRaises(ManagedProcessError):
            run_managed(self.command(0), timeout=5)
        time.sleep(2.1)
        self.assertFalse(self.changed.exists())

    def test_signal_in_main_thread_reaches_worker_and_cleans_group(self):
        errors = []

        def worker():
            try:
                run_managed(self.command(60), timeout=10)
            except BaseException as exc:
                errors.append(exc)

        previous = signal.getsignal(signal.SIGTERM)
        with upgrade_signals():
            thread = threading.Thread(target=copy_context().run, args=(worker,))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not self.ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(self.ready.exists())
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], UpgradeInterrupted)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        time.sleep(2.1)
        self.assertFalse(self.changed.exists())


class ProcessGroupScanTests(unittest.TestCase):
    def test_disappearing_proc_entry_does_not_hide_active_group(self):
        entries = [Path('/proc/123'), Path('/proc/456')]
        with (
            patch.object(Path, 'iterdir', return_value=iter(entries)),
            patch.object(Path, 'read_bytes', side_effect=[
                FileNotFoundError(), b'456 (worker) S 1 42 42',
            ]),
        ):
            self.assertTrue(_group_active(42))

    def test_disappearing_proc_entry_alone_is_not_an_active_group(self):
        with (
            patch.object(Path, 'iterdir', return_value=iter([Path('/proc/123')])),
            patch.object(Path, 'read_bytes', side_effect=ProcessLookupError()),
        ):
            self.assertFalse(_group_active(42))

    def test_unreadable_proc_entry_fails_conservatively(self):
        with (
            patch.object(Path, 'iterdir', return_value=iter([Path('/proc/123')])),
            patch.object(Path, 'read_bytes', side_effect=PermissionError()),
        ):
            self.assertTrue(_group_active(42))
