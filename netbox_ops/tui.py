"""Curses terminal interface; shared CLI backend for multi-service maintenance."""
from __future__ import annotations

import curses
import locale
import math
import os
import textwrap
import time
import datetime as dt
import queue
import concurrent.futures
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import __version__
from .backup_progress import (BackupProgress, BackupTask, BackupTaskEvent, batch_tasks,
                              create_tasks, restore_tasks)
from .config import CONFIG_PATH, SERVICE_NAMES, ConfigError, load_config
from .i18n import Translator
from .journal import JournalPanel, JournalViewport
from .menu import TUI_MENU_ACTIONS
from .services import (ServiceError, ServiceState, SystemdServices, ServiceVersionCache,
                       challenge, order_services, perform_service_batch)

if TYPE_CHECKING:
    from .backups import BackupSummary

SECTIONS = {0: 'menu_services', 1: 'menu_backups', 2: 'menu_updates',
            3: 'menu_maintenance'}
LOG_COUNTS = (10, 20, 50, 100, 200, 500)
ACTIONS = ('start', 'stop', 'restart', 'enable', 'disable')


def make_main_items(t: Translator) -> list[tuple[str, str, tuple[str, ...]]]:
    """One source of truth for the menu labels and CLI commands."""
    items = [(f'{number}  {t(key)}', 'action', (group, action))
             for number, group, action, key in TUI_MENU_ACTIONS]
    items.append((f'L  {t("tui_language")}', 'language', ()))
    items.append((f'Q  {t("tui_exit")}', 'exit', ()))
    return items


def move_action_focus(index: int, key: int, positions: Sequence[tuple[int, int]]) -> int:
    """Move across the displayed action rows without wrapping horizontally."""
    if not positions or not 0 <= index < len(positions):
        return index

    x, row = positions[index]
    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        direction = -1 if key == curses.KEY_LEFT else 1
        choices = [(i, px) for i, (px, py) in enumerate(positions)
                   if py == row and (px - x) * direction > 0]
        if choices:
            return min(choices, key=lambda item: abs(item[1] - x))[0]
    elif key in (curses.KEY_UP, curses.KEY_DOWN, ord('k'), ord('j')):
        direction = -1 if key in (curses.KEY_UP, ord('k')) else 1
        target_row = row + direction
        choices = [(i, px) for i, (px, py) in enumerate(positions)
                   if py == target_row]
        if choices:
            return min(choices, key=lambda item: (abs(item[1] - x), item[0]))[0]
    return index


def move_selection(index: int, key: int, count: int) -> int:
    if count < 1:
        return 0
    if key in (curses.KEY_UP, ord('k')):
        return (index - 1) % count
    if key in (curses.KEY_DOWN, ord('j')):
        return (index + 1) % count
    if key == curses.KEY_HOME:
        return 0
    if key == curses.KEY_END:
        return count - 1
    if key == curses.KEY_NPAGE:
        return min(count - 1, index + 6)
    if key == curses.KEY_PPAGE:
        return max(0, index - 6)
    return index


