"""Linux process-group lifetime and cooperative upgrade interruption.

The group leader is not reaped until all live group members are gone. The
caller therefore keeps its operation lock and temporary resources throughout
cleanup, even when a process is stuck in uninterruptible kernel sleep.
"""
from __future__ import annotations

import locale
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class UpgradeInterrupted(Exception):
    """An operator requested interruption; never include command arguments."""


class ManagedProcessError(Exception):
    """A command left live descendants after its leader exited."""


@dataclass
class Cancellation:
    requested: bool = False


_cancellation: ContextVar[Cancellation | None] = ContextVar('upgrade_cancellation', default=None)
_POLL = 0.05
_TERM_GRACE = 2.0
_KILL_GRACE = 3.0


@contextmanager
def upgrade_signals():
    """Defer catchable signals to safe boundaries, including a TUI worker.

    The main thread owns signal handlers; copy its context to the worker.
    Nested scopes share the request and do not replace handlers.
    """
    existing = _cancellation.get()
    if existing is not None:
        yield existing
        return
    cancellation = Cancellation()
    token = _cancellation.set(cancellation)
    handlers = {}

    def request(signum, frame):
        cancellation.requested = True

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, request)
        yield cancellation
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        _cancellation.reset(token)


def check_cancelled() -> None:
    cancellation = _cancellation.get()
    if cancellation is not None and cancellation.requested:
        raise UpgradeInterrupted('Upgrade interruption requested')


def _group_active(pgid: int) -> bool:
    """Zombies cannot modify the installation and may await a system reaper."""
    try:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                # comm may contain spaces and parentheses; parse from the end.
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X', 'x'):
                return True
        return False
    except (OSError, UnicodeError, ValueError, IndexError):
        # Failure to verify is not evidence that the group is safe.
        return True


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _wait_group(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _group_active(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)
    return True


def _cleanup(pgid: int) -> None:
    """Bound signal escalation, not the time allowed for unsafe writers."""
    _signal_group(pgid, signal.SIGTERM)
    _wait_group(pgid, _TERM_GRACE)
    # Even if the scan found no live members, close the fork/exit race.
    _signal_group(pgid, signal.SIGKILL)
    if _wait_group(pgid, _KILL_GRACE):
        return
    try:
        from .services import write_audit
        write_audit(f'Upgrade process group {pgid} still unverified after SIGKILL; retaining operation lock')
    except Exception:
        pass
    while not _wait_group(pgid, _KILL_GRACE):
        _signal_group(pgid, signal.SIGKILL)


def run_managed(command, *, timeout, check=False, capture_output=False,
                text=False, encoding=None, errors=None, **kwargs):
    """Run a foreground command in a new session; never orphan managed writers.

    Capture output in private temporary files rather than pipes, so a child
    holding a pipe open cannot prevent interruption or the leader exit check.
    Only the caller's requested output is returned, never logged here.
    """
    if capture_output:
        if 'stdout' in kwargs or 'stderr' in kwargs:
            raise ValueError('capture_output conflicts with stdout/stderr')
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with upgrade_signals(), ExitStack() as stack:
        check_cancelled()
        captures = {}
        for name in ('stdout', 'stderr'):
            if kwargs.get(name) == subprocess.PIPE:
                captures[name] = stack.enter_context(tempfile.TemporaryFile())
                kwargs[name] = captures[name]
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        try:
            deadline = time.monotonic() + timeout
            while True:
                check_cancelled()
                # WNOWAIT reserves the leader PID/PGID until cleanup finishes.
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(_POLL)
            if _group_active(process.pid):
                raise ManagedProcessError('Upgrade command left active descendants')
            # Close the scan/fork race even on successful leader exit.
            _signal_group(process.pid, signal.SIGKILL)
            if not _wait_group(process.pid, _KILL_GRACE):
                raise ManagedProcessError('Upgrade command left active descendants')
            check_cancelled()
        except BaseException:
            _cleanup(process.pid)
            raise
        finally:
            # Never reap the leader before group cleanup: its PID could be
            # reused by an unrelated group while we are still sending signals.
            process.wait()
        output = {}
        for name, stream in captures.items():
            stream.seek(0)
            value = stream.read()
            if text or encoding or errors:
                value = value.decode(encoding or locale.getpreferredencoding(False), errors or 'strict')
                value = value.replace('\r\n', '\n').replace('\r', '\n')
            output[name] = value
        result = subprocess.CompletedProcess(command, process.returncode, **output)
        if check:
            result.check_returncode()
        return result
