"""In-memory curses screen for terminal-independent tests."""

from __future__ import annotations


class FakeScreen:
    def __init__(self, keys=(), dimensions=(24, 100)):
        self.keys = iter(keys)
        self.dimensions = dimensions
        self.writes = []
        self.current_frame = []
        self.frames = []

    def getmaxyx(self):
        return self.dimensions

    def addnstr(self, y, x, text, n, attr):
        entry = (y, x, text[:n], attr)
        self.writes.append(entry)
        self.current_frame.append(entry)

    def erase(self):
        self.current_frame = []

    def refresh(self):
        self.frames.append(list(self.current_frame))

    def getch(self):
        return next(self.keys)

    def keypad(self, enabled):
        pass

    def timeout(self, ms):
        self.wait_ms = ms