def safe_text(value: object) -> str:
    """Do not send control/escape sequences from journal or systemctl to curses."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def state_style(state: ServiceState) -> str:
    return 'success' if state.healthy else 'failure'


def action_enabled(action: str, state: ServiceState) -> bool:
    """Do not present nonsensical operations as available."""
    if state.loaded != 'loaded':
        return False
    if action == 'start':
        return state.active in ('inactive', 'failed')
    if action in ('stop', 'restart'):
        return state.active == 'active'
    if action == 'enable':
        return state.enabled in ('disabled', 'disabled-runtime')
    if action == 'disable':
        return state.enabled in ('enabled', 'enabled-runtime')
    return False


class _QuitTui(BaseException):
    """Unwind only from safe navigation; let screen finally blocks clean up."""


class CursesMenu:
    def __init__(self, stdscr, t: Translator, config_path: Path = CONFIG_PATH):
        self.stdscr = stdscr
        self.t = t
        self.config_path = config_path
        self.selection = 0
        self.colors: dict[str, int] = {}
        self._panels: list[tuple[int, int, int, int]] = []
        self.versions = ServiceVersionCache()
        self.cpu_samples: dict[str, tuple[int, int]] = {}
        self.cpu_display: dict[str, str] = {}
        self._service_steps: list[tuple[str, str, str]] = []
        self._backup_steps: list[tuple[str, str, str]] = []
        self._backup_step_indexes: dict[tuple[str, str], int] = {}

    def _init_screen(self) -> None:
        self.stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        # curses defaults to ~1000ms waiting after ESC for an arrow/function sequence.
        # Shorten locally rather than changing TERM or SSH settings.
        if hasattr(curses, 'set_escdelay'):
            curses.set_escdelay(45)
        if curses.has_colors() and 'NO_COLOR' not in os.environ:
            try:
                curses.start_color()
                curses.use_default_colors()
                # NetBox Dark: dark blue/slate surfaces, teal focus, clear semantic states.
                # xterm 256-colour approximations for the approved palette A;
                # never alter a terminal's global colour definitions.
                if curses.COLORS >= 256:
                    palette = {
                        'base': (255, 233),       # near #071B25, terminal canvas
                        'text': (255, 233),       # soft white
                        'panel': (252, 234),      # slightly brighter section surface
                        'title': (43, 233),       # teal (approx #35DCC5)
                        'section': (80, 233),     # restrained pale teal
                        'selected': (255, 23),   # white on deep teal
                        'success': (79, 233),     # soft mint green
                        'failure': (210, 233),    # salmon red
                        'warning': (215, 233),    # amber
                        'muted': (250, 233),      # slate grey
                        'disk_backup': (43, 233),
                        'disk_other': (67, 233),
                        'disk_free': (244, 233),
                    }
                else:
                    # Up to seven pairs on minimal 8-colour terminals.
                    palette = {
                        'base': (curses.COLOR_WHITE, curses.COLOR_BLACK),
                        'title': (curses.COLOR_CYAN, curses.COLOR_BLACK),
                        'selected': (curses.COLOR_WHITE, curses.COLOR_BLUE),
                        'success': (curses.COLOR_GREEN, curses.COLOR_BLACK),
                        'failure': (curses.COLOR_RED, curses.COLOR_BLACK),
                        'warning': (curses.COLOR_YELLOW, curses.COLOR_BLACK),
                        'muted': (curses.COLOR_WHITE, curses.COLOR_BLACK),
                    }
                # Panel-specific foreground pairs retain a consistent surface
                # colour when text is overlaid on the panel background.
                if curses.COLORS >= 256:
                    for role in ('text', 'title', 'section', 'success', 'failure',
                                 'warning', 'muted', 'disk_backup', 'disk_other',
                                 'disk_free'):
                        palette[role + '_panel'] = (palette[role][0], 234)
                for number, (name, pair) in enumerate(palette.items(), 1):
                    curses.init_pair(number, *pair)
                    self.colors[name] = curses.color_pair(number)
                if curses.COLORS < 256:
                    for alias, target in {
                        'text': 'base', 'panel': 'base', 'section': 'title',
                        'disk_backup': 'title', 'disk_other': 'muted',
                        'disk_free': 'muted',
                    }.items():
                        self.colors[alias] = self.colors[target]
                # Apply the dark theme to the entire canvas, not only text.
                if hasattr(self.stdscr, 'bkgd'):
                    self.stdscr.bkgd(' ', self.colors['base'])
            except curses.error:
                self.colors.clear()

    def _put(self, y: int, x: int, msg: object, style: str = '') -> None:
        h, w = self.stdscr.getmaxyx()
        if not (0 <= y < h and 0 <= x < w - 1):
            return
        text = safe_text(msg)
        limit = w - x - 1
        bold = style == 'title_bold'
        role = 'title' if bold else style or 'text'
        if role != 'selected' and any(x0 <= x < x1 and y0 <= y < y1
                                           for x0, y0, x1, y1 in self._panels):
            role = role + '_panel' if role + '_panel' in self.colors else role
        attr = self.colors.get(role, self.colors.get('text', 0))
        if style == 'selected':
            attr = attr or curses.A_REVERSE
        if bold:
            attr |= curses.A_BOLD
        try:
            self.stdscr.addnstr(y, x, text, limit, attr)
        except curses.error:
            pass

    def _frame(self, title: str, hint: str) -> tuple[int, int] | None:
        self.stdscr.erase()
        self._panels.clear()
        h, w = self.stdscr.getmaxyx()
        if h < 16 or w < 50:
            self._put(0, 0, self.t('tui_resize'), 'failure')
            self._put(min(h - 1, 2), 0, self.t('tui_resize_hint'))
            self.stdscr.refresh()
            return None
        self._put(0, 2, 'NETBOX-OPS', 'title')
        self._put(0, min(17, w - 12), f'v{__version__}  {self.t.language.upper()}', 'muted')
        self._put(1, 2, title, 'section')
        self._put(2, 2, '─' * (w - 5), 'muted')
        self._put(h - 2, 2, '─' * (w - 5), 'muted')
        if ' | ' in hint:
            x = 2
            for part in hint.split(' | '):
                key, _, action = part.partition(' ')
                self._put(h - 1, x, key, 'title')
                if action:
                    self._put(h - 1, x + len(key) + 1, action, 'muted')
                x += len(part) + 2
        else:
            self._put(h - 1, 2, hint, 'muted')
        return h, w

    def _paint(self, y: int, x: int, width: int, height: int = 1,
               style: str = 'panel') -> None:
        """Paint a text-cell panel background while respecting terminal bounds."""
        h, w = self.stdscr.getmaxyx()
        if width < 1 or height < 1 or x >= w - 1 or y >= h:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(w - 1, x + width), min(h - 2, y + height)
        for row in range(y0, y1):
            self._put(row, x0, ' ' * max(0, x1 - x0), style)
        self._panels.append((x0, y0, x1, y1))

    def _quit_hint(self, hint: str) -> str:
        return f'{self.t("tui_quit_key")} | {hint}'

    @staticmethod
    def _check_quit(key: int) -> None:
        if key in (ord('q'), ord('Q')):
            raise _QuitTui

    @staticmethod
    def _flush_input() -> None:
        # A queued key from an operation or result must not quit a later screen.
        try:
            curses.flushinp()
        except curses.error:
            pass

    def _wait_result_ack(self) -> None:
        """Keep the final progress panel visible until the operator dismisses it."""
        self.stdscr.timeout(-1)
        self._flush_input()
        while True:
            if self.stdscr.getch() in (10, 13, curses.KEY_ENTER, 27, 32):
                self._flush_input()
                return

    def _choose(self, title: str, entries: Sequence[tuple[str, str]],
                hint: str, index: int = 0, headings: dict[int, str] | None = None,
                allow_language_key: bool = False, main_menu: bool = False,
                numeric_shortcuts: bool = False, escape_clears_selection: bool = True,
                overview: Sequence[tuple[str, str]] = ()) -> int | None | str:
        """No initial highlight; Esc disarms selection before leaving submenu."""
        headings = headings or {}
        if not entries:
            return None
        lines: list[tuple[int | None, str, str]] = []
        select_to_row = []
        for i, (label, kind) in enumerate(entries):
            if i in headings:
                lines.append((None, self.t(headings[i]).strip(), 'section'))
            select_to_row.append(len(lines))
            lines.append((i, label, kind))
        index = max(0, min(index, len(entries) - 1))
        armed = False
        scroll = 0
        movement = (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END,
                    curses.KEY_NPAGE, curses.KEY_PPAGE, ord('j'), ord('k'))
        while True:
            frame = self._frame(title, self._quit_hint(hint))
            if frame:
                h, w = frame
                start = 3
                if overview:
                    limit = min(10, max(5, (h - 7) // 2))
                    shown = list(overview[:limit])
                    if len(overview) > limit:
                        shown[-1] = (self.t('upgrade_overview_more'), 'muted')
                    for y, (label, style) in enumerate(shown, start):
                        self._put(y, 3, safe_text(label)[:w - 6], style)
                    start += len(shown)
                    self._put(start, 3, '─' * (w - 7), 'muted')
                    start += 1
                rows_visible = max(1, h - start - 3)
                scroll = min(scroll, max(0, len(lines) - rows_visible))
                target = select_to_row[index] if armed else 0
                if target < scroll:
                    scroll = target
                elif target >= scroll + rows_visible:
                    scroll = target - rows_visible + 1
                for y, (item_idx, label, style) in enumerate(lines[scroll:scroll + rows_visible], start):
                    if armed and item_idx == index:
                        self._put(y, 3, '  ' + label + '  ', 'selected')
                    elif item_idx is None:
                        self._put(y, 3, label, 'section')
                    else:
                        self._put(y, 3, '  ' + label, style)
                if len(lines) > rows_visible and armed:
                    self._put(h - 3, 3, f'{index + 1}/{len(entries)}', 'muted')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key == 27:
                if armed and escape_clears_selection:
                    armed = False
                    continue
                if not main_menu:
                    return None
                continue
            if key == 3:
                return 'quit' if main_menu else None
            if key in (ord('q'), ord('Q')):
                if main_menu:
                    return 'quit'
                self._check_quit(key)
            if allow_language_key and key in (ord('l'), ord('L')):
                return 'language'
            if not frame:
                continue
            if numeric_shortcuts and ord('1') <= key <= ord('9'):
                choice = key - ord('1')
                if choice < len(entries):
                    return choice
            if key in (10, 13, curses.KEY_ENTER):
                if armed:
                    return index
            elif key in movement:
                if not armed:
                    armed = True
                    index = len(entries) - 1 if key in (curses.KEY_UP, ord('k'),
                                                       curses.KEY_END, curses.KEY_PPAGE) else 0
                else:
                    index = move_selection(index, key, len(entries))

    def _show_lines(self, title: str, lines: Sequence[tuple[str, str]],
                    hint: str | None = None, *, allow_quit: bool = False) -> bool:
        """Return True on Enter, False on cancellation; read-only views may quit."""
        hint = hint or self.t('tui_ack_hint')
        if allow_quit:
            hint = self._quit_hint(hint)
        else:
            self._flush_input()
        offset = 0
        while True:
            frame = self._frame(title, hint)
            if frame:
                h, w = frame
                width = max(10, w - 8)
                wrapped: list[tuple[str, str]] = []
                for line, style in lines:
                    parts = textwrap.wrap(safe_text(line), width=width,
                                          replace_whitespace=False, drop_whitespace=False,
                                          break_long_words=True) or ['']
                    wrapped.extend((part, style) for part in parts)
                rows_visible = h - 6
                offset = max(0, min(offset, max(0, len(wrapped) - rows_visible)))
                for y, (line, style) in enumerate(wrapped[offset:offset + rows_visible], 3):
                    self._put(y, 3, line, style)
                if len(wrapped) > rows_visible:
                    self._put(h - 3, 3, f'{offset + 1}-{min(offset + rows_visible, len(wrapped))}/{len(wrapped)}', 'muted')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if allow_quit:
                self._check_quit(key)
            if key in (27, 10, 13, curses.KEY_ENTER, 3):
                if not allow_quit:
                    self._flush_input()
                return key in (10, 13, curses.KEY_ENTER) and bool(frame)
            if not frame:
                continue
            if key in (curses.KEY_DOWN, ord('j')):
                offset += 1
            if key in (curses.KEY_UP, ord('k')):
                offset -= 1
            if key == curses.KEY_NPAGE:
                offset += max(1, h - 7)
            if key == curses.KEY_PPAGE:
                offset -= max(1, h - 7)
            offset = max(0, offset)

    def _update_cpu(self, states: Sequence[ServiceState]) -> None:
        """Current CPU utilisation, no history and no background collector."""
        now = time.monotonic_ns()
        for state in states:
            prev = self.cpu_samples.get(state.name)
            shown = '—'
            if state.cpu_usage_nsec is not None:
                if prev and now > prev[0] and state.cpu_usage_nsec >= prev[1]:
                    shown = f'{100 * (state.cpu_usage_nsec - prev[1]) / (now-prev[0]):.1f}%'
                self.cpu_samples[state.name] = (now, state.cpu_usage_nsec)
            self.cpu_display[state.name] = shown

    @staticmethod
    def _uptime(value: int | None) -> str:
        if value is None:
            return '—'
        days, rem = divmod(value, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        return f'{days}d {hours:02d}h {minutes:02d}m' if days else f'{hours}h {minutes:02d}m'

    def _read_number(self, initial: int) -> int | None:
        """Prompt for a bounded log count, without a shell or external UI package."""
        typed = ''
        while True:
            frame = self._frame(self.t('tui_log_count_title'), self.t('tui_confirm_hint'))
            if frame:
                self._put(4, 3, self.t('tui_log_count_info'))
                self._put(6, 3, f'{self.t("tui_log_count_prompt")}({initial}) {typed}', 'section')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (27, 3):
                return None
            if key in (10, 13, curses.KEY_ENTER):
                value = int(typed) if typed else initial
                if 1 <= value <= 500:
                    return value
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                typed = typed[:-1]
            elif 48 <= key <= 57 and len(typed) < 3:
                typed += chr(key)

    def _status(self) -> None:
        """Read-only compact status table; Enter opens read-only journal details."""
        try:
            config = load_config(self.config_path)
            systemd = SystemdServices(config)
            self.versions.config = config
            states = systemd.status(SERVICE_NAMES)
        except (ServiceError, ConfigError, OSError) as exc:
            self._show_lines(self.t('tui_error_title'),
                             [(self.t('service_error', error=exc), 'failure')])
            return
        index: int | None = None
        scroll = 0
        movement = (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END,
                    curses.KEY_NPAGE, curses.KEY_PPAGE, ord('j'), ord('k'))
        while True:
            frame = self._frame(self.t('tui_status_title'), self._quit_hint(self.t('tui_status_hint')))
            if frame:
                h, w = frame
                good = sum(s.healthy for s in states)
                self._put(3, 3, self.t('tui_summary', good=good, total=len(states)),
                          'success' if good == len(states) else 'failure')
                self._put(5, 4, self.t('tui_col_service'), 'muted')
                self._put(5, 25, self.t('tui_col_state'), 'muted')
                if w >= 62:
                    self._put(5, 43, self.t('tui_col_startup'), 'muted')
                if w >= 78:
                    self._put(5, 60, 'PID', 'muted')
                available = max(1, h - 10)
                scroll = max(0, min(scroll, max(0, len(states) - available)))
                if index is not None:
                    if index < scroll:
                        scroll = index
                    elif index >= scroll + available:
                        scroll = index - available + 1
                for i in range(scroll, min(scroll + available, len(states))):
                    y = 6 + i - scroll
                    state = states[i]
                    chosen = index == i
                    if chosen:
                        self._put(y, 3, ' ' * (w - 7), 'selected')
                    style = 'selected' if chosen else ''
                    self._put(y, 4, ('› ' if chosen else '  ') + state.name, style)
                    label = self.t('tui_active') if state.healthy else self.t('tui_inactive')
                    if not state.healthy and state.active not in ('inactive', 'failed'):
                        label = state.active
                    self._put(y, 25, label, style or state_style(state))
                    if w >= 62:
                        self._put(y, 43, self.t('tui_enabled') if state.enabled == 'enabled'
                                  else state.enabled, style or 'muted')
                    if w >= 78:
                        self._put(y, 60, state.main_pid if state.main_pid not in ('0', '') else '—', style)
                self.stdscr.refresh()
            key = self.stdscr.getch()
            self._check_quit(key)
            if key == 3:
                return
            if key == 27:
                if index is not None:
                    index = None
                else:
                    return
            elif key in (curses.KEY_F5, ord('r'), ord('R')):
                try:
                    states = systemd.status(SERVICE_NAMES)
                except ServiceError as exc:
                    self._show_lines(self.t('tui_error_title'), [(str(exc), 'failure')])
            elif frame and key in (10, 13, curses.KEY_ENTER) and index is not None:
                self._detail(systemd, states[index])
                try:
                    states = systemd.status(SERVICE_NAMES)
                except ServiceError as exc:
                    self._show_lines(self.t('tui_error_title'), [(str(exc), 'failure')])
            elif frame and key in movement:
                if index is None:
                    index = len(states)-1 if key in (curses.KEY_UP, ord('k'),
                                                     curses.KEY_END, curses.KEY_PPAGE) else 0
                else:
                    index = move_selection(index, key, len(states))

    def _read_logs(self, systemd: SystemdServices, name: str,
                   count: int) -> tuple[list[str], str]:
        try:
            return systemd.logs(name, count), ''
        except ServiceError as exc:
            return [], safe_text(exc)

    def _detail(self, systemd: SystemdServices, initial: ServiceState) -> None:
        """Read-only service details; version is queried once per app invocation."""
        state = initial
        self.versions.request(state.name)
        self.stdscr.timeout(200)
        last_refresh = time.monotonic()
        log_count = 20
        # The full-detail screen (not the management preview) shows a bounded
        # journal excerpt. Never reload it for each redraw/keyboard movement.
        journal, journal_error = self._read_logs(systemd, state.name, log_count)
        try:
            while True:
                if time.monotonic() - last_refresh >= 3:
                    try:
                        state = systemd.status([state.name])[0]
                        self._update_cpu([state])
                    except ServiceError:
                        pass
                    last_refresh = time.monotonic()
                frame = self._frame(f'{self.t("tui_detail_title")} / {state.name}',
                                    self._quit_hint(self.t('nbo_detail_hint_032')))
                if frame:
                    h, w = frame
                    x = 4
                    self._put(4, x, state.unit, 'title')
                    self._put(6, x, f'{self.t("tui_col_state")}: {state.active} ({state.substate})',state_style(state))
                    self._put(7, x, f'{self.t("tui_col_startup")}: {state.enabled}', 'muted')
                    self._put(8, x, f'PID: {state.main_pid}', 'muted')
                    self._put(10, x, f'CPU: {self.cpu_display.get(state.name, "—")}', 'section')
                    self._put(11, x, f'RAM: {self._human_size(state.memory_current)}', 'section')
                    ver = self.versions.get(state.name)
                    self._put(13, x, f'{self.t("nbo_service_version")}: {ver if ver is not None else "…"}', 'muted')
                    self._put(14, x, f'{self.t("nbo_service_uptime")}: {self._uptime(state.uptime_seconds)}', 'muted')
                    self._put(16, x, self.t('nbo_logs_on_demand', count=log_count), 'section')
                    if journal_error:
                        self._put(18,x,journal_error[:max(10,w-9)],'failure')
                    else:
                        visible = max(1, h-21)
                        for i, line in enumerate(journal[-visible:]):
                            self._put(18+i,x,line[:max(10,w-9)],'muted')
                        if not journal:
                            self._put(18,x,self.t('logs_empty'),'muted')
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                self._check_quit(key)
                if key in (27, 3):
                    return
                if key in (ord('l'), ord('L')):
                    rows = ([(journal_error, 'failure')] if journal_error else
                            [(line, 'muted') for line in journal] or
                            [(self.t('logs_empty'), 'muted')])
                    self.stdscr.timeout(-1)
                    self._show_lines(self.t('tui_logs_title', count=log_count), rows, allow_quit=not journal_error)
                    self.stdscr.timeout(200)
                    last_refresh = 0
                elif key in (ord('n'), ord('N')):
                    self.stdscr.timeout(-1)
                    value = self._read_number(log_count)
                    self.stdscr.timeout(200)
                    if value is not None:
                        log_count = value
                        journal,journal_error = self._read_logs(systemd,state.name,log_count)
                elif key in (curses.KEY_F5, ord('r'), ord('R')):
                    journal,journal_error = self._read_logs(systemd,state.name,log_count)
                    last_refresh = 0
        finally:
            self.stdscr.timeout(-1)

    @staticmethod
    def _confirmation_wrap(text: str, width: int) -> list[str]:
        return textwrap.wrap(safe_text(text), width=width, drop_whitespace=False,
                             break_on_hyphens=False) or ['']

    def _confirmation_panel(self, h: int, w: int, summary: Sequence[tuple[str, str]],
                            typed: str, *, token: str | None = None, invalid: bool = False,
                            target_names: Sequence[str] = (), offset: int = 0) -> tuple[int, int]:
        """Keep the exact instruction and field adjacent; scroll the review, not the input."""
        width = w - 7
        heading = self.t('tui_confirm_panel' if token is not None else 'tui_input_panel')
        instruction = self.t('tui_confirm_token', token=token) if token is not None else ''
        instructions = self._confirmation_wrap(instruction, width) if instruction else []
        errors = []
        if invalid:
            for key in ('nbo_confirm_failed_title', 'nbo_confirm_failed_message'):
                errors.extend(textwrap.wrap(self.t(key), width=width))
        # Reserve the literal instruction, input and complete inline error first.
        available = h - 2 - 4 - len(instructions) - 1 - len(errors)
        lines = [(part, style) for text, style in summary
                 for part in self._confirmation_wrap(text, width)]
        # Keep a concise warning visible while scrolling lengthy targets. If
        # it cannot fit completely, leave the complete wording in the review.
        pinned = []
        if summary and summary[0][1] == 'warning':
            warning = self._confirmation_wrap(summary[0][0], width)
            if len(warning) < available:
                pinned = [(part, 'warning') for part in warning]
                lines = lines[len(warning):]
                available -= len(pinned)
            elif available >= 2:
                pinned = [(self.t('tui_confirm_review_warning'), 'warning')]
                available -= 1
        targets = []
        for index, name in enumerate(target_names, 1):
            targets.extend((part, 'muted') for part in
                           self._confirmation_wrap(f'{index}. {name}', width))
        if target_names:
            lines.append((f'{self.t("nbo_operation_targets")} ({len(target_names)}):', 'section'))
            lines.extend(targets)
        visible = min(len(lines), max(1, available))
        scroll = len(lines) > visible
        if scroll and visible > 1:
            visible -= 1
        limit = max(0, len(lines) - visible)
        offset = min(max(0, offset), limit)
        gap = 1 if available > len(lines) and lines else 0
        height = (1 + len(pinned) + visible + int(scroll and available > 1)
                  + gap + len(instructions) + 1 + len(errors))
        self._paint(3, 2, w - 4, height)
        self._put(3, 3, heading, 'section')
        y = 4
        for line, style in pinned + lines[offset:offset + visible]:
            self._put(y, 3, line, style)
            y += 1
        if scroll and available > 1:
            self._put(y, 3, f'↑↓ {offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}', 'section')
            y += 1
        y += gap

        # Split the translated instruction into styled spans without changing
        # token spelling, even when a long token wraps onto multiple lines.
        token_start = instruction.find(token) if token else -1
        position = 0
        for line in instructions:
            if token_start < 0:
                self._put(y, 3, line)
            else:
                left = max(0, min(len(line), token_start - position))
                right = max(left, min(len(line), token_start + len(token) - position))
                if left:
                    self._put(y, 3, line[:left])
                if right > left:
                    self._put(y, 3 + left, line[left:right], 'title_bold')
                if right < len(line):
                    self._put(y, 3 + right, line[right:])
            position += len(line)
            y += 1
        prefix = self.t('tui_confirm_prompt') if token is not None else '> '
        value = prefix + typed[-max(1, width - len(prefix) - 1):]
        self._put(y, 3, value)
        self._put(y, 3 + len(value), '▏', 'title')
        for row, line in enumerate(errors, y + 1):
            self._put(row, 3, line, 'failure')
        return offset, visible

    @staticmethod
    def _confirmation_scroll(key: int, offset: int, visible: int) -> int:
        if key in (curses.KEY_UP, curses.KEY_PPAGE):
            return max(0, offset - (visible if key == curses.KEY_PPAGE else 1))
        return offset + (visible if key == curses.KEY_NPAGE else 1)

    def _confirm_action(self, action: str, names: Sequence[str],
                        skipped: Sequence[str] = ()) -> bool:
        """Use the shared exact challenge, with service-specific review information."""
        token = challenge(action, names)
        warning = (self.t('tui_startup_only') if action in ('enable', 'disable')
                   else self.t('tui_action_warning'))
        summary = [(warning, 'muted' if action in ('enable', 'disable') else 'warning'),
                   (self.t('action_target', action=self.t('action_' + action),
                           names=', '.join(names)), '')]
        if skipped:
            summary.append((self.t('nbo_skipped', names=', '.join(skipped)), 'muted'))
        return self._confirm_token(self.t('tui_confirm_title'), '', token, summary=summary)

    def _confirm(self, names: Sequence[str]) -> bool:
        return self._confirm_action('restart', names)

    def _draw_progress(self, action: str, completed: int, total: int,
                       name: str, stage: str, detail: str,
                       *, finished: bool = False, summary: str = '',
                       failed: bool = False) -> None:
        """Completed services / total, not a fabricated estimate of systemctl duration."""
        frame = self._frame(self.t('tui_progress_title'),
                            self.t('nbo_progress_result_hint') if finished else self.t('tui_quit_blocked'))
        if not frame:
            return
        h, w = frame
        self._put(4, 3, self.t('action_target', action=self.t('action_' + action), names=name))
        self._put(6, 3, self.t('tui_progress_count', done=completed, total=total),
                  'failure' if stage == 'error' or failed else 'title')
        length = max(10, min(60, w - 12))
        filled = int(length * completed / total) if total else 0
        self._put(8, 3, '[' + '█' * filled + '░' * (length-filled) + '] %3d%%' %
                  (100 * completed // total if total else 0),
                  'failure' if stage == 'error' or failed else 'success')
        status = 'tui_progress_running' if stage == 'running' else (
            'tui_progress_failed' if stage == 'error' else 'tui_progress_done')
        self._put(10, 3, self.t(status, name=name),
                  'failure' if stage == 'error' else 'section')
        if detail:
            self._put(11, 3, detail[:max(10, w-8)], 'failure' if stage == 'error' else 'muted')
        # Keep a live task list below the progress bar, including completed
        # services, current command and failures. No prediction of elapsed time.
        if not hasattr(self, '_service_steps'):
            self._service_steps = []
        item = (name, stage, detail)
        old = next((i for i, step in enumerate(self._service_steps) if step[0] == name), None)
        if old is None:
            self._service_steps.append(item)
        else:
            self._service_steps[old] = item
        self._put(13, 3, self.t('nbo_task_header'), 'section')
        visible = self._service_steps[-max(0, h - 19):] if h >= 19 else []
        for i, (service, state, info) in enumerate(visible):
            mark = 'OK' if state == 'done' else ('!!' if state == 'error' else '..')
            self._put(14+i, 4, f'{mark} {self.t("action_"+action)} {service}: {info}'[:max(8,w-9)],
                      'success' if state == 'done' else ('failure' if state == 'error' else 'muted'))
        if finished:
            y = min(h - 4, 15 + len(visible))
            self._put(y, 3, summary[:max(10, w - 8)],
                      'failure' if failed else 'success')
        self.stdscr.refresh()

    def _execute_batch(self, systemd: SystemdServices,
                       action: str, names: Sequence[str]) -> None:
        ordered = order_services(action, names)
        self._service_steps = []
        self._draw_progress(action, 0, len(ordered), ordered[0], 'running', '')
        completed: list[tuple[str, str]] = []
        failure: str | None = None
        try:
            completed, failure = perform_service_batch(
                systemd, action, ordered,
                on_progress=lambda stage, done, total, name, info:
                self._draw_progress(action, done, total, name, stage, info))
        except (ServiceError, OSError) as exc:
            failure = safe_text(exc)
            name = self._service_steps[-1][0] if self._service_steps else ordered[0]
            self._draw_progress(action, len(completed), len(ordered), name,
                                'error', failure)
        # Do not replace this screen with a second, redundant results page.
        done = len(completed)
        status = self._service_steps[-1][1] if self._service_steps else 'running'
        last = self._service_steps[-1][0] if self._service_steps else ordered[0]
        if failure:
            summary = self.t('tui_batch_failed', error=failure)
        else:
            summary = self.t('tui_batch_complete', count=done)
        self._draw_progress(action, done, len(ordered), last, status,
                            failure or '', finished=True, summary=summary,
                            failed=bool(failure))
        self._wait_result_ack()

    @staticmethod
    def _human_size(value: int | None) -> str:
        if value is None or value < 0:
            return '—'
        size = float(value)
        for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
            if size < 1024 or unit == 'TiB':
                return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024
        return '—'

    @staticmethod
    def _braille_donut(backups: int, other: int, free: int, total: int,
                       cols: int = 18, rows: int = 9) -> list[list[tuple[str, str]]]:
        """Dot-resolution donut (2×4 Braille pixels/cell), preserving circle ratio.

        Colour is per Braille character, selected by the majority of filled
        dots in each segment. This avoids horizontally stretched ASCII circles.
        """
        result: list[list[tuple[str, str]]] = []
        if total <= 0:
            return result
        f_backup = min(1.0, max(0, backups) / total)
        f_other = min(1.0 - f_backup, max(0, other) / total)
        grid_w, grid_h = 2 * cols, 4 * rows
        cx, cy = (grid_w - 1) / 2, (grid_h - 1) / 2
        # The dot grid, unlike the character grid, is approximately square
        # in a monospace terminal (2 horizontal dots, 4 vertical dots/cell).
        radius_outer = min(grid_w, grid_h) * .47
        radius_inner = radius_outer * .61
        bit_map = ((0, 1, 2, 6), (3, 4, 5, 7))
        for row in range(rows):
            rendered = []
            for col in range(cols):
                bits = 0
                counts = {'disk_backup': 0, 'disk_other': 0, 'disk_free': 0}
                for ix in range(2):
                    for iy in range(4):
                        dx = col * 2 + ix - cx
                        dy = row * 4 + iy - cy
                        r = math.hypot(dx, dy)
                        if not radius_inner <= r <= radius_outer:
                            continue
                        bits |= 1 << bit_map[ix][iy]
                        angle = (math.atan2(dx, -dy) + 2 * math.pi) % (2 * math.pi)
                        fraction = angle / (2 * math.pi)
                        tone = ('disk_backup' if fraction < f_backup else
                                'disk_other' if fraction < f_backup + f_other else
                                'disk_free')
                        counts[tone] += 1
                tone = max(counts, key=counts.get)
                rendered.append((chr(0x2800 + bits) if bits else ' ', tone))
            result.append(rendered)
        return result

    def _draw_storage_ring(self, x: int, y: int, disk: tuple[int,int,int,int,int]) -> None:
        """Smooth proportional Braille donut: backups / other disk use / free."""
        backups, other, free, total, _ = disk
        if total <= 0:
            return
        for row, cells in enumerate(self._braille_donut(backups, other, free, total)):
            for col, (glyph, style) in enumerate(cells):
                if glyph != ' ':
                    self._put(y + row, x + col, glyph, style)
        self._put(y + 4, x + 6, f'{100 * backups / total:.1f}%'[:7], 'title')

    def _draw_platform_status(self, x: int, y: int, width: int,
                              states: Sequence[ServiceState], metrics: dict[str, str],
                              error: str) -> int:
        """Render a content-sized table; return the first row below the table."""
        headers = (self.t('tui_col_service'), self.t('tui_col_state'),
                   self.t('nbo_service_version').upper(), 'CPU', 'RAM')
        indexed = {state.name: state for state in states}
        rows = []
        for name in SERVICE_NAMES:
            state = indexed.get(name)
            label, style, mark = '—', 'muted', '?'
            version, cpu, ram = '—', '—', '—'
            if state is not None and not error:
                if state.healthy:
                    label, style, mark = self.t('tui_active'), 'success', '●'
                elif state.loaded != 'loaded':
                    label, style, mark = '—', 'warning', '?'
                elif state.active == 'failed':
                    label, style, mark = self.t('nbo_dashboard_failed'), 'failure', '○'
                elif state.active == 'inactive':
                    label, style, mark = self.t('tui_inactive'), 'failure', '○'
                elif state.active in ('activating', 'deactivating', 'reloading', 'refreshing'):
                    label, style, mark = self.t('nbo_dashboard_transition'), 'warning', '○'
                cached = self.versions.get(name)
                version = cached if cached is not None else '…'
                cpu = metrics.get(name, '—')
                ram = self._human_size(state.memory_current)
            rows.append(((f'{mark} {name}', label, safe_text(version), cpu, ram), style))

        def fit(value: str, size: int) -> str:
            return value if len(value) <= size else value[:size - 1] + '…'

        widths = [max(len(header), *(len(values[i]) for values, _ in rows))
                  for i, header in enumerate(headers)]
        widths[2] = min(widths[2], 16)
        # Prefer all five columns. Drop version before live metrics and never
        # squeeze numeric values into unreadable fragments.
        candidates = ((0, 1, 2, 3, 4), (0, 1, 3, 4), (0, 1, 3), (0, 1))
        columns, gap = candidates[-1], 1
        for candidate in candidates:
            total = sum(widths[i] for i in candidate)
            if total + 3 * (len(candidate) - 1) <= width:
                columns, gap = candidate, 3
                break
            if total + len(candidate) - 1 <= width:
                columns, gap = candidate, 1
                break
        positions = []
        offset = x
        for i in columns:
            size = min(widths[i], max(0, x + width - offset))
            if size < 1:
                break
            positions.append((i, offset, size))
            heading = fit(headers[i], size)
            self._put(y, offset, heading.rjust(size) if i in (3, 4) else heading, 'muted')
            offset += size + gap
        if error:
            self._put(y + 1, x, fit(error, width), 'failure')
        else:
            for row, (values, style) in enumerate(rows, y + 1):
                for i, col, size in positions:
                    value = fit(values[i], size)
                    if i in (3, 4):
                        value = value.rjust(size)
                    self._put(row, col, value, style if i in (0, 1) else 'muted')
        return y + 1 + len(rows)

    def _dashboard_storage_legend(self, storage, disk) -> list[tuple[str, str]]:
        occupied, free, count = storage
        return [
            (f'{self.t("nbo_storage_used")}: {self._human_size(occupied)}', 'disk_backup'),
            (f'{self.t("nbo_storage_other")}: {self._human_size(disk[1])}', 'disk_other'),
            (f'{self.t("nbo_storage_free")}: {self._human_size(free)}', 'disk_free'),
            (f'{self.t("nbo_storage_count")}: {count}', 'muted'),
        ]

    _DASHBOARD_LEGEND_OFFSET = 20

    def _dashboard_storage_height(self, width, available, storage, disk, error) -> int:
        if available <= 0:
            return 0
        if error:
            return min(2, available)
        legend = self._dashboard_storage_legend(storage, disk)
        if (disk[3] and available >= 10 and
                width >= self._DASHBOARD_LEGEND_OFFSET + max(len(label) + 2 for label, _ in legend)):
            return 10
        return min(4, available)

    def _draw_dashboard_storage(self, x: int, y: int, width: int, bottom: int,
                                storage: tuple[int, int, int],
                                disk: tuple[int, int, int, int, int],
                                error: str) -> int:
        """Keep storage within its panel and return the next available row."""
        height = self._dashboard_storage_height(width, bottom - y + 1, storage, disk, error)
        if not height:
            return y
        self._put(y, x, self.t('nbo_storage_label')[:width], 'section')
        if height == 1:
            return y + 1
        if error:
            self._put(y + 1, x, error[:width], 'failure')
            return y + 2
        legend = self._dashboard_storage_legend(storage, disk)
        if height == 10:
            self._draw_storage_ring(x, y + 1, disk)
            for row, (label, style) in enumerate(legend, y + 1):
                self._put(row, x + self._DASHBOARD_LEGEND_OFFSET,
                          f'● {label}' if style != 'muted' else label, style)
        elif height == 4:
            for row, index in enumerate((0, 2, 3), y + 1):
                self._put(row, x, legend[index][0][:width], 'muted')
        else:
            summary = f'{legend[3][0]} / {self._human_size(storage[0])}'
            self._put(y + 1, x, summary[:width], 'muted')
            if height == 3:
                self._put(y + 2, x, legend[2][0][:width], 'muted')
        return y + height

    def _latest_backup_lines(self, latest: BackupSummary | None, width: int) -> list[tuple[str, str]]:
        """Unambiguous UTC provenance and complete, sanitized filename."""
        rows = [(self.t('nbo_latest_heading'), 'section')]
        if latest is None:
            return rows + [(self.t('nbo_latest_none'), 'muted')]
        stamp = latest.timestamp.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        provenance = self.t('nbo_latest_source_' + latest.source)
        tag = self.t({'manual': 'nbo_manual_short', 'pre_restore': 'nbo_latest_pre_restore',
                      'unknown': 'nbo_latest_unknown_tag'}[latest.purpose])
        labels = [self.t('nbo_latest_' + field) for field in ('date', 'name', 'type', 'tag')]
        size = max(map(len, labels))
        values = [f'{stamp} ({provenance})', latest.path.name.encode('unicode_escape').decode('ascii'),
                  self.t('nbo_kind_' + latest.kind), tag]
        for label, value in zip(labels, values):
            prefix = f'{label:<{size}} : '
            # Keep labels aligned and never lose characters of long filenames.
            available = max(1, width - len(prefix))
            chunks = [value[i:i + available] for i in range(0, len(value), available)] or ['']
            rows.extend((f'{prefix if i == 0 else " " * len(prefix)}{chunk}', 'muted')
                        for i, chunk in enumerate(chunks))
        return rows

    def _draw_main_dashboard(self, h, w, items, cursor, states, metrics, service_error,
                             storage, disk, latest, storage_error) -> None:
        """Three content-height panels; compact terminals expose full details via B."""
        split = max(22, w * 35 // 100)
        x = split + 2
        width = w - split - 5
        available = h - 5
        platform_height = 7  # heading + table header + five service rows
        gap = 1 if available >= 14 else 0
        full = self._latest_backup_lines(latest, width)
        compact = [(self.t('nbo_latest_heading'), 'section'),
                   (self.t('nbo_latest_details_hint'), 'muted')]
        latest_rows = [] if storage_error else full
        if len(latest_rows) + 2 > available - platform_height - gap:
            latest_rows = compact
        # Make room for the platform note only when all chosen storage rows fit.
        note = textwrap.wrap(self.t('nbo_live_note'), width=width)
        note_height = len(note) + 2
        if available - platform_height - gap - len(latest_rows) - 2 >= note_height:
            platform_height += note_height
        else:
            note = []
        storage_y = 3 + platform_height + gap
        storage_space = h - 2 - storage_y
        usage_space = storage_space - len(latest_rows)
        latest_gap = 1 if latest_rows and usage_space >= 5 else 0
        usage_height = self._dashboard_storage_height(
            width, usage_space - latest_gap, storage, disk, storage_error)
        storage_height = usage_height + latest_gap + len(latest_rows)
        latest_x, latest_width = x, width
        latest_y = storage_y + usage_height + latest_gap
        # The donut is ten rows including its heading; the four legend rows
        # finish at y+4. Reserve one empty row before the adjacent details.
        legend_width = width - self._DASHBOARD_LEGEND_OFFSET
        if (not storage_error and legend_width >= 40 and
                self._dashboard_storage_height(width, storage_space, storage, disk, '') == 10):
            adjacent = self._latest_backup_lines(latest, legend_width)
            adjacent_height = max(10, 6 + len(adjacent))
            if adjacent_height <= storage_space:
                latest_rows = adjacent
                latest_x = x + self._DASHBOARD_LEGEND_OFFSET
                latest_width = legend_width
                latest_y = storage_y + 6
                usage_height = 10
                storage_height = adjacent_height
        # Paint all three panels before rendering their content.
        menu_height = max(1 + len(items) + (1 if available >= 10 else 0),
                          platform_height + gap + storage_height)
        self._paint(3, 2, split - 3, menu_height)
        self._paint(3, split + 1, w - split - 3, platform_height)
        self._paint(storage_y, split + 1, w - split - 3, storage_height)
        self._put(3, 3, self.t('nbo_menu_label'), 'section')
        for idx, (label, _, _) in enumerate(items):
            row = 4 + idx + (1 if available >= 10 and idx >= 3 else 0)
            if cursor == idx:
                self._put(row, 3, ' ' * (split - 5), 'selected')
            self._put(row, 4, label[:split - 7], 'selected' if cursor == idx else '')
        self._put(3, x, self.t('nbo_dashboard_label')[:width], 'section')
        table_end = self._draw_platform_status(x, 4, width, states, metrics, service_error)
        for row, line in enumerate(note, table_end + 1):
            self._put(row, x, line, 'muted')
        self._draw_dashboard_storage(
            x, storage_y, width, storage_y + usage_height - 1, storage, disk, storage_error)
        for row, (line, style) in enumerate(latest_rows, latest_y):
            if row < h - 2:
                self._put(row, latest_x, line[:latest_width], style)

    def _main(self) -> int:
        """Main screen: compact menu and live service/storage dashboard."""
        from .backups import BackupError, BackupManager
        names = list(SERVICE_NAMES)
        states: list[ServiceState] = []
        metrics: dict[str, str] = {}
        service_error = ''
        storage = (0, 0, 0)
        disk = (0, 0, 0, 0, 0)
        latest: BackupSummary | None = None
        storage_error = ''
        last_updated = 0.0
        last_storage = 0.0
        cursor: int | None = None
        items = make_main_items(self.t)
        try:
            config = load_config(self.config_path)
            systemd = SystemdServices(config)
            manager = BackupManager(config)
        except (ConfigError, OSError) as exc:
            self._show_lines(self.t('tui_error_title'), [(str(exc), 'failure')])
            return 1
        self.versions.config = config
        for name in names:
            self.versions.request(name)
        self.stdscr.timeout(250)
        try:
            while True:
                now = time.monotonic()
                if now - last_updated >= 3 or not last_updated:
                    last_updated = now
                    try:
                        states = systemd.status(names)
                        service_error = ''
                        self._update_cpu(states)
                        metrics.update(self.cpu_display)
                    except (ServiceError, OSError) as exc:
                        service_error = safe_text(exc)[:90]
                if now - last_storage >= 15 or not last_storage:
                    last_storage = now
                    try:
                        disk, latest = manager.dashboard_storage()
                        storage = (disk[0], disk[2], disk[4])
                        storage_error = ''
                    except (BackupError, OSError, ValueError, OverflowError) as exc:
                        disk = (0, 0, 0, 0, 0)
                        storage = (0, 0, 0)
                        latest = None
                        storage_error = safe_text(exc)[:90] or self.t('nbo_latest_unavailable')
                items = make_main_items(self.t)
                hint = self.t('nbo_main_hint' if self.stdscr.getmaxyx()[1] >= 85 else 'nbo_main_hint_compact')
                frame = self._frame(self.t('tui_main_title'), hint)
                if frame:
                    h, w = frame
                    self._draw_main_dashboard(h, w, items, cursor, states, metrics, service_error,
                                              storage, disk, latest, storage_error)
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                if key == -1:
                    continue
                if key in (ord('q'), ord('Q'), 3):
                    return 0
                if key == 27:
                    cursor = None  # Esc never leaves the main screen.
                    continue
                if key in (ord('l'), ord('L')):
                    self._language()
                    cursor = None
                    continue
                if key in (ord('b'), ord('B')):
                    self.stdscr.timeout(-1)
                    try:
                        lines = ([(storage_error, 'failure')] if storage_error
                                 else self._latest_backup_lines(latest, 4096))
                        self._show_lines(self.t('nbo_latest_heading'), lines, allow_quit=True)
                    finally:
                        self.stdscr.timeout(250)
                    last_storage = 0.0
                    continue
                if key in (curses.KEY_F5, ord('r'), ord('R')):
                    last_updated = 0.0
                    last_storage = 0.0
                    continue
                if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END,
                           curses.KEY_NPAGE, curses.KEY_PPAGE, ord('j'), ord('k')):
                    if cursor is None:
                        cursor = len(items)-1 if key in (curses.KEY_UP, ord('k')) else 0
                    else:
                        cursor = move_selection(cursor, key, len(items))
                elif (ord('1') <= key < ord('1') + len(TUI_MENU_ACTIONS) or
                      (key in (10, 13, curses.KEY_ENTER) and cursor is not None)):
                    if ord('1') <= key < ord('1') + len(TUI_MENU_ACTIONS):
                        cursor = key - ord('1')
                    _, kind, data = items[cursor]
                    if kind == 'exit':
                        return 0
                    if kind == 'language':
                        self._language()
                    elif data == ('services', 'manage'):
                        self.stdscr.timeout(-1)
                        self._manage()
                    elif data == ('self-upgrade', ''):
                        self.stdscr.timeout(-1)
                        result = self._self_upgrade()
                        if result is not None:
                            return result
                    elif data == ('upgrade', 'menu'):
                        self.stdscr.timeout(-1)
                        self._upgrade_menu()
                    elif data == ('backup', 'manage'):
                        self.stdscr.timeout(-1)
                        self._backups()
                    else:
                        self.stdscr.timeout(-1)
                        self._show_lines(self.t('tui_not_implemented_title'),
                                         [(self.t('not_implemented', command=' '.join(data)), 'muted')])
                    self.stdscr.timeout(250)
                    last_updated = 0
                    last_storage = 0
                    cursor = None
        finally:
            self.stdscr.timeout(-1)

    def _upgrade_menu(self) -> None:
        """Open the unified version-target management screen."""
        from .upgrade_dashboard import UpgradeDashboard

        UpgradeDashboard(self).run()

    def _upgrade_input(self, title: str, prompt: str, *, initial: str = '',
                       secret: bool = False) -> str | None:
        typed = initial
        offset = 0
        visible = 1
        while True:
            frame = self._frame(title, self.t('tui_confirm_hint'))
            if frame:
                value = '*' * len(typed) if secret else typed
                offset, visible = self._confirmation_panel(
                    *frame, [(prompt, '')], value, offset=offset)
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (27, 3):
                return None
            if not frame:
                continue
            if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
                offset = self._confirmation_scroll(key, offset, visible)
            elif key in (10, 13, curses.KEY_ENTER):
                return typed
            elif key in (8, 127, curses.KEY_BACKSPACE):
                typed = typed[:-1]
            elif 32 <= key <= 126 and len(typed) < 2048:
                typed += chr(key)

    def _execute_reviewed_upgrade(self, title, cfg, operation, plan) -> bool:
        """Use only the exact reviewed plan; return True after an execution attempt."""
        from .upgrade_ui import execute_plan, summary_lines

        if not plan.can_execute:
            self._show_lines(title, [(self.t('upgrade_blocked'), 'failure')])
            return False
        if not self._show_lines(title, [(line, '') for line in summary_lines(plan, self.t, details=True)],
                                self.t('upgrade_review_hint'), allow_quit=True):
            return False
        url = self._upgrade_input(title, self.t('upgrade_url_prompt'))
        if not url:
            return False
        auth = self._upgrade_input(title, self.t('upgrade_auth_prompt'), secret=True)
        if auth is None:
            return False
        prompt = self.t('upgrade_confirm')
        if not self._confirm_token(title, prompt, 'UPGRADE', summary=[(prompt, 'warning')]):
            return False
        unknowns = plan.requires_strong_confirmation
        prompt = self.t('upgrade_confirm_unknowns')
        if unknowns and not self._confirm_token(title, prompt, 'UNCERTAIN', summary=[(prompt, 'warning')]):
            return False
        result = self._upgrade_job(title, lambda report: execute_plan(
            cfg, operation, plan, fingerprint=plan.fingerprint,
            acknowledge_unknowns=unknowns, health_url=url, authorization=auth or None,
            progress=report,
        ))
        if result is not None:
            self._show_lines(title, [(self.t('upgrade_success', recovery=result.directory), 'success')])
        return True

    def _review_plugin_pins(self, title, inventory) -> bool:
        """Select, display and explicitly approve exact pin corrections."""
        from .plugin_pin_ui import build_pin_review, pin_review_lines, apply_pin_review
        from .plugin_inventory import PluginInventoryError, normalize_name

        available = tuple(normalize_name(p.package) for p in inventory.proposals)
        if not available:
            review = build_pin_review(inventory, ())
            self._show_lines(title, [(line, '') for line in pin_review_lines(review, self.t)],
                             allow_quit=True)
            return False
        selected = set()
        while True:
            entries = [(self.t('upgrade_select_all'), ''), (self.t('upgrade_clear_all'), ''),
                       (self.t('upgrade_pin_review', count=len(selected)), '')]
            entries.extend((f'[{"x" if name in selected else " "}] {name}=={proposal.version}', '')
                           for name, proposal in zip(available, inventory.proposals))
            choice = self._choose(title, entries, self.t('tui_back_hint'))
            if not isinstance(choice, int):
                return False
            if choice == 0:
                selected = set(available)
            elif choice == 1:
                selected.clear()
            elif choice == 2:
                if not selected:
                    continue
                review = build_pin_review(inventory, tuple(name for name in available if name in selected))
                if self._show_lines(title, [(line, '') for line in pin_review_lines(review, self.t)],
                                    self.t('upgrade_review_hint'), allow_quit=True) is False:
                    continue
                if not self._confirm_token(title, self.t('upgrade_pin_confirm'), 'PIN',
                                           target_names=review.selected):
                    return False
                try:
                    apply_pin_review(review, review.fingerprint)
                except PluginInventoryError as exc:
                    self._show_lines(title, [(self.t('upgrade_error', message=str(exc)), 'failure')])
                    return False
                self._show_lines(title, [(self.t('upgrade_pins_applied'), 'success')])
                return True
            else:
                name = available[choice - 3]
                if name in selected:
                    selected.remove(name)
                else:
                    selected.add(name)

    def _upgrade_job(self, title: str, worker, *, error_key: str = 'upgrade_discovery_error'):
        """Keep curses in the UI thread and show actual backend stage progress."""
        from contextvars import copy_context
        from .upgrade_execution import UpgradeExecutionError
        from .upgrade_process import UpgradeInterrupted, upgrade_signals
        events = queue.SimpleQueue()
        phase, done, total = 'preflight', 0, 0
        tick = 0
        self.stdscr.timeout(150)
        try:
            with upgrade_signals(), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(copy_context().run, worker,
                                     lambda phase, done, total: events.put((phase, done, total)))
                while not future.done():
                    while not events.empty():
                        phase, done, total = events.get_nowait()
                    frame = self._frame(title, self.t('tui_quit_blocked'))
                    if frame:
                        self._put(4, 3, self.t('upgrade_progress', phase=self.t('upgrade_phase_' + phase),
                                              done=done, total=total))
                        self._put(6, 3, ('|', '/', '-', '/')[tick % 4])
                        self.stdscr.refresh()
                    tick += 1
                    self.stdscr.getch()
                return future.result()
        except UpgradeInterrupted:
            error = self.t('upgrade_cancelled')
        except UpgradeExecutionError as exc:
            error = self.t('upgrade_error', message=str(exc))
        except Exception:
            error = self.t(error_key)
        finally:
            self.stdscr.timeout(-1)
            self._flush_input()
        self._show_lines(title, [(error, 'failure')])
        return None

    def _self_upgrade(self) -> int | None:
        """Keep selection, compatibility, review and recovery status together."""
        from .self_upgrade_releases import Version, discover_releases
        from .self_upgrade_ui import (
            HANDOFF_EXIT, catalogue_lines, changelog_lines, compatibility_catalogue,
            decision_lines, execute_review, release_status, review_lines, review_upgrade,
            tui_recovery_state,
        )

        title = self.t('menu_13')
        hint = self.t('upgrade_overview_hint')
        try:
            installed = Version.parse(f'v{__version__}')
        except ValueError:
            self._show_lines(title, [(self.t('self_upgrade_invalid_version'), 'failure')])
            return None

        def discover():
            releases = sorted(discover_releases(), key=lambda item: item.version, reverse=True)
            catalogue = compatibility_catalogue(releases, installed)
            # Resolve catalogue decisions in the worker, not during curses redraw.
            lines = catalogue_lines(releases, self.t, installed, catalogue=catalogue)
            return releases, catalogue, lines

        snapshot = self._upgrade_job(title, lambda _: discover(), error_key='self_upgrade_discovery_failed')
        selected = None
        review = None
        while True:
            recovery_clear, recovery = tui_recovery_state(self.t)
            if not recovery_clear:
                review = None
            releases, catalogue, lines = snapshot if snapshot is not None else ([], None, [])
            overview = [
                (self.t('self_upgrade_installed', version=str(installed)), 'section'),
                (self.t('self_upgrade_target', version=selected.tag if selected else '?'), 'title'),
                (recovery[0], 'muted' if recovery_clear else 'failure'),
            ]
            actions = []
            entries = []

            def add(action, label, style=''):
                actions.append(action)
                entries.append((self.t(label), style))

            decision = (review.decision if review is not None else
                        catalogue.decision_for(selected.version) if selected and catalogue is not None else None)
            if decision is not None:
                status = release_status(selected, installed, decision)
                eligible = (selected.version > installed and decision.status == 'compatible')
                overview.insert(2, (self.t(status), 'success' if eligible else 'warning'))
                overview.extend((line, 'muted') for line in decision_lines(decision, self.t))
            else:
                eligible = False
                overview.insert(2, (self.t('upgrade_status_not_checked'), 'muted'))
            if review is not None:
                overview.insert(4, (self.t('self_upgrade_reviewed'), 'section'))
            if releases:
                add('target', 'self_upgrade_select_target')
            else:
                overview.append((self.t('self_upgrade_empty' if snapshot is not None else
                                        'self_upgrade_discovery_failed'), 'warning'))
            if selected is not None:
                add('notes', 'self_upgrade_notes_menu')
            if eligible:
                add('review', 'self_upgrade_review_menu')
                if review is not None:
                    add('review_details', 'upgrade_overview_details')
                    if recovery_clear:
                        add('install', 'self_upgrade_install_reviewed', 'warning')
            add('status', 'self_upgrade_status_menu')
            add('refresh', 'upgrade_refresh')
            choice = self._choose(title, entries, hint, overview=overview,
                                  escape_clears_selection=False)
            if not isinstance(choice, int):
                return None
            action = actions[choice]
            if action == 'target':
                index = self._choose(title, [(line, '') for line in lines[1:-1]], hint,
                                     overview=[overview[0]], escape_clears_selection=False)
                if isinstance(index, int):
                    selected = releases[index]
                    review = None
            elif action == 'notes':
                self._show_lines(title, [(line, '') for line in changelog_lines(
                    selected, self.t, installed, catalogue=catalogue)], allow_quit=True)
            elif action == 'status':
                self._show_lines(title, [(line, '') for line in recovery], allow_quit=True)
            elif action == 'refresh':
                selected = None
                review = None
                snapshot = self._upgrade_job(title, lambda _: discover(),
                                             error_key='self_upgrade_discovery_failed')
            elif action == 'review':
                review = None
                candidate = self._upgrade_job(title, lambda _: review_upgrade(selected.version),
                                              error_key='self_upgrade_review_failed')
                if candidate is not None:
                    accepted = self._show_lines(title, [(line, '') for line in review_lines(candidate, self.t)],
                                                self.t('upgrade_review_hint'), allow_quit=True)
                    if (accepted is not False and candidate.decision.status == 'compatible'
                            and candidate.decision.steps and candidate.target == selected.version
                            and candidate.installed.version == installed):
                        review = candidate
            elif action == 'review_details':
                self._show_lines(title, [(line, '') for line in review_lines(review, self.t)],
                                 allow_quit=True)
            elif action == 'install':
                # Read live recovery state again; the backend independently
                # rechecks the reviewed path, installation and locked state.
                clear, status = tui_recovery_state(self.t)
                if not clear or review is None or review.target != selected.version:
                    review = None
                    self._show_lines(title, [(line, 'failure') for line in status])
                    continue
                if self._show_lines(title, [(line, '') for line in review_lines(review, self.t)],
                                    self.t('upgrade_review_hint'), allow_quit=True) is False:
                    continue
                prompt = self.t('self_upgrade_install_warning')
                if not self._confirm_token(title, prompt, review.token,
                                           target_names=[f'v{step}' for step in review.decision.steps],
                                           summary=[(prompt, 'warning')]):
                    continue
                clear, status = tui_recovery_state(self.t)
                if not clear:
                    review = None
                    self._show_lines(title, [(line, 'failure') for line in status])
                    continue
                messages = []

                def progress(message: str) -> None:
                    messages.append(message)
                    frame = self._frame(title, self.t('tui_quit_blocked'))
                    if frame:
                        h, w = frame
                        for i, line in enumerate(messages[-max(1, h - 6):], 3):
                            self._put(i, 3, safe_text(line)[:max(1, w - 6)], 'muted')
                        self.stdscr.refresh()

                result = execute_review(review, review.token, self.t, progress)
                self._flush_input()
                if result == HANDOFF_EXIT:
                    return result
                review = None
                self._show_lines(title, [(line, '') for line in messages])

    def _render_journal(self, panel: JournalPanel, y: int, rows: int,
                         x: int, width: int, *, viewport: JournalViewport) -> None:
        """Render the bounded viewport, anchored while reading older live logs."""
        if rows <= 0 or width < 8:
            return
        if panel.error:
            self._put(y, x, panel.error[:width], 'failure')
            return
        if panel.loading and not panel.lines:
            self._put(y, x, self.t('nbo_journal_loading'), 'muted')
            return
        if not panel.lines:
            self._put(y, x, self.t('logs_empty'), 'muted')
            return
        viewport.horizontal(panel, width, 0)
        start, end = viewport.bounds(panel, rows)
        for j, text in enumerate(list(panel.lines)[start:end]):
            self._put(y + j, x, text[viewport.column:viewport.column + width], 'muted')

    def _manage(self) -> None:
        """Manage marked services with operations above the journal preview."""
        try:
            config = load_config(self.config_path)
            systemd = SystemdServices(config)
            self.versions.config = config
            states = systemd.status(SERVICE_NAMES)
            self._update_cpu(states)
        except (ServiceError, ConfigError, OSError) as exc:
            self._show_lines(self.t('tui_error_title'), [(str(exc), 'failure')])
            return
        chosen: set[str] = set()
        focus: str | None = None
        index = 0
        operation = 0
        refreshed = time.monotonic()
        for state in states:
            self.versions.request(state.name)
        operations = ACTIONS
        movement = (curses.KEY_UP, curses.KEY_DOWN, ord('j'), ord('k'),
                    curses.KEY_LEFT, curses.KEY_RIGHT)
        panel = JournalPanel(systemd)
        viewport = JournalViewport()
        positions: list[tuple[int, int]] = []
        log_rows = 1
        log_width = 1
        self.stdscr.timeout(150)
        try:
            while True:
                panel.poll()
                if time.monotonic() - refreshed >= 3:
                    try:
                        states = systemd.status(SERVICE_NAMES)
                        self._update_cpu(states)
                    except ServiceError:
                        pass
                    refreshed = time.monotonic()
                if states:
                    index = min(index, len(states) - 1)
                preview = states[index] if states and focus is not None else None
                if preview and panel.name != preview.name:
                    panel.select(preview.name)
                    viewport.reset()
                hint_key = {'actions': 'nbo_manage_actions_hint', 'logs': 'nbo_manage_logs_hint'}.get(
                    focus, 'nbo_manage_hint_035')
                frame = self._frame(self.t('tui_manage_title'), self._quit_hint(self.t(hint_key)))
                if frame:
                    h, w = frame
                    split = max(31, min(44, w // 3))
                    labels = [self.t('action_' + action) for action in operations]
                    positions = self._backup_action_positions(labels, w - 10, separator_before=3)
                    action_rows = max(row for _, row in positions) + 1
                    # Reserve the service list and operation bar first. On
                    # short terminals the journal preview disappears entirely.
                    actions_y = max(10, min(13 if h >= 27 else 11,
                                             h - 3 - action_rows))
                    log_title_y = actions_y + action_rows + 2
                    compact_logs = focus == 'logs' and h - log_title_y < 7
                    log_width = w - 10
                    if compact_logs:
                        log_title_y = 5
                        self._put(3, 3, f'{self.t("nbo_services_label")}: {preview.name if preview else "—"}', 'section')
                    else:
                        self._paint(3, 2, split - 3, max(1, actions_y - 4))
                        self._paint(3, split + 1, w - split - 4, max(1, actions_y - 4))
                        service_heading = f'{self.t("nbo_services_label")}  {self.t("nbo_chosen")}: {len(chosen)}'
                        if len(service_heading) > split - 5:
                            service_heading = f'{self.t("nbo_services_label")}  ({len(chosen)})'
                        self._put(3, 3, service_heading, 'section')
                        self._put(3, split + 2, self.t('nbo_detail_label'), 'section')
                        for i, state in enumerate(states):
                            y = 5 + i
                            if y >= actions_y:
                                break
                            selected = focus == 'services' and index == i
                            if selected:
                                self._put(y, 3, ' ' * (split - 5), 'selected')
                            self._put(y, 4, f"[{'x' if state.name in chosen else ' '}] {state.name:<13}",
                                      'selected' if selected else '')
                            self._put(y, 24, '●' if state.healthy else '○',
                                      'selected' if selected else state_style(state))
                        if preview is not None:
                            x = split + 2
                            version = self.versions.get(preview.name)
                            props = (
                                (5, preview.unit, 'title'),
                                (6, f'{self.t("tui_col_state")}: {preview.active} ({preview.substate})',
                                 state_style(preview)),
                                (7, f'{self.t("tui_col_startup")}: {preview.enabled}    PID: {preview.main_pid}', 'muted'),
                                (9, f'CPU: {self.cpu_display.get(preview.name, "—")}    '
                                 f'RAM: {self._human_size(preview.memory_current)}', 'section'),
                                (10, f'{self.t("nbo_service_version")}: {version or "—"}', 'muted'),
                                (11, f'{self.t("nbo_service_uptime")}: {self._uptime(preview.uptime_seconds)}', 'muted'),
                            )
                            for y, value, style in props:
                                if y < actions_y - 1:
                                    self._put(y, x, value, style)
                        elif 5 < actions_y - 1:
                            self._put(5, split + 2, self.t('nbo_select_to_preview'), 'muted')

                        self._put(actions_y - 1, 3, '─' * (w - 7), 'muted')
                        self._put(actions_y, 3, self.t('nbo_actions_label'), 'section')
                        targets = [s for s in states if s.name in chosen]
                        startup_x, startup_row = positions[3]
                        if startup_row == positions[2][1]:
                            self._put(actions_y + 1 + startup_row, 2 + startup_x, '│', 'muted')
                        for i, action in enumerate(operations):
                            ax, row = positions[i]
                            enabled = any(action_enabled(action, state) for state in targets)
                            focused = focus == 'actions' and operation == i
                            style = ('selected' if focused and enabled
                                     else ('title' if enabled else 'muted'))
                            self._put(actions_y + 1 + row, 4 + ax,
                                      f'[{labels[i]}]' if enabled else f' {labels[i]} ', style)
                            if focused and not enabled:
                                self._put(actions_y + 1 + row, 3 + ax, '›', 'section')
                    if log_title_y <= h - 4:
                        self._put(log_title_y - 1, 3, '─' * (w - 7), 'muted')
                        self._put(log_title_y, 3,
                                  self.t('nbo_live_logs_title') if panel.live
                                  else self.t('nbo_logs_title'),
                                  'selected' if focus == 'logs' else 'section')
                        if preview is not None:
                            self._put(log_title_y, min(42, max(28, w // 2)),
                                      f'[L: {self.t("nbo_live_action")} '
                                      f'{"ON" if panel.live else "OFF"}]',
                                      'success' if panel.live else 'muted')
                        logs_y = log_title_y + 1
                        logs_height = max(0, h - 3 - logs_y)
                        log_rows = max(1, logs_height - 1)
                        if focus == 'logs':
                            mode = self.t('nbo_journal_paused') if viewport.end is not None else self.t('nbo_journal_tail')
                            self._put(h - 3, 3, f'{mode} | N: {panel.count} | → {viewport.column}', 'section')
                        if logs_height:
                            self._paint(logs_y, 2, w - 4, logs_height)
                            if preview is None:
                                self._put(logs_y + 1, 4, self.t('nbo_select_to_preview'), 'muted')
                            else:
                                self._render_journal(panel, logs_y + 1, logs_height - 1,
                                                     4, log_width, viewport=viewport)
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                self._check_quit(key)
                if key == 3:
                    return
                if key == 27:
                    if focus in ('actions', 'logs'):
                        focus = 'services'
                    elif chosen:
                        chosen.clear()
                    elif focus is not None:
                        focus = None
                    else:
                        return
                elif key in (curses.KEY_F5, ord('r'), ord('R')):
                    refreshed = 0
                    if panel.name:
                        panel.refresh()
                        viewport.reset()
                elif key in (ord('l'), ord('L')) and preview:
                    panel.set_live(not panel.live)
                    viewport.reset()
                elif key in (ord('n'), ord('N')) and preview:
                    self.stdscr.timeout(-1)
                    count = self._read_number(panel.count)
                    self.stdscr.timeout(150)
                    if count is not None:
                        panel.count = count
                        panel.refresh()
                        viewport.reset()
                elif key == 9:
                    focus = {'services': 'actions', 'actions': 'logs', 'logs': 'services'}.get(focus, 'actions')
                elif key == curses.KEY_BTAB:
                    focus = {'services': 'logs', 'logs': 'actions', 'actions': 'services'}.get(focus, 'services')
                elif focus == 'logs' and key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE,
                                                 curses.KEY_NPAGE, ord('j'), ord('k')):
                    amount = -1 if key in (curses.KEY_UP, curses.KEY_PPAGE, ord('k')) else 1
                    if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                        amount *= log_rows
                    viewport.move(panel, log_rows, amount)
                elif focus == 'logs' and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                    viewport.horizontal(panel, log_width, -8 if key == curses.KEY_LEFT else 8)
                elif focus == 'logs' and key == curses.KEY_END:
                    viewport.end = None
                elif focus == 'logs' and key == curses.KEY_HOME:
                    viewport.move(panel, log_rows, -len(panel.lines))
                elif key in movement:
                    if focus is None:
                        focus = 'services'
                        index = max(0, len(states) - 1) if key in (curses.KEY_UP, ord('k')) else 0
                    elif focus == 'services':
                        index = move_selection(index, key, len(states))
                    elif focus == 'actions':
                        operation = move_action_focus(operation, key, positions)
                elif focus == 'services' and key == 32 and states:
                    name = states[index].name
                    if name in chosen:
                        chosen.remove(name)
                    else:
                        chosen.add(name)
                elif key in (10, 13, curses.KEY_ENTER):
                    if focus == 'services' and states:
                        focus = 'logs'
                    elif focus == 'actions':
                        action = operations[operation]
                        targets = [s for s in states if s.name in chosen]
                        eligible = [s.name for s in targets if action_enabled(action, s)]
                        skipped = [s.name for s in targets if s.name not in eligible]
                        if not eligible:
                            self._show_lines(self.t('tui_error_title'),
                                             [(self.t('nbo_no_applicable'), 'muted')])
                            self.stdscr.timeout(150)
                            continue
                        if os.geteuid() != 0:
                            self._show_lines(self.t('tui_error_title'),
                                             [(self.t('root_required_actions'), 'failure')])
                            self.stdscr.timeout(150)
                            continue
                        confirmed = (self._confirm_action(action, eligible, skipped=skipped)
                                     if skipped else self._confirm_action(action, eligible))
                        if confirmed:
                            self._execute_batch(systemd, action, eligible)
                            try:
                                states = systemd.status(SERVICE_NAMES)
                                self._update_cpu(states)
                            except ServiceError:
                                pass
                            if panel.name:
                                panel.refresh()
                                viewport.reset()
                            refreshed = time.monotonic()
                        self.stdscr.timeout(150)
        finally:
            panel.close()
            self.stdscr.timeout(-1)

    def _confirm_token(self, title: str, prompt: str, token: str,
                       *, target_names: Sequence[str] = (),
                       summary: Sequence[tuple[str, str]] = ()) -> bool:
        typed = ''
        invalid = False
        offset = 0
        visible = 1
        lines = list(summary) or [(prompt, '')]
        while True:
            frame = self._frame(title, self.t('tui_confirm_hint'))
            if frame:
                offset, visible = self._confirmation_panel(
                    *frame, lines, typed, token=token, invalid=invalid,
                    target_names=target_names, offset=offset)
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (27, 3):
                return False
            if not frame:
                continue
            if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
                offset = self._confirmation_scroll(key, offset, visible)
            elif key in (10, 13, curses.KEY_ENTER):
                if typed == token:
                    return True
                typed = ''
                invalid = True
            elif key in (8, 127, curses.KEY_BACKSPACE):
                typed = typed[:-1]
                invalid = False
            elif 32 <= key <= 126 and len(typed) < 48:
                typed += chr(key)
                invalid = False

    def _run_backup_job(self, title: str, worker, *, tasks: Sequence[BackupTask] = ()) -> bool:
        """Report explicit task events; only the worker's result establishes success."""
        events: queue.SimpleQueue[BackupTaskEvent] = queue.SimpleQueue()
        progress = ('prepare', 0, sum(task.applicable for task in tasks), '')
        tick = 0
        self._backup_steps = []
        self._backup_step_indexes = {}
        for task in tasks:
            self._record_backup_step(BackupTaskEvent(
                task, 'pending' if task.applicable else 'not_applicable', 0, progress[2]))
        self.stdscr.timeout(150)
        result = None
        error = ''

        def drain():
            nonlocal progress
            while not events.empty():
                event = events.get_nowait()
                self._record_backup_step(event)
                phase, _, _, detail = progress
                if event.state == 'running':
                    phase, detail = event.task.phase, event.task.detail
                progress = phase, event.done, event.total, detail

        try:
            # Display the plan before starting the worker.
            self._draw_backup_progress(title, progress, tick)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker, events.put)
                while not future.done():
                    drain()
                    self._draw_backup_progress(title, progress, tick)
                    tick += 1
                    self.stdscr.getch()  # No cancellation while a transaction runs.
                drain()
                result = future.result()
                progress = ('complete', progress[1], progress[2], '')
        except Exception as exc:
            error = safe_text(exc) or type(exc).__name__
            drain()
        finally:
            self.stdscr.timeout(-1)
        self._draw_backup_progress(title, progress, tick,
                                   finished=True, error=error, result=result)
        self._wait_result_ack()
        return not bool(error)

    def _record_backup_step(self, event: BackupTaskEvent) -> None:
        """Update the exact task; never infer success from another task's arrival."""
        key = event.task.key
        row = (event.task.phase, event.state, safe_text(event.task.detail))
        if key in self._backup_step_indexes:
            self._backup_steps[self._backup_step_indexes[key]] = row
        else:
            self._backup_step_indexes[key] = len(self._backup_steps)
            self._backup_steps.append(row)

    def _draw_backup_progress(self, title: str, progress: tuple, tick: int,
                              *, finished: bool = False, error: str = '',
                              result=None) -> None:
        frame = self._frame(title, self.t('nbo_progress_result_hint') if finished
                            else self.t('tui_quit_blocked'))
        if not frame:
            return
        h, w = frame
        phase, done, total, detail = progress
        self._put(4, 3, self.t('nbo_stage_'+phase), 'section')
        length = max(10, min(50, w-20))
        filled = round(length * done / total) if total else 0
        count = f'{done}/{total}' if total else '—'
        self._put(7, 3, '['+'█'*filled+'░'*(length-filled)+'] '+count,
                  'failure' if error else 'title')
        status = (self.t('nbo_job_failed') if error else self.t('nbo_job_success')) if finished \
                 else self.t('nbo_working') + '.'*(1+(tick % 4))
        self._put(9, 3, status, 'failure' if error else ('success' if finished else 'muted'))
        if error:
            self._put(10, 3, error[:max(8,w-9)], 'failure')
        elif finished and result is not None:
            self._put(10, 3, safe_text(getattr(result,'path',result))[:max(8,w-9)], 'title')
            if getattr(result, 'detail', ''):
                self._put(11, 3, result.detail[:max(8,w-9)], 'muted')
        elif detail:
            self._put(10, 3, detail[:max(8,w-9)], 'muted')
        self._put(12, 3, self.t('nbo_task_header'), 'section')
        visible = max(1, h - 15)
        active = next((i for i, (_, state, _) in enumerate(self._backup_steps)
                       if state == ('failed' if error else 'running')), 0)
        offset = max(0, active - visible + 1)
        for i, (phase_name, step_status, info) in enumerate(self._backup_steps[offset:offset + visible]):
            marker = self.t('nbo_task_state_' + step_status)
            style = {'done': 'success', 'failed': 'failure', 'running': 'title'}.get(step_status, 'muted')
            self._put(13 + i, 4, f'{marker} {self.t("nbo_stage_" + phase_name)}' +
                      (f' — {info}' if info else '')[:max(0, w - 35)], style)
        self.stdscr.refresh()

    def _targets_summary(self, names: Sequence[str], width: int) -> str:
        """Show marked operation targets without confusing them with keyboard focus."""
        prefix = self.t('nbo_operation_targets') + ': '
        if not names:
            return (prefix + self.t('nbo_none_marked'))[:width]
        summary = ', '.join(names)
        if len(prefix) + len(summary) > width and len(names) > 1:
            summary = f'{names[0]} (+{len(names) - 1})'
        if len(prefix) + len(summary) > width:
            tail = max(1, width - len(prefix) - 1)
            summary = '…' + summary[-tail:]
        return (prefix + summary)[:width]

    @staticmethod
    def _backup_action_positions(labels: Sequence[str], width: int, *,
                                 separator_before: int | None = None) -> list[tuple[int, int]]:
        """Lay out actions, reserving a non-selectable gap between groups."""
        positions: list[tuple[int, int]] = []
        x = row = 0
        for i, label in enumerate(labels):
            length = len(label) + 2
            if i == separator_before and x > 0:
                group_width = sum(len(item) + 4 for item in labels[i:]) - 2
                if x + 2 + length > width or (
                    group_width <= width and x + 2 + group_width > width
                ):
                    row += 1
                    x = 0
                else:
                    x += 2
            if x > 0 and x + length > width:
                row += 1
                x = 0
            positions.append((x, row))
            x += length + 2
        return positions

    @staticmethod
    def _backup_action_enabled(action: str, count: int, marked: int,
                               target_present: bool, *, restorable: bool = True,
                               verifiable: bool = True) -> bool:
        """Focus is never authorization for a backup operation."""
        if action == 'create':
            return True
        if action == 'select_all':
            return count > 0
        if action == 'restore':
            return marked == 1 and target_present and restorable
        if action == 'verify':
            return marked > 0 and verifiable
        if action == 'delete':
            return marked > 0
        return False

    def _choose_restore_scope(self, entry) -> str | None:
        """Explicit, arrow-controlled restore scope; no accidental Enter default."""
        kinds = ('db', 'media', 'all')
        names = (self.t('nbo_restore_db'), self.t('nbo_restore_media'),
                 self.t('nbo_restore_all'))
        descriptions = (self.t('nbo_restore_db_desc'), self.t('nbo_restore_media_desc'),
                        self.t('nbo_restore_all_desc'))
        selected: int | None = None
        while True:
            frame = self._frame(self.t('nbo_restore_choose'), self._quit_hint(self.t('nbo_restore_scope_hint')))
            if frame:
                h, w = frame
                width = min(w-8, max(55, min(92, w-8)))
                self._paint(3, 3, width, min(18, h-7))
                self._put(4, 5, self.t('nbo_restore_source'), 'section')
                self._put(5, 5, safe_text(entry.path.name)[:max(10,width-7)], 'title')
                compact = h < 25
                for i, (name, description) in enumerate(zip(names, descriptions)):
                    y = (7 + i*2) if compact else (8 + i*3)
                    if y >= h-3:
                        break
                    highlighted = i == selected
                    if highlighted:
                        self._put(y, 5, ' ' * (width-4), 'selected')
                    marker = '●' if highlighted else '○'
                    self._put(y, 6, f'{marker} {name}', 'selected' if highlighted else 'title')
                    if y+1 < h-3:
                        self._put(y+1, 9, description[:max(10,width-12)], 'muted')
                if h > 25:
                    self._put(19, 5, self.t('nbo_restore_scope_warning')[:max(10,width-7)], 'warning')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            self._check_quit(key)
            if key in (27,3):
                return None
            if key in (curses.KEY_UP, curses.KEY_DOWN, ord('j'), ord('k')):
                if selected is None:
                    selected = 2 if key in (curses.KEY_UP, ord('k')) else 0
                else:
                    selected = (selected + (-1 if key in (curses.KEY_UP,ord('k')) else 1)) % len(kinds)
            elif key in (10,13,curses.KEY_ENTER) and selected is not None:
                return kinds[selected]

    def _backup_inventory_counts(self, entries):
        """Count each displayed inventory entry once, without filesystem I/O."""
        counts = dict.fromkeys(('db', 'media', 'combined', 'legacy', 'unknown'), 0)
        for entry in entries:
            kind = 'combined' if entry.kind == 'full' else entry.kind
            counts[kind if kind in counts else 'unknown'] += 1
        rows = [(self.t('nbo_inventory_' + kind), count)
                for kind, count in counts.items()
                if kind in ('db', 'media', 'combined') or count]
        rows.append((self.t('nbo_inventory_total'), len(entries)))
        return rows

    def _backups(self) -> None:
        """Responsive inventory; manifests loaded only for selection and cached.

        Never decompress a large archive in the curses/main thread. Legacy
        archives without an adjacent sidecar are inspected in a worker.
        """
        from .backups import BackupManager, BackupError
        try:
            manager = BackupManager(load_config(self.config_path))
            entries = manager.quick_list()
        except (ConfigError, OSError, BackupError) as exc:
            self._show_lines(self.t('tui_error_title'), [(safe_text(exc), 'failure')])
            return
        cursor: int | None = None
        scroll = 0
        chosen: set[Path] = set()
        focus: str | None = None
        action_index = 0
        backup_actions = ('create', 'verify', 'restore', 'delete', 'select_all')
        details: dict[tuple, object] = {}
        verified: set[tuple] = set()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='nbo-metadata')
        task = None
        task_key = None
        disk_info: tuple[int,int,int,int,int] | None = None
        disk_error = ''
        inventory_error = ''

        def refresh_storage():
            nonlocal disk_info, disk_error
            try:
                measurement = manager.storage_breakdown()
                disk_info = measurement if isinstance(measurement, tuple) and len(measurement) == 5 else None
                disk_error = ''
            except (OSError, ValueError, BackupError) as exc:
                disk_info = None
                disk_error = safe_text(exc)

        refresh_storage()
        refreshed = time.monotonic()
        labels = [self.t('nbo_act_' + action) for action in backup_actions]
        headers = [self.t('nbo_' + key) for key in ('type', 'date', 'size', 'tag_header', 'name')]

        def table_values(item):
            tag = self.t('nbo_pre_restore_short' if item.purpose == 'pre_restore'
                         else 'nbo_manual_short')
            return ('combined' if item.kind == 'full' else item.kind,
                    self._pretty_date(item.created), self._human_size(item.size),
                    tag, item.path.name)

        def measure_columns():
            # Stable widths while scrolling; recalculate only on inventory refresh.
            widths = [len(label) for label in headers[:-1]]
            for item in entries:
                widths = [max(width, len(value)) for width, value in
                          zip(widths, table_values(item))]
            return [min(width, limit) for width, limit in zip(widths, (8, 16, 12, 12))]

        column_widths = measure_columns()

        def fingerprint(entry):
            # A replaced file or modified sidecar must not inherit stale detail.
            try:
                st = entry.path.stat()
                manifest = Path(str(entry.path) + '.json')
                mt = manifest.stat() if manifest.is_file() and not manifest.is_symlink() else None
                return (str(entry.path), st.st_mtime_ns, st.st_size,
                        mt.st_mtime_ns if mt else None, mt.st_size if mt else None)
            except OSError:
                return (str(entry.path), 'missing')

        def reload_inventory():
            nonlocal entries, cursor, scroll, refreshed, column_widths, inventory_error
            nonlocal disk_info, disk_error, task_key
            selected = entries[cursor].path if cursor is not None and cursor < len(entries) else None
            marked_fingerprints = {e.path: fingerprint(e) for e in entries if e.path in chosen}
            try:
                entries = manager.quick_list()
            except (BackupError, OSError) as exc:
                inventory_error = safe_text(exc) or self.t('nbo_inventory_unavailable')
                # A failed inventory cannot authorize operations on old marks.
                entries = []
                chosen.clear()
                cursor, scroll = None, 0
                details.clear()
                verified.clear()
                task_key = None  # discard a late metadata result
                disk_info = None
                disk_error = self.t('nbo_inventory_unavailable')
                column_widths = measure_columns()
                refreshed = time.monotonic()
                return False
            inventory_error = ''
            column_widths = measure_columns()
            refresh_storage()
            cursor = next((i for i, e in enumerate(entries) if e.path == selected), None)
            chosen.intersection_update(e.path for e in entries)
            # A replaced backup must be marked again, even if its path is unchanged.
            chosen.difference_update(e.path for e in entries if e.path in chosen
                                     and marked_fingerprints[e.path] != fingerprint(e))
            scroll = min(scroll, max(0, len(entries) - 1))
            refreshed = time.monotonic()
            # Old entries remain cached only while the fingerprints still match.
            fresh = {fingerprint(e) for e in entries}
            for key in list(details):
                if key not in fresh:
                    details.pop(key)
                    verified.discard(key)
            return True

        self.stdscr.timeout(120)
        try:
            while True:
                if time.monotonic() - refreshed >= 10:
                    reload_inventory()
                if task is not None and task.done():
                    try:
                        result = task.result()
                    except Exception:
                        result = None
                    if task_key is not None:
                        details[task_key] = result
                    task = None
                    task_key = None
                entry = entries[cursor] if cursor is not None and cursor < len(entries) else None
                marked = [item for item in entries if item.path in chosen]
                # Detail follows keyboard focus. Marked files are separate
                # operation targets; never pin preview to the first marked row.
                preview = entry if entry is not None else (marked[0] if len(marked) == 1 else None)
                entry_key = fingerprint(preview) if preview else None
                if preview is not None and entry_key not in details and task is None:
                    # v0.3.1 sidecars are tiny JSON: resolve immediately. Older
                    # gzip-only archives must never block the curses thread.
                    sidecar = Path(str(preview.path) + '.json')
                    if sidecar.is_file() and not sidecar.is_symlink():
                        details[entry_key] = manager.describe_one(preview)
                    else:
                        task_key = entry_key
                        task = pool.submit(manager.describe_one, preview)
                detail = (details.get(entry_key) or preview) if preview else None
                ready = preview is not None and details.get(entry_key) is not None
                if len(marked) == 1 and fingerprint(marked[0]) not in details and task is None:
                    other = marked[0]
                    other_key = fingerprint(other)
                    task_key = other_key
                    task = pool.submit(manager.describe_one, other)
                # A legacy or corrupt archive is never presented as
                # restorable. Metadata must be loaded before enabling restore.
                action_targets = marked
                incompatible = any(item.kind == 'legacy' or
                                   (item is not None and
                                    getattr(details.get(fingerprint(item)), 'legacy', False))
                                   for item in action_targets)
                can_restore = (len(marked) == 1 and
                               details.get(fingerprint(marked[0])) is not None and
                               not incompatible)
                # Legacy archives may be verified read-only, but never restored.
                can_verify = bool(action_targets)
                props = []
                if preview is not None:
                    purpose = (self.t('nbo_pre_restore') if detail.purpose == 'pre_restore'
                               else self.t('nbo_manual'))
                    props = [
                        (self.t('nbo_name') + ': ' + preview.path.name, 'title'),
                        (self.t('nbo_type') + ': ' + self.t('nbo_kind_' + preview.kind) + '   ' +
                         self.t('nbo_purpose') + ': ' + purpose, 'muted'),
                        (self.t('nbo_date') + ': ' + self._pretty_date(detail.created) + ' UTC', 'muted'),
                        (self.t('nbo_size') + ': ' + self._human_size(preview.size), 'muted'),
                        (self.t('nbo_version') + ': ' + (detail.version or '—')
                         if ready else self.t('nbo_metadata_loading'), 'muted'),
                        (self.t('nbo_legacy_blocked') if ready and detail.legacy else
                         (self.t('nbo_integrity_ok') if entry_key in verified else
                          self.t('nbo_integrity_not_checked')),
                         'failure' if ready and detail.legacy else 'muted'),
                        (self.t('nbo_contents') + ': ' + self.t('nbo_kind_contents_' + preview.kind), 'muted'),
                    ]

                hint_key = 'nbo_backup_actions_hint' if focus == 'actions' else 'nbo_backup_hint_034'
                positions = self._backup_action_positions(
                    labels, max(1, self.stdscr.getmaxyx()[1] - 8), separator_before=4)
                hint = self.t(hint_key) if self.stdscr.getmaxyx()[1] >= 100 else self.t('nbo_inventory_hint_short')
                frame = self._frame(self.t('nbo_backup_title'), self._quit_hint(hint))
                if frame:
                    h, w = frame
                    pane = w >= 104
                    split = max(56, min(92, w * 55 // 100)) if pane else w - 3
                    count_rows = self._backup_inventory_counts(entries) if not inventory_error else []
                    selected_label = f'{self.t("nbo_chosen")}: {len(marked)}'
                    all_x, all_row = positions[-1]
                    count_x = all_x + len(labels[-1]) + 4
                    count_row = all_row
                    if count_x + len(selected_label) > w - 8:
                        selected_label = f'{self.t("nbo_inventory_selected_short")}: {len(marked)}'
                    if count_x + len(selected_label) > w - 8:
                        count_x, count_row = 0, all_row + 1
                    action_rows = max(count_row, all_row) + 1
                    # The list owns the top and at least one visible item at the
                    # minimum terminal size. Bottom panels use at most half the
                    # space left after reserving the action bar.
                    panel_height = min(12 if pane else 10,
                                       max(3, (h - 5 - action_rows - 4) // 2))
                    detail_y = h - 2 - panel_height
                    action_y = detail_y - action_rows - 2
                    list_title_y = 3
                    self._paint(list_title_y, 2, w - 4, action_y - list_title_y - 1)
                    self._put(list_title_y, 3, self.t('nbo_backup_table')[:w - 6], 'section')

                    # Select the columns and one spacing policy for the entire
                    # table. Name always takes the remaining width.
                    table_width = w - 7
                    show_tag = 4 + sum(column_widths) + 4 * 3 + 12 <= table_width
                    indexes = [0, 1, 2, 3, 4] if show_tag else [0, 1, 2, 4]
                    widths = [column_widths[i] for i in indexes[:-1]]
                    # On the minimum 50-column terminal keep at least one
                    # name cell even for unusually large disk sizes.
                    if not show_tag:
                        widths[2] = min(widths[2], table_width - 4 - sum(widths[:2]) - 4)
                    gap = 3 if 4 + sum(widths) + len(widths) * 3 + 8 <= table_width else 1
                    widths.append(max(1, table_width - 4 - sum(widths) - len(widths) * gap))

                    def format_row(values, marker='    '):
                        cells = [safe_text(values[i])[:width].ljust(width)
                                 for i, width in zip(indexes, widths)]
                        return marker + (' ' * gap).join(cells)

                    if entries:
                        self._put(list_title_y + 1, 4, format_row(headers), 'muted')
                    else:
                        message = self.t('nbo_inventory_unavailable' if inventory_error else 'nbo_backup_empty')
                        self._put(list_title_y + 1, 4, message[:w - 8],
                                  'warning' if inventory_error else 'muted')
                    visible = max(0, action_y - list_title_y - 3)
                    scroll = min(scroll, max(0, len(entries) - visible))
                    if cursor is not None and visible:
                        if cursor < scroll:
                            scroll = cursor
                        elif cursor >= scroll + visible:
                            scroll = cursor - visible + 1
                    for i in range(scroll, min(len(entries), scroll + visible)):
                        item = entries[i]
                        row = list_title_y + 2 + i - scroll
                        selected = i == cursor and focus != 'actions'
                        if selected:
                            self._put(row, 3, ' ' * (w - 6), 'selected')
                        marker = f"[{'x' if item.path in chosen else ' '}] "
                        self._put(row, 4, format_row(table_values(item), marker),
                                  'selected' if selected else '')

                    self._put(action_y - 1, 3, '─' * (w - 7), 'muted')
                    self._put(action_y, 3, self.t('nbo_backup_actions_label'), 'section')
                    all_x, all_row = positions[-1]
                    if all_row == positions[-2][1]:
                        self._put(action_y + 1 + all_row, 2 + all_x, '│', 'muted')
                    for i, action in enumerate(backup_actions):
                        ax, row = positions[i]
                        enabled = self._backup_action_enabled(
                            action, len(entries), len(marked),
                            len(marked) == 1, restorable=can_restore,
                            verifiable=can_verify)
                        style = ('selected' if focus == 'actions' and action_index == i and enabled
                                 else ('title' if enabled else 'muted'))
                        label = f'[{labels[i]}]' if enabled else f' {labels[i]} '
                        self._put(action_y + 1 + row, 4 + ax, label, style)
                        if focus == 'actions' and action_index == i and not enabled:
                            self._put(action_y + 1 + row, 3 + ax, '›', 'section')
                    self._put(action_y + 1 + count_row, 4 + count_x, selected_label,
                              'title' if marked else 'muted')
                    self._put(detail_y - 1, 3, '─' * (w - 7), 'muted')

                    # Match the separate service/detail panels. On narrow
                    # terminals stack storage below the preview instead.
                    storage_rows = 2 if panel_height >= 6 else 1
                    storage_y = detail_y if pane else h - 2 - storage_rows
                    detail_end = h - 2 if pane else storage_y - (1 if storage_rows == 2 else 0)
                    self._paint(detail_y, 2, split - 3 if pane else w - 4,
                                detail_end - detail_y)
                    self._put(detail_y, 3, self.t('nbo_detail_label'), 'section')
                    detail_width = split - 6 if pane else w - 8
                    if not props:
                        self._put(detail_y + 1, 4,
                                  self.t('nbo_backup_select_preview')[:detail_width], 'muted')
                    else:
                        y = detail_y + 1
                        for j, (value, style) in enumerate(props):
                            parts = textwrap.wrap(safe_text(value), detail_width,
                                                  break_long_words=True) or ['']
                            if j:
                                parts = parts[:2]
                            for k, line in enumerate(parts):
                                if y >= detail_end:
                                    break
                                if j == 0 and y == detail_end - 1 and k < len(parts) - 1:
                                    line = line[:detail_width - 1] + '…'
                                self._put(y, 4, line, style)
                                y += 1
                            if y >= detail_end:
                                break

                    if pane:
                        x = split + 2
                        panel_width = w - x - 3
                        self._paint(detail_y, split + 1, w - split - 4, panel_height)
                        self._put(detail_y, x, self.t('nbo_storage_label')[:panel_width], 'section')
                        legend_x = x
                        legend_width = panel_width
                        counts_start = detail_y + 3
                        if disk_info is not None:
                            used, other, free, total, _ = disk_info
                            show_ring = panel_height >= 10 and panel_width >= 46
                            legend_x = x + 21 if show_ring else x
                            legend_width = w - legend_x - 3
                            if show_ring:
                                self._draw_storage_ring(x, detail_y + 1, disk_info)
                            if panel_height >= 5:
                                legends = ((self.t('nbo_storage_used'), used, 'disk_backup'),
                                           (self.t('nbo_storage_other'), other, 'disk_other'),
                                           (self.t('nbo_storage_free'), free, 'disk_free'))
                                for j, (label, value, color) in enumerate(legends):
                                    self._put(detail_y + 1 + j, legend_x,
                                              f'● {label}: {self._human_size(value)}'[:legend_width], color)
                                counts_start = detail_y + 5
                            else:
                                self._put(detail_y + 1, x,
                                          f'{self._human_size(used)} / {self._human_size(total)}'[:panel_width],
                                          'muted')
                        else:
                            self._put(detail_y + 1, x, (disk_error or '—')[:panel_width],
                                      'warning' if disk_error else 'muted')
                        # Inventory counts are independent of disk accounting.
                        show_multi = len(marked) >= 2 and panel_height >= 8
                        counts_end = detail_y + panel_height - (3 if show_multi else 0)
                        size = max((len(label) for label, _ in count_rows), default=0)
                        if count_rows and counts_start + len(count_rows) <= counts_end and all(
                                size + 3 + len(str(count)) <= legend_width for _, count in count_rows):
                            for row, (label, count) in enumerate(count_rows, counts_start):
                                self._put(row, legend_x, f'{label:<{size}} : {count}', 'muted')
                        elif counts_start - 1 < counts_end:
                            message = (self.t('nbo_inventory_unavailable') if inventory_error
                                       else self.t('nbo_inventory_details'))
                            self._put(counts_start - 1, legend_x, message[:legend_width],
                                      'warning' if inventory_error else 'muted')
                        if show_multi:
                            sy = detail_y + panel_height - 3
                            self._put(sy, legend_x, (self.t('nbo_multi_title') +
                                      f' ({len(marked)})')[:legend_width], 'section')
                            self._put(sy + 1, legend_x, (f'{self.t("nbo_total_size")}: '
                                      f'{self._human_size(sum(e.size for e in marked))}')[:legend_width], 'muted')
                            self._put(sy + 2, legend_x,
                                      self.t('nbo_multi_restore_disabled')[:legend_width], 'warning')
                    else:
                        self._paint(storage_y, 2, w - 4, storage_rows)
                        label = self.t('nbo_storage_label')
                        storage_text = disk_error or '—'
                        if disk_info is not None:
                            used, _, _, total, _ = disk_info
                            storage_text = f'{self._human_size(used)} / {self._human_size(total)}'
                        if inventory_error:
                            storage_text = self.t('nbo_inventory_unavailable') + ' | ' + storage_text
                        label += ' (B)'
                        if storage_rows == 1:
                            self._put(storage_y, 3, f'{label}: {storage_text}'[:w - 6],
                                      'warning' if disk_error or inventory_error else 'muted')
                        else:
                            self._put(storage_y, 3, label[:w - 6], 'section')
                            self._put(storage_y + 1, 4, storage_text[:w - 8],
                                      'warning' if disk_error or inventory_error else 'muted')
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                self._check_quit(key)
                if key == 3:
                    return
                if key == 27:
                    if chosen:
                        chosen.clear()
                    elif focus is not None:
                        focus = None
                        cursor = None
                    else:
                        return
                elif key == 9:
                    focus = 'backups' if focus=='actions' else 'actions'
                elif key in (ord('*'), ord('s'), ord('S'), ord('a'), ord('A')):
                    if len(chosen)==len(entries):
                        chosen.clear()
                    else:
                        chosen = {e.path for e in entries}
                elif key == 32 and cursor is not None and focus != 'actions':
                    path = entries[cursor].path
                    if path in chosen:
                        chosen.remove(path)
                    else:
                        chosen.add(path)
                elif key in (curses.KEY_F5,):
                    if not reload_inventory():
                        self.stdscr.timeout(-1)
                        self._show_lines(self.t('tui_error_title'), [(inventory_error, 'failure')])
                        self.stdscr.timeout(120)
                elif key in (curses.KEY_UP,curses.KEY_DOWN,ord('j'),ord('k'),
                             curses.KEY_LEFT,curses.KEY_RIGHT,
                             curses.KEY_HOME,curses.KEY_END,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    if focus == 'actions':
                        action_index = move_action_focus(action_index, key, positions)
                    elif entries:
                        focus = 'backups'
                        if cursor is None:
                            cursor = len(entries)-1 if key in (curses.KEY_UP,ord('k')) else 0
                        else:
                            cursor = move_selection(cursor,key,len(entries))
                elif key in (ord('b'), ord('B')):
                    lines = []
                    if disk_info is not None:
                        used, other, free, _, _ = disk_info
                        lines.extend((f'{self.t(key)}: {self._human_size(value)}', 'muted')
                                     for key, value in (('nbo_storage_used', used),
                                                        ('nbo_storage_other', other),
                                                        ('nbo_storage_free', free)))
                    else:
                        lines.append((disk_error or '—', 'warning'))
                    if inventory_error:
                        lines.append((self.t('nbo_inventory_unavailable') + ': ' + inventory_error, 'warning'))
                    else:
                        counts = self._backup_inventory_counts(entries)
                        size = max(len(label) for label, _ in counts)
                        lines.extend((f'{label:<{size}} : {count}', 'muted') for label, count in counts)
                    lines.append((f'{self.t("nbo_chosen")}: {len(marked)}', 'title' if marked else 'muted'))
                    if len(marked) >= 2:
                        lines.extend([(f'{self.t("nbo_total_size")}: {self._human_size(sum(e.size for e in marked))}',
                                       'muted'), (self.t('nbo_multi_restore_disabled'), 'warning')])
                    self.stdscr.timeout(-1)
                    try:
                        self._show_lines(self.t('nbo_storage_label'), lines, allow_quit=True)
                    finally:
                        self.stdscr.timeout(120)
                elif key in (ord('i'), ord('I')) and focus != 'actions' and props:
                    # A long filename can always be read in full, even when the
                    # compact bottom panel has only one preview row.
                    self.stdscr.timeout(-1)
                    try:
                        self._show_lines(self.t('nbo_detail_label'), props,
                                         self.t('tui_back_hint'), allow_quit=True)
                    finally:
                        self.stdscr.timeout(120)
                elif key in (10,13,curses.KEY_ENTER) and focus == 'actions':
                    action = backup_actions[action_index]
                    target_present = len(marked) == 1
                    if not self._backup_action_enabled(action, len(entries), len(marked),
                                                       target_present, restorable=can_restore,
                                                       verifiable=can_verify):
                        if action == 'restore':
                            if not marked:
                                explanation = self.t('nbo_mark_backup_required')
                            elif len(marked) > 1:
                                explanation = self.t('nbo_restore_single')
                            elif incompatible:
                                explanation = self.t('nbo_legacy_blocked')
                            else:
                                explanation = self.t('nbo_metadata_loading')
                            self._show_lines(self.t('nbo_restore_choose'), [(explanation, 'warning')])
                            self.stdscr.timeout(120)
                        continue
                    key = {'create':ord('c'),'restore':ord('r'),'verify':ord('v'),
                           'delete':ord('d'),
                           'select_all':ord('*')}[action]
                    if key == ord('*'):
                        chosen = (set() if len(chosen)==len(entries) else {e.path for e in entries})
                        continue
                    # Continue through the same safe confirmation handlers as shortcuts.
                if key in (ord('c'),ord('C')):
                    self.stdscr.timeout(-1)
                    types = [(self.t('nbo_create_kind_db'), ''),
                             (self.t('nbo_create_kind_media'), ''),
                             (self.t('nbo_create_kind_combined'), '')]
                    i = self._choose(self.t('nbo_create_title'), types, self.t('tui_back_hint'))
                    if isinstance(i,int):
                        kind = ('db','media','combined')[i]
                        if self._confirm_token(self.t('nbo_create_title'),
                                               self.t('nbo_confirm_create',kind=kind), 'CREATE'):
                            self._run_backup_job(self.t('nbo_create_title'),
                                                 lambda cb: manager.backup(kind, task_progress=cb),
                                                 tasks=create_tasks(kind))
                            reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('r'), ord('R')) and len(marked) == 1:
                    entry = marked[0]
                    entry_key = fingerprint(entry)
                    detail = details.get(entry_key) or entry
                    ready = details.get(entry_key) is not None
                    if not ready:
                        self._show_lines(self.t('nbo_detail_label'),
                                         [(self.t('nbo_metadata_loading'), 'muted')])
                        self.stdscr.timeout(120)
                        continue
                    if detail.legacy:
                        self._show_lines(self.t('tui_error_title'),
                                         [(self.t('nbo_legacy_blocked'), 'failure')])
                        self.stdscr.timeout(120)
                        continue
                    self.stdscr.timeout(-1)
                    only = 'all'
                    if entry.kind in ('combined', 'full'):
                        only = self._choose_restore_scope(entry)
                        if only is None:
                            self.stdscr.timeout(120)
                            continue
                    # Backups made before v0.3.2 mislabeled Django as NetBox.
                    # Never auto-bypass the version guard. Require a distinct
                    # confirmation before permitting an unknown backup version.
                    accept_unknown = not bool(detail.version)
                    if accept_unknown:
                        if not self._confirm_token(
                                self.t('nbo_restore_choose'),
                                self.t('nbo_unknown_version_warning'), 'ACCEPT UNKNOWN VERSION',
                                summary=[(self.t('nbo_unknown_version_warning'), 'warning')]):
                            self.stdscr.timeout(120)
                            continue
                    token = f'RESTORE {entry.kind.upper()}' + (f' {only.upper()}' if entry.kind in ('combined', 'full') else '')
                    description = (f'{entry.path.name} | {self._pretty_date(entry.created)} | '
                                   f'{self._human_size(entry.size)}.')
                    warning = self.t('nbo_restore_warning')
                    notice = description + ' ' + warning
                    if self._confirm_token(self.t('nbo_restore_choose'), notice, token,
                                           summary=[(warning, 'warning'), (description, '')]):
                        def restore_worker(cb, item=entry, scope=only, unknown=accept_unknown):
                            opts = {'only': scope, 'task_progress': cb}
                            if unknown:
                                opts['accept_unknown'] = True
                            return manager.restore(item.kind, item.path, **opts)
                        self._run_backup_job(self.t('nbo_restore_choose'), restore_worker,
                                             tasks=restore_tasks(entry.kind, only))
                        reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('d'), ord('D')) and marked:
                    targets = marked
                    token = 'DELETE BACKUP' if len(targets)==1 else f'DELETE {len(targets)} BACKUPS'
                    self.stdscr.timeout(-1)
                    notice = self.t('nbo_delete_selected_warning', count=len(targets),
                                    size=self._human_size(sum(e.size for e in targets)))
                    if self._confirm_token(self.t('nbo_delete_title'), notice, token,
                                           target_names=[e.path.name for e in targets],
                                           summary=[(notice, 'warning')]):
                        self._run_backup_job(self.t('nbo_delete_title'),
                             lambda cb, paths=[e.path for e in targets]:
                             self._delete_selected_job(manager, paths, cb),
                             tasks=batch_tasks('delete', (e.path.name for e in targets), prepare=True))
                        reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('v'), ord('V')) and marked:
                    targets = marked
                    self.stdscr.timeout(-1)
                    if self._run_backup_job(self.t('nbo_verify_title'),
                                            lambda cb, selected=targets: self._verify_job_many(manager, selected, cb),
                                            tasks=batch_tasks('verify', (e.path.name for e in targets))):
                        verified.update(fingerprint(e) for e in targets)
                    self.stdscr.timeout(120)
                # Enter on the file list remains a no-op; I opens the full
                # read-only details without changing marks or action focus.
        finally:
            self.stdscr.timeout(-1)
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _verify_job_many(manager, entries, cb):
        tracker = BackupProgress(batch_tasks('verify', (e.path.name for e in entries)), events=cb)
        with tracker.operation():
            for entry in entries:
                with tracker.step('verify', entry.path.name):
                    manager.verify(entry.kind, entry.path)
        return f'{len(entries)} backups verified'

    @staticmethod
    def _delete_selected_job(manager, paths, cb):
        count, freed = manager.delete_backups(paths, task_progress=cb)
        return f'{count} backups deleted; {freed} bytes released'

    @staticmethod
    def _verify_job(manager, entry, cb):
        CursesMenu._verify_job_many(manager, [entry], cb)
        return entry.path

    @staticmethod
    def _pretty_date(value: str) -> str:
        try:
            if value.endswith('Z') and 'T' in value and len(value)==16:
                return dt.datetime.strptime(value,'%Y%m%dT%H%M%SZ').strftime('%Y-%m-%d %H:%M')
            return dt.datetime.fromisoformat(value.replace('Z','+00:00')).strftime('%Y-%m-%d %H:%M')
        except (ValueError, TypeError):
            return str(value)[:18]

    def _language(self) -> None:
        options = [('Português (Portugal)', ''), ('English', '')]
        result = self._choose(self.t('tui_language_title'), options, self.t('tui_back_hint'),
                              index=0 if self.t.language == 'pt' else 1)
        if isinstance(result, int):
            self.t = Translator('pt' if result == 0 else 'en')

    def run(self, initial_view: str = 'main') -> int:
        self._init_screen()
        try:
            if initial_view == 'manage':
                self._manage()
                return 0
            if initial_view == 'backups':
                self._backups()
                return 0
            return self._main()
        except _QuitTui:
            return 0


def run_tui(t: Translator, config_path: Path = CONFIG_PATH, initial_view: str = 'main') -> int:
    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        pass
    from .self_upgrade_ui import HANDOFF_EXIT

    menu = None

    def run(stdscr):
        nonlocal menu
        menu = CursesMenu(stdscr, t, config_path)
        return menu.run(initial_view)

    result = curses.wrapper(run)
    if result == HANDOFF_EXIT:
        print((menu.t if menu is not None else t)('self_upgrade_submitted'), flush=True)
        return 0
    return result
