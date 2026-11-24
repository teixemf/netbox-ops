"""Non-blocking journal preview and bounded live stream for the SSH TUI.

Static reads run in a worker. A live journalctl --follow subprocess is drained
non-blockingly; the UI must never wait for a new journal record to navigate.
"""
from __future__ import annotations

import concurrent.futures
from collections import deque
import os
import subprocess
from typing import Callable

from .services import ServiceError, SystemdServices
from .config import SERVICE_NAMES


class JournalPanel:
    """One selected systemd unit, a bounded buffer, and an optional follower."""

    def __init__(self, systemd: SystemdServices, count: int = 50,
                 popen: Callable = subprocess.Popen):
        self.systemd = systemd
        self.count = count
        self.popen = popen
        self.name: str | None = None
        self.live = False
        self.lines: deque[str] = deque(maxlen=500)
        self.sequence = 0
        self.error = ''
        self.loading = False
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                           thread_name_prefix='nbo-journal')
        self._future: concurrent.futures.Future | None = None
        self._future_name: str | None = None
        self._follow = None
        self._buffer = b''

    def select(self, name: str) -> None:
        if name not in SERVICE_NAMES:
            raise ServiceError(f'Unknown service: {name}')
        if name == self.name:
            return
        self._stop_follower()
        self.name = name
        self.lines.clear()
        self.sequence = 0
        self.error = ''
        if self.live:
            self._start_follower()
        else:
            self.refresh()

    def refresh(self) -> None:
        """Reload only when requested. Never block curses on journalctl."""
        if not self.name:
            return
        if self.live:
            self._stop_follower()
            self.lines.clear()
            self.sequence = 0
            self._start_follower()
            return
        self.loading = True
        self.error = ''
        self._future_name = self.name
        self._future = self._pool.submit(self.systemd.logs, self.name, self.count)

    def set_live(self, enabled: bool) -> bool:
        if not self.name:
            return False
        if enabled == self.live:
            return self.live
        self.live = enabled
        self._stop_follower()
        if self.live:
            self.lines.clear()
            self.sequence = 0
            self.error = ''
            self._start_follower()
        else:
            self.refresh()
        return self.live

    def _start_follower(self) -> None:
        if not self.name:
            return
        unit = self.systemd.config.units[self.name]
        try:
            self._follow = self.popen(
                ['journalctl', '--quiet', '--no-pager', '--output=short-iso',
                 '--unit', unit, '--lines', str(self.count), '--follow'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
            if self._follow.stdout is None:
                raise OSError('journalctl did not provide stdout')
            os.set_blocking(self._follow.stdout.fileno(), False)
            self._buffer = b''
        except OSError as exc:
            self.error = f'journalctl: {exc}'
            self._stop_follower()
            self.live = False

    def poll(self) -> None:
        if self._future is not None and self._future.done():
            task, name = self._future, self._future_name
            self._future = None
            self.loading = False
            try:
                lines = task.result()
            except Exception as exc:
                if name == self.name and not self.live:
                    self.error = str(exc)
            else:
                if name == self.name and not self.live:
                    self.lines = deque((self._sanitize(line) for line in lines), maxlen=500)
                    self.sequence = len(self.lines)
        if self._follow is None:
            return
        proc = self._follow
        if proc.poll() is not None:
            self.error = f'journalctl exited (code {proc.returncode})'
            self._stop_follower()
            self.live = False
            return
        # Bound processing to avoid journal bursts starving keyboard handling.
        for _ in range(4):
            try:
                chunk = os.read(proc.stdout.fileno(), 8192)
            except BlockingIOError:
                break
            except OSError as exc:
                self.error = str(exc)
                self._stop_follower()
                self.live = False
                break
            if not chunk:
                break
            self._buffer += chunk
            if len(self._buffer) > 256 * 1024:
                self._buffer = self._buffer[-128 * 1024:]
                # Drop any partial preceding record at the truncation point.
                self._buffer = self._buffer.partition(b'\n')[2]
            while b'\n' in self._buffer:
                record, self._buffer = self._buffer.split(b'\n', 1)
                self.lines.append(self._sanitize(record.decode('utf-8', 'replace')))
                self.sequence += 1

    @staticmethod
    def _sanitize(value: str) -> str:
        return ''.join(ch if ch.isprintable() else ' ' for ch in value)[:2048]

    def _stop_follower(self) -> None:
        proc = self._follow
        self._follow = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.10)
        if proc.stdout is not None:
            proc.stdout.close()

    def close(self) -> None:
        self._stop_follower()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class JournalViewport:
    """Follow the tail or anchor the visible end to a fixed journal sequence."""

    def __init__(self):
        self.end: int | None = None
        self.column = 0

    def reset(self) -> None:
        self.end = None
        self.column = 0

    def bounds(self, panel: JournalPanel, rows: int) -> tuple[int, int]:
        rows = max(1, rows)
        oldest = panel.sequence - len(panel.lines)
        earliest_end = min(panel.sequence, oldest + rows)
        end = panel.sequence if self.end is None else max(earliest_end, min(self.end, panel.sequence))
        if self.end is not None:
            self.end = end
        return max(0, end - oldest - rows), end - oldest

    def move(self, panel: JournalPanel, rows: int, amount: int) -> None:
        _, end = self.bounds(panel, rows)
        oldest = panel.sequence - len(panel.lines)
        target = oldest + end + amount
        if target >= panel.sequence:
            self.end = None
        else:
            self.end = max(min(panel.sequence, oldest + max(1, rows)), target)

    def horizontal(self, panel: JournalPanel, width: int, amount: int) -> None:
        maximum = max(0, max((len(line) for line in panel.lines), default=0) - max(1, width))
        self.column = min(maximum, max(0, self.column + amount))
