"""Capture real curses backup and restore selectors without operational I/O.

Run in a terminal: python -m tests.tui.terminal_selector_capture /tmp/selectors.json en
"""
from __future__ import annotations

import curses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu


class CaptureScreen:
    def __init__(self, screen):
        self.screen = screen
        self.frames = []

    def __getattr__(self, name):
        return getattr(self.screen, name)

    def refresh(self):
        self.screen.refresh()
        h, _ = self.screen.getmaxyx()
        self.frames.append([self.screen.instr(y, 0).decode('utf-8', errors='replace').rstrip()
                            for y in range(h)])


def capture(screen, language):
    window = CaptureScreen(screen)
    menu = CursesMenu(window, Translator(language))
    menu._init_screen()
    result = []
    for h, w in ((34, 140), (16, 50)):
        curses.resizeterm(h, w)
        for selector, keys in (
            ('backup', (10, curses.KEY_DOWN, curses.KEY_END, 27, 27)),
            ('restore', (10, curses.KEY_DOWN, 27)),
        ):
            window.frames.clear()
            for key in reversed(keys):
                curses.ungetch(key)
            if selector == 'backup':
                assert menu._choose_backup_type() is None
            else:
                assert menu._choose_restore_scope(SimpleNamespace(path=Path('backup.full.tar.gz'))) is None
            result.append(dict(selector=selector, height=h, width=w, color=bool(menu.colors),
                               frames=window.frames.copy()))
    return result


if __name__ == '__main__':
    Path(sys.argv[1]).write_text(json.dumps(curses.wrapper(capture, sys.argv[2]), ensure_ascii=False),
                                 encoding='utf-8')
