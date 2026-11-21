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
import shutil
import concurrent.futures
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import CONFIG_PATH, SERVICE_NAMES, ConfigError, load_config
from .i18n import Translator
from .journal import JournalPanel
from .menu import MENU_ACTIONS
from .services import (ServiceError, ServiceState, SystemdServices, ServiceVersionCache,
                       challenge, order_services, perform_service_batch)

SECTIONS = {0: 'menu_services', 1: 'menu_backups', 2: 'menu_updates',
            6: 'menu_maintenance'}
LOG_COUNTS = (10, 20, 50, 100, 200, 500)
ACTIONS = ('start', 'stop', 'restart', 'enable', 'disable')


def make_main_items(t: Translator) -> list[tuple[str, str, tuple[str, ...]]]:
    """One source of truth for the menu labels and CLI commands."""
    items = [(f'{int(number):02d}  {t(key)}', 'action', (group, action))
             for number, group, action, key in MENU_ACTIONS]
    items.append((f'L   {t("tui_language")}', 'language', ()))
    items.append((f'0   {t("tui_exit")}', 'exit', ()))
    return items


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
        role = style or 'text'
        if role != 'selected' and any(x0 <= x < x1 and y0 <= y < y1
                                           for x0, y0, x1, y1 in self._panels):
            role = role + '_panel' if role + '_panel' in self.colors else role
        attr = self.colors.get(role, self.colors.get('text', 0))
        if style == 'selected':
            attr = attr or curses.A_REVERSE
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

    def _wait_result_ack(self) -> None:
        """Keep the final progress panel visible until the operator dismisses it."""
        self.stdscr.timeout(-1)
        while True:
            if self.stdscr.getch() in (10, 13, curses.KEY_ENTER, 27, 32):
                return

    def _choose(self, title: str, entries: Sequence[tuple[str, str]],
                hint: str, index: int = 0, headings: dict[int, str] | None = None,
                allow_language_key: bool = False, main_menu: bool = False) -> int | None | str:
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
            frame = self._frame(title, hint)
            if frame:
                h, _ = frame
                rows_visible = h - 6
                target = select_to_row[index] if armed else 0
                if target < scroll:
                    scroll = target
                elif target >= scroll + rows_visible:
                    scroll = target - rows_visible + 1
                for y, (item_idx, label, style) in enumerate(lines[scroll:scroll + rows_visible], 3):
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
                if armed:
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
                continue
            if allow_language_key and key in (ord('l'), ord('L')):
                return 'language'
            if not frame:
                continue
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
                    hint: str | None = None) -> None:
        """Scrollable results/errors; Esc returns to parent."""
        hint = hint or self.t('tui_back_hint')
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
            if key in (27, 10, 13, curses.KEY_ENTER, 3):
                return
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
            frame = self._frame(self.t('tui_status_title'), self.t('tui_status_hint'))
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
                                    self.t('nbo_detail_hint_032'))
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
                if key in (27, 3):
                    return
                if key in (ord('l'), ord('L')):
                    rows = ([(journal_error, 'failure')] if journal_error else
                            [(line, 'muted') for line in journal] or
                            [(self.t('logs_empty'), 'muted')])
                    self.stdscr.timeout(-1)
                    self._show_lines(self.t('tui_logs_title', count=log_count), rows)
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

    def _confirm_action(self, action: str, names: Sequence[str],
                        skipped: Sequence[str] = ()) -> bool:
        """Use exact, strong challenge for PostgreSQL and Redis changes."""
        token = challenge(action, names)
        typed = ''
        while True:
            frame = self._frame(self.t('tui_confirm_title'), self.t('tui_confirm_hint'))
            if frame:
                h, w = frame
                width = max(12, w - 8)
                lines = [self.t('action_target', action=self.t('action_' + action),
                                names=', '.join(names))]
                if skipped:
                    lines.append(self.t('nbo_skipped', names=', '.join(skipped)))
                lines.extend([self.t('tui_startup_only') if action in ('enable', 'disable')
                         else self.t('tui_action_warning'),
                         self.t('tui_confirm_token', token=token)])
                y = 3
                for line in lines:
                    for part in textwrap.wrap(safe_text(line), width=width):
                        if y >= h - 5:
                            break
                        self._put(y, 3, part, 'section' if token in part else '')
                        y += 1
                self._put(min(y + 1, h - 3), 3,
                          f'{self.t("tui_confirm_prompt")}{typed}', 'title')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (27, 3):
                return False
            if key in (10, 13, curses.KEY_ENTER):
                return typed == token
            if key in (curses.KEY_BACKSPACE, 8, 127):
                typed = typed[:-1]
            elif 32 <= key <= 126 and len(typed) < 48:
                typed += chr(key)

    def _confirm(self, names: Sequence[str]) -> bool:
        return self._confirm_action('restart', names)

    def _draw_progress(self, action: str, completed: int, total: int,
                       name: str, stage: str, detail: str,
                       *, finished: bool = False, summary: str = '',
                       failed: bool = False) -> None:
        """Completed services / total, not a fabricated estimate of systemctl duration."""
        frame = self._frame(self.t('tui_progress_title'),
                            self.t('nbo_progress_result_hint') if finished else self.t('tui_progress_hint'))
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

    def _main(self) -> int:
        """Main screen: compact menu and live service/storage dashboard."""
        from .backups import BackupManager
        names = list(SERVICE_NAMES)
        states: list[ServiceState] = []
        metrics: dict[str, str] = {}
        service_error = ''
        storage = (0, 0, 0)
        disk = (0, 0, 0, 0, 0)
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
                        storage = manager.storage_stats()
                        # disk_usage does not walk backup contents.
                        usage = shutil.disk_usage(manager.directory if manager.directory.exists() else manager.directory.parent)
                        occupied, _, count = storage
                        disk = (occupied, max(0,usage.used-occupied),usage.free,usage.total,count)
                        storage_error = ''
                    except (OSError, ValueError) as exc:
                        storage_error = safe_text(exc)[:90]
                items = make_main_items(self.t)
                frame = self._frame(self.t('tui_main_title'), self.t('nbo_main_hint'))
                if frame:
                    h, w = frame
                    split = min(max(36, w * 2 // 5), 46)
                    # Distinct dark surfaces instead of bare terminal black.
                    self._paint(3, 2, max(3, split - 3), min(19, h - 8))
                    self._paint(3, split + 1, max(3, w - split - 4), min(20, h - 8))
                    self._put(3, 3, self.t('nbo_menu_label'), 'section')
                    self._put(3, split + 2, self.t('nbo_dashboard_label'), 'section')
                    for bar_y in range(4, min(20,h-4)):
                        self._put(bar_y, split, '│', 'muted')
                    for idx, (text, _, _) in enumerate(items):
                        row = 5 + idx + (1 if idx >= 2 else 0) + (2 if idx >= 6 else 0)
                        if row >= h - 4:
                            break
                        if cursor == idx:
                            self._put(row, 3, ' ' * (split - 5), 'selected')
                        self._put(row, 4, text[:split-7], 'selected' if cursor == idx else '')
                    self._put(5, split+2, self.t('nbo_columns_v032') if w>=119 else self.t('nbo_columns_v031'), 'muted')
                    if service_error:
                        self._put(7, split+2, service_error[:w-split-5], 'failure')
                    else:
                        for i, state in enumerate(states):
                            y = 6+i
                            mark = '●' if state.healthy else '○'
                            name = f'{mark} {state.name:<11}'
                            self._put(y, split+2, name, 'success' if state.healthy else 'failure')
                            self._put(y, split+16, (self.t('tui_active') if state.healthy else self.t('tui_inactive'))[:9],
                                      'success' if state.healthy else 'failure')
                            if w >= 119:
                                version = self.versions.get(state.name)
                                self._put(y, split+27, f'{(version if version is not None else "…")[:12]:<12}', 'muted')
                                self._put(y, split+41, f'{metrics.get(state.name, "—"):>6}', 'muted')
                                self._put(y, split+51, self._human_size(state.memory_current), 'muted')
                            else:
                                self._put(y, split+28, f'{metrics.get(state.name, "—"):>6}', 'muted')
                                self._put(y, split+38, self._human_size(state.memory_current), 'muted')
                    line = 12
                    self._put(line, split+2, self.t('nbo_storage_label'), 'section')
                    if storage_error:
                        self._put(line+2, split+2, storage_error, 'failure')
                    else:
                        occupied, free, count = storage
                        if disk[3] and w-split >= 65 and h >= 25:
                            self._draw_storage_ring(split+3, line+2, disk)
                            bx = split + 23
                            self._put(line+2,bx, f'● {self.t("nbo_storage_used")}: {self._human_size(occupied)}','disk_backup')
                            self._put(line+3,bx, f'● {self.t("nbo_storage_other")}: {self._human_size(disk[1])}','disk_other')
                            self._put(line+4,bx, f'● {self.t("nbo_storage_free")}: {self._human_size(free)}','disk_free')
                            self._put(line+5,bx, f'{self.t("nbo_storage_count")}: {count}', 'muted')
                        else:
                            self._put(line+2, split+2, f'{self.t("nbo_storage_used")}: {self._human_size(occupied)}', 'muted')
                            self._put(line+3, split+2, f'{self.t("nbo_storage_free")}: {self._human_size(free)}', 'muted')
                            self._put(line+4, split+2, f'{self.t("nbo_storage_count")}: {count}', 'muted')
                    if h > 31:
                        self._put(24, split+2, self.t('nbo_live_note'), 'muted')
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
                elif key in (10, 13, curses.KEY_ENTER) and cursor is not None:
                    _, kind, data = items[cursor]
                    if kind == 'exit':
                        return 0
                    if kind == 'language':
                        self._language()
                    elif data == ('services', 'manage'):
                        self.stdscr.timeout(-1)
                        self._manage()
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

    def _render_journal(self, panel: JournalPanel, y: int, rows: int,
                        x: int, width: int, *, scroll: int = 0) -> None:
        """Render a bounded log viewport without calling journalctl in curses."""
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
        all_lines = list(panel.lines)
        end = max(0, len(all_lines) - scroll)
        start = max(0, end - rows)
        for j, text in enumerate(all_lines[start:end]):
            self._put(y + j, x, text[:width], 'muted')
        if scroll:
            self._put(y + rows - 1, x + max(0, width - 18),
                      f'↑ {scroll} / {len(all_lines)}', 'section')

    def _expanded_logs(self, systemd: SystemdServices, initial: ServiceState,
                       panel: JournalPanel) -> None:
        """Enter on the focused service expands its journal panel, not a popup."""
        state = initial
        scroll = 0
        last_refreshed = 0.0
        self.stdscr.timeout(150)
        try:
            while True:
                now = time.monotonic()
                panel.poll()
                if now - last_refreshed >= 3:
                    try:
                        state = systemd.status([state.name])[0]
                        self._update_cpu([state])
                    except ServiceError:
                        pass
                    last_refreshed = now
                frame = self._frame(f'{self.t("tui_detail_title")} / {state.name}',
                                    self.t('nbo_expanded_hint'))
                if frame:
                    h, w = frame
                    self._paint(3, 2, w-4, 7)
                    self._put(3, 3, state.unit, 'title')
                    self._put(4, 4, f'{self.t("tui_col_state")}: {state.active} ({state.substate})', state_style(state))
                    self._put(5, 4, f'{self.t("tui_col_startup")}: {state.enabled}   PID: {state.main_pid}', 'muted')
                    version = self.versions.get(state.name)
                    self._put(6, 4, f'{self.t("nbo_service_version")}: {version or "—"}', 'muted')
                    self._put(7, 4, f'CPU: {self.cpu_display.get(state.name,"—")}   '
                                       f'RAM: {self._human_size(state.memory_current)}   '
                                       f'{self.t("nbo_service_uptime")}: {self._uptime(state.uptime_seconds)}', 'muted')
                    self._put(10, 3, self.t('nbo_live_logs_title') if panel.live
                              else self.t('nbo_logs_title'), 'section')
                    self._put(10, min(40, w-22), f'{self.t("nbo_live_action")}: '
                              f'{"ON" if panel.live else "OFF"}',
                              'success' if panel.live else 'muted')
                    self._paint(11, 2, w-4, max(1, h-14))
                    self._render_journal(panel, 12, h-16, 4, w-10, scroll=scroll)
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                if key in (27, 3):
                    return
                if key in (ord('l'), ord('L')):
                    panel.set_live(not panel.live)
                    scroll = 0
                elif key in (ord('n'), ord('N')):
                    self.stdscr.timeout(-1)
                    count = self._read_number(panel.count)
                    self.stdscr.timeout(150)
                    if count is not None:
                        panel.count = count
                        panel.refresh()
                        scroll = 0
                elif key in (curses.KEY_F5, ord('r'), ord('R')):
                    panel.refresh()
                    last_refreshed = 0
                elif key in (curses.KEY_PPAGE, curses.KEY_UP):
                    scroll = min(max(0, len(panel.lines)-1), scroll + (10 if key == curses.KEY_PPAGE else 1))
                elif key in (curses.KEY_NPAGE, curses.KEY_DOWN):
                    scroll = max(0, scroll - (10 if key == curses.KEY_NPAGE else 1))
                elif key == curses.KEY_END:
                    scroll = 0
        finally:
            self.stdscr.timeout(200)

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
                frame = self._frame(self.t('tui_manage_title'), self.t('nbo_manage_hint_035'))
                if frame:
                    h, w = frame
                    split = max(31, min(44, w // 3))
                    labels = [self.t('action_' + action) for action in operations]
                    positions = self._backup_action_positions(labels, w - 10)
                    action_rows = max(row for _, row in positions) + 1
                    # Reserve the service list and operation bar first. On
                    # short terminals the journal preview disappears entirely.
                    actions_y = max(10, min(13 if h >= 27 else 11,
                                             h - 3 - action_rows))
                    self._paint(3, 2, split - 3, max(1, actions_y - 4))
                    self._paint(3, split + 1, w - split - 4, max(1, actions_y - 4))
                    self._put(3, 3, self.t('nbo_services_label'), 'section')
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
                    if actions_y > 12:
                        self._put(11, 4, f'{self.t("nbo_chosen")}: {len(chosen)}', 'muted')
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
                    for i, action in enumerate(operations):
                        ax, row = positions[i]
                        enabled = any(action_enabled(action, state) for state in targets)
                        style = ('selected' if focus == 'actions' and operation == i and enabled
                                 else ('title' if enabled else 'muted'))
                        self._put(actions_y + 1 + row, 4 + ax,
                                  f'[{labels[i]}]' if enabled else f' {labels[i]} ', style)

                    log_title_y = actions_y + action_rows + 3
                    if log_title_y <= h - 4:
                        self._put(log_title_y, 3,
                                  self.t('nbo_live_logs_title') if panel.live
                                  else self.t('nbo_logs_title'), 'section')
                        if preview is not None:
                            self._put(log_title_y, min(42, max(28, w // 2)),
                                      f'[{self.t("nbo_live_action")}: '
                                      f'{"ON" if panel.live else "OFF"}]',
                                      'success' if panel.live else 'muted')
                        logs_y = log_title_y + 1
                        logs_height = max(0, h - 3 - logs_y)
                        if logs_height:
                            self._paint(logs_y, 2, w - 4, logs_height)
                            if preview is None:
                                self._put(logs_y + 1, 4, self.t('nbo_select_to_preview'), 'muted')
                            else:
                                self._render_journal(panel, logs_y + 1,
                                                     max(0, logs_height - 1), 4, w - 10)
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                if key == 3:
                    return
                if key == 27:
                    if chosen:
                        chosen.clear()
                    elif focus is not None:
                        focus = None
                    else:
                        return
                elif key in (curses.KEY_F5, ord('r'), ord('R')):
                    refreshed = 0
                    if panel.name:
                        panel.refresh()
                elif key in (ord('l'), ord('L')) and preview:
                    panel.set_live(not panel.live)
                elif key == 9:
                    focus = 'services' if focus == 'actions' else 'actions'
                elif key in movement:
                    if focus is None:
                        focus = 'services'
                        index = len(states) - 1 if key in (curses.KEY_UP, ord('k')) else 0
                    elif focus == 'services':
                        index = move_selection(index, key, len(states))
                    else:
                        operation = (operation + (1 if key in (
                            curses.KEY_RIGHT, curses.KEY_DOWN, ord('j')) else -1)) % len(operations)
                elif focus == 'services' and key == 32 and states:
                    name = states[index].name
                    if name in chosen:
                        chosen.remove(name)
                    else:
                        chosen.add(name)
                elif key in (10, 13, curses.KEY_ENTER):
                    if focus == 'services' and states:
                        self._expanded_logs(systemd, states[index], panel)
                        self.stdscr.timeout(150)
                        refreshed = 0
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
                            refreshed = time.monotonic()
                        self.stdscr.timeout(150)
        finally:
            panel.close()
            self.stdscr.timeout(-1)

    def _confirm_token(self, title: str, prompt: str, token: str) -> bool:
        typed = ''
        while True:
            frame = self._frame(title, self.t('tui_confirm_hint'))
            if frame:
                h, w = frame
                for i, line in enumerate(textwrap.wrap(safe_text(prompt), width=max(15,w-10))[:5]):
                    self._put(4+i, 3, line, 'section')
                self._put(min(11,h-4), 3, f'{self.t("nbo_type_token")} {token}: {typed}', 'title')
                self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (27, 3):
                return False
            if key in (10,13,curses.KEY_ENTER):
                if typed == token:
                    return True
                self._show_lines(self.t('nbo_confirm_failed_title'),
                                 [(self.t('nbo_confirm_failed_message'), 'failure')],
                                 self.t('nbo_confirm_failed_hint'))
                return False
            if key in (8,127,curses.KEY_BACKSPACE):
                typed = typed[:-1]
            elif 32 <= key <= 126 and len(typed) < 48:
                typed += chr(key)

    def _run_backup_job(self, title: str, worker) -> bool:
        """Draw progress and keep the final tasks/results in that same panel.

        The bar counts completed stages or files; it does not estimate bytes.
        All curses access remains in the UI thread.
        """
        events: queue.SimpleQueue[tuple[str,int,int,str]] = queue.SimpleQueue()
        progress: tuple[str, int, int, str] = ('prepare', 0, 1, '')
        tick = 0
        self._backup_steps = []
        self._backup_started_counts = {}
        self.stdscr.timeout(150)
        result = None
        error = ''
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker, lambda name, done, total, detail='':
                                     events.put((name, done, total, detail)))
                while not future.done():
                    while not events.empty():
                        progress = events.get_nowait()
                        self._record_backup_step(progress)
                    self._draw_backup_progress(title, progress, tick)
                    tick += 1
                    self.stdscr.getch()  # No cancellation while a transaction runs.
                while not events.empty():
                    progress = events.get_nowait()
                    self._record_backup_step(progress)
                try:
                    result = future.result()
                except Exception as exc:
                    error = safe_text(exc)
                    if self._backup_steps:
                        name, _, info = self._backup_steps[-1]
                        self._backup_steps[-1] = (name, 'error', info)
        except Exception as exc:
            error = safe_text(exc)
            if self._backup_steps:
                name, _, info = self._backup_steps[-1]
                self._backup_steps[-1] = (name, 'error', info)
        finally:
            self.stdscr.timeout(-1)
        self._draw_backup_progress(title, progress, tick,
                                   finished=True, error=error, result=result)
        self._wait_result_ack()
        return not bool(error)

    def _record_backup_step(self, progress: tuple) -> None:
        """Keep an inspectable task timeline, including individual batch files."""
        phase, done, total, detail = progress
        file_step = phase in ('verify', 'delete') and bool(detail)
        key = f'{phase}:{detail}' if file_step else phase
        if not hasattr(self, '_backup_started_counts'):
            self._backup_started_counts = {}
        if not self._backup_steps or self._backup_steps[-1][0] != key:
            if self._backup_steps and self._backup_steps[-1][1] == 'running':
                old = self._backup_steps[-1]
                self._backup_steps[-1] = (old[0], 'done', old[2])
            self._backup_steps.append((key, 'running', detail))
            self._backup_started_counts[key] = done
        completed = (phase == 'complete' or
                     (file_step and done > self._backup_started_counts[key]))
        if completed:
            self._backup_steps[-1] = (key, 'done', detail)

    def _draw_backup_progress(self, title: str, progress: tuple, tick: int,
                              *, finished: bool = False, error: str = '',
                              result=None) -> None:
        frame = self._frame(title, self.t('nbo_progress_result_hint') if finished
                            else self.t('nbo_do_not_interrupt'))
        if not frame:
            return
        h, w = frame
        phase, done, total, detail = progress
        self._put(4, 3, self.t('nbo_stage_'+phase), 'section')
        length = max(10, min(50, w-20))
        if finished and not error:
            done = total
        filled = round(length * done / total) if total else 0
        self._put(7, 3, '['+'█'*filled+'░'*(length-filled)+'] '+f'{done}/{total}',
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
        for i, (name, step_status, info) in enumerate(self._backup_steps[-max(1,h-17):]):
            marker = 'OK' if step_status=='done' else ('!!' if step_status=='error' else '..')
            step_phase = name.split(':',1)[0]
            self._put(13+i, 4, f'{marker} {self.t("nbo_stage_"+step_phase)}'+
                      (f' — {info}' if info else '')[:max(0,w-35)],
                      'success' if step_status=='done' else
                      ('failure' if step_status=='error' else 'muted'))
        self.stdscr.refresh()

    @staticmethod
    def _backup_action_positions(labels: Sequence[str], width: int) -> list[tuple[int, int]]:
        """Lay out one horizontal operation bar, wrapping for narrow SSH terminals."""
        positions: list[tuple[int, int]] = []
        x = row = 0
        for label in labels:
            length = len(label) + 2
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
        if action in ('create', 'refresh'):
            return True
        if action == 'select_all':
            return count > 0
        if action == 'restore':
            return target_present and marked <= 1 and restorable
        if action == 'verify':
            return (marked > 0 or target_present) and verifiable
        if action == 'delete':
            return marked > 0 or target_present
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
            frame = self._frame(self.t('nbo_restore_choose'), self.t('nbo_restore_scope_hint'))
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
            if key in (27,3):
                return None
            if key in (curses.KEY_UP, curses.KEY_DOWN, ord('j'), ord('k')):
                if selected is None:
                    selected = 2 if key in (curses.KEY_UP, ord('k')) else 0
                else:
                    selected = (selected + (-1 if key in (curses.KEY_UP,ord('k')) else 1)) % len(kinds)
            elif key in (10,13,curses.KEY_ENTER) and selected is not None:
                return kinds[selected]

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
        backup_actions = ('create','verify','restore','delete','select_all','refresh')
        details: dict[tuple, object] = {}
        verified: set[tuple] = set()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='nbo-metadata')
        task = None
        task_key = None
        disk_info: tuple[int,int,int,int,int] | None = None
        disk_error = ''

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
            nonlocal entries, cursor, scroll
            selected = entries[cursor].path if cursor is not None and cursor < len(entries) else None
            entries = manager.quick_list()
            refresh_storage()
            cursor = next((i for i, e in enumerate(entries) if e.path == selected), None)
            chosen.intersection_update(e.path for e in entries)
            scroll = 0
            # Old entries remain cached only while the fingerprints still match.
            fresh = {fingerprint(e) for e in entries}
            for key in list(details):
                if key not in fresh:
                    details.pop(key)
                    verified.discard(key)

        self.stdscr.timeout(120)
        try:
            while True:
                if task is not None and task.done():
                    try:
                        details[task_key] = task.result()
                    except Exception:
                        details[task_key] = None
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
                detail = details.get(entry_key, preview) if preview else None
                ready = preview is not None and entry_key in details
                if len(marked) == 1 and fingerprint(marked[0]) not in details and task is None:
                    other = marked[0]
                    other_key = fingerprint(other)
                    task_key = other_key
                    task = pool.submit(manager.describe_one, other)
                # A legacy or corrupt archive is never presented as
                # restorable. Metadata must be loaded before enabling restore.
                action_targets = marked if marked else ([entry] if entry else [])
                incompatible = any(item.kind == 'legacy' or
                                   (item is not None and
                                    getattr(details.get(fingerprint(item)), 'legacy', False))
                                   for item in action_targets)
                can_restore = (len(action_targets) == 1 and
                               fingerprint(action_targets[0]) in details and
                               not incompatible)
                # Legacy archives may be verified read-only, but never restored.
                can_verify = bool(action_targets)
                frame = self._frame(self.t('nbo_backup_title'), self.t('nbo_backup_hint_034'))
                if frame:
                    h, w = frame
                    pane = w >= 104
                    split = min(max(60, w * 3 // 5), 93) if pane else w - 3

                    # One horizontal action bar, wrapping only when the window
                    # is too narrow; never overlap the footer or the inventory.
                    labels = [self.t('nbo_act_' + a) for a in backup_actions]
                    positions = self._backup_action_positions(labels, w - 8)
                    action_rows = max(row for _, row in positions) + 1
                    action_y = max(10, h - 5 - action_rows)
                    list_bottom = action_y - 2
                    show_storage = pane and split >= 57 and h >= 31 and action_y >= 25
                    chart_y = min(6 + min(13, len(entries)) + 2, action_y - 12) if show_storage else 0
                    self._paint(3, 2, split - 3, max(2, list_bottom - 2))
                    if pane:
                        self._paint(3, split + 1, w - split - 4, max(2, list_bottom - 2))
                    self._put(3, 3, self.t('nbo_backup_table'), 'section')
                    # Center each heading over its fixed-width data column.
                    show_tag = split >= 77
                    header = (f'{"":4}{self.t("nbo_type"):^7} {self.t("nbo_date"):^16} '
                              f'{self.t("nbo_size"):^10} ')
                    if show_tag:
                        header += f'{self.t("nbo_tag_header"):^12} '
                    remaining = max(8, split - 7 - len(header))
                    header += f'{self.t("nbo_name"):^{remaining}}'
                    self._put(4, 4, header[:max(8, split-7)], 'muted')
                    visible = max(1, min(action_y - 8,
                                         chart_y - 7 if show_storage else action_y - 8))
                    if cursor is not None:
                        if cursor < scroll:
                            scroll = cursor
                        elif cursor >= scroll + visible:
                            scroll = cursor - visible + 1
                    for i in range(scroll, min(len(entries), scroll + visible)):
                        item = entries[i]
                        row = 5 + i - scroll
                        selected = i == cursor and focus != 'actions'
                        if selected:
                            self._put(row, 3, ' ' * (split - 5), 'selected')
                        content = (f"[{'x' if item.path in chosen else ' '}] {item.kind:<7} "
                                   f'{self._pretty_date(item.created):<16} '
                                   f'{self._human_size(item.size):>10} ')
                        if show_tag:
                            tag = (self.t('nbo_pre_restore_short') if item.purpose=='pre_restore' else self.t('nbo_manual_short'))
                            content += f'{tag:<12} '
                        content += item.path.name
                        self._put(row, 4, content[:max(8, split - 7)],
                                  'selected' if selected else '')
                    if not entries:
                        self._put(6, 4, self.t('nbo_backup_empty'), 'muted')

                    if pane:
                        x = split + 2
                        right_width = max(10, w - x - 3)
                        self._put(3, x, self.t('nbo_detail_label'), 'section')
                        if preview is None:
                            self._put(5, x, self.t('nbo_backup_select_preview'), 'muted')
                        else:
                            kind_label = self.t('nbo_kind_' + preview.kind)
                            props = [
                                self.t('nbo_type') + ': ' + kind_label,
                                self.t('nbo_date') + ': ' + self._pretty_date(detail.created) + ' UTC',
                                self.t('nbo_size') + ': ' + self._human_size(preview.size),
                                (self.t('nbo_version') + ': ' + (detail.version or '—')
                                 if ready else self.t('nbo_metadata_loading')),
                                (self.t('nbo_legacy_blocked') if ready and detail.legacy else
                                 (self.t('nbo_integrity_ok') if entry_key in verified else
                                  self.t('nbo_integrity_not_checked'))),
                                self.t('nbo_contents') + ': ' +
                                self.t('nbo_kind_contents_' + preview.kind),
                                self.t('nbo_purpose') + ': ' + (self.t('nbo_pre_restore')
                                 if detail.purpose == 'pre_restore' else self.t('nbo_manual')),
                                self.t('nbo_name') + ':',
                            ]
                            y = 5
                            for j, value in enumerate(props):
                                for line in textwrap.wrap(safe_text(value), right_width,
                                                          break_long_words=True)[:2] or ['']:
                                    if y >= action_y - 2:
                                        break
                                    self._put(y, x, line,
                                              'failure' if j == 4 and ready and detail.legacy else 'muted')
                                    y += 1
                            for line in textwrap.wrap(preview.path.name, right_width,
                                                      break_long_words=True)[:5]:
                                if y >= action_y - 2:
                                    break
                                self._put(y, x, line, 'title')
                                y += 1

                    if show_storage and disk_info is not None:
                        self._put(chart_y - 1, 4, self.t('nbo_storage_label'), 'section')
                        self._draw_storage_ring(5, chart_y, disk_info)
                        bx = 25
                        used, other, free, total, count = disk_info
                        legends = ((self.t('nbo_storage_used'), used, 'disk_backup'),
                                   (self.t('nbo_storage_other'), other, 'disk_other'),
                                   (self.t('nbo_storage_free'), free, 'disk_free'))
                        for j, (label, value, color) in enumerate(legends):
                            self._put(chart_y + j + 1, bx,
                                      f'● {label}: {self._human_size(value)}'[:max(8, split-bx-2)],
                                      color)
                        self._put(chart_y + 5, bx,
                                  f'{self.t("nbo_storage_count")}: {count}', 'muted')
                    elif show_storage and disk_error:
                        self._put(chart_y, 4, disk_error[:max(10, split-7)], 'warning')

                    if pane and len(marked) >= 2 and action_y >= 25:
                        sy = min(20, action_y - 5)
                        if sy > 17:
                            self._put(sy, x, self.t('nbo_multi_title') +
                                      f' ({len(marked)})', 'section')
                            self._put(sy+1, x, f'{self.t("nbo_total_size")}: '
                                      f'{self._human_size(sum(e.size for e in marked))}', 'muted')
                            self._put(sy+2, x, self.t('nbo_multi_restore_disabled')[:right_width], 'warning')
                    self._put(action_y - 2, 3, f'{self.t("nbo_backup_count")}: {len(entries)}  |  '
                              f'{self.t("nbo_chosen")}: {len(chosen)}', 'muted')
                    self._put(action_y - 1, 3, '─' * (w - 7), 'muted')
                    self._put(action_y, 3, self.t('nbo_backup_actions_label'), 'section')
                    target = (marked[0] if len(marked) == 1 else entry) if len(marked) <= 1 else None
                    for i, action in enumerate(backup_actions):
                        ax, row = positions[i]
                        enabled = self._backup_action_enabled(action, len(entries),
                                                               len(marked), target is not None,
                                                               restorable=can_restore,
                                                               verifiable=can_verify)
                        style = ('selected' if focus == 'actions' and action_index == i and enabled
                                 else ('title' if enabled else 'muted'))
                        text = f'[{labels[i]}]' if enabled else f' {labels[i]} '
                        self._put(action_y + 1 + row, 4 + ax, text, style)
                        if focus == 'actions' and action_index == i and not enabled and ax > 0:
                            self._put(action_y + 1 + row, 3 + ax, '›', 'muted')
                    self.stdscr.refresh()
                key = self.stdscr.getch()
                if key == 3:
                    return
                if key == 27:
                    if chosen:
                        chosen.clear()
                    elif focus == 'actions':
                        focus = 'backups'
                    elif cursor is None:
                        return
                    else:
                        cursor = None
                        focus = None
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
                    try:
                        reload_inventory()
                    except (BackupError, OSError) as exc:
                        self._show_lines(self.t('tui_error_title'),[(safe_text(exc),'failure')])
                        self.stdscr.timeout(120)
                elif key in (curses.KEY_UP,curses.KEY_DOWN,ord('j'),ord('k'),
                             curses.KEY_LEFT,curses.KEY_RIGHT,
                             curses.KEY_HOME,curses.KEY_END,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    if focus == 'actions':
                        step = -1 if key in (curses.KEY_UP,ord('k'),curses.KEY_LEFT) else 1
                        action_index = (action_index+step) % len(backup_actions)
                    elif entries:
                        focus = 'backups'
                        if cursor is None:
                            cursor = len(entries)-1 if key in (curses.KEY_UP,ord('k')) else 0
                        else:
                            cursor = move_selection(cursor,key,len(entries))
                elif key in (10,13,curses.KEY_ENTER) and focus == 'actions':
                    action = backup_actions[action_index]
                    target_present = bool(marked) if len(marked) == 1 else (not marked and entry is not None)
                    if not self._backup_action_enabled(action, len(entries), len(marked),
                                                       target_present, restorable=can_restore,
                                                       verifiable=can_verify):
                        if action == 'restore' and (marked or entry is not None):
                            if len(marked) > 1:
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
                           'select_all':ord('*'),'refresh':curses.KEY_F5}[action]
                    if key == ord('*'):
                        chosen = (set() if len(chosen)==len(entries) else {e.path for e in entries})
                        continue
                    if key == curses.KEY_F5:
                        reload_inventory()
                        continue
                    # Continue through the same safe confirmation handlers as shortcuts.
                if key in (ord('c'),ord('C')):
                    self.stdscr.timeout(-1)
                    types = [('DB / PostgreSQL',''),('Media / uploads',''),('Full / DB + media','')]
                    i = self._choose(self.t('nbo_create_title'), types, self.t('tui_back_hint'))
                    if isinstance(i,int):
                        kind = ('db','media','full')[i]
                        if self._confirm_token(self.t('nbo_create_title'),
                                               self.t('nbo_confirm_create',kind=kind), 'CREATE'):
                            self._run_backup_job(self.t('nbo_create_title'),
                                                 lambda cb: manager.backup(kind,progress=cb))
                            reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('r'),ord('R')) and (entry is not None or len(chosen) == 1):
                    if len(chosen)>1:
                        continue  # Restore is disabled for a multi-selection.
                    if len(chosen)==1:
                        entry = marked[0]
                        entry_key = fingerprint(entry)
                        detail = details.get(entry_key, entry)
                        ready = entry_key in details
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
                    if entry.kind == 'full':
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
                                self.t('nbo_unknown_version_warning'), 'ACCEPT UNKNOWN VERSION'):
                            self.stdscr.timeout(120)
                            continue
                    token = f'RESTORE {entry.kind.upper()}' + (f' {only.upper()}' if entry.kind=='full' else '')
                    notice = (f'{entry.path.name} | {self._pretty_date(entry.created)} | '
                              f'{self._human_size(entry.size)}. '+self.t('nbo_restore_warning'))
                    if self._confirm_token(self.t('nbo_restore_choose'),notice,token):
                        def restore_worker(cb, item=entry, scope=only, unknown=accept_unknown):
                            opts = {'only': scope, 'progress': cb}
                            if unknown:
                                opts['accept_unknown'] = True
                            return manager.restore(item.kind, item.path, **opts)
                        self._run_backup_job(self.t('nbo_restore_choose'), restore_worker)
                        reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('d'),ord('D')) and (entry is not None or chosen):
                    targets = [e for e in entries if e.path in chosen] if chosen else [entry]
                    token = 'DELETE BACKUP' if len(targets)==1 else f'DELETE {len(targets)} BACKUPS'
                    self.stdscr.timeout(-1)
                    notice = self.t('nbo_delete_selected_warning', count=len(targets),
                                    size=self._human_size(sum(e.size for e in targets)))
                    if self._confirm_token(self.t('nbo_delete_title'), notice, token):
                        self._run_backup_job(self.t('nbo_delete_title'),
                             lambda cb, paths=[e.path for e in targets]:
                             self._delete_selected_job(manager, paths, cb))
                        reload_inventory()
                    self.stdscr.timeout(120)
                elif key in (ord('v'),ord('V')) and (entry is not None or chosen):
                    targets = [e for e in entries if e.path in chosen] if chosen else [entry]
                    self.stdscr.timeout(-1)
                    if self._run_backup_job(self.t('nbo_verify_title'),
                                            lambda cb, selected=targets: self._verify_job_many(manager, selected, cb)):
                        verified.update(fingerprint(e) for e in targets)
                    self.stdscr.timeout(120)
                # Enter on the file list no longer opens a separate detail
                # screen. The right-hand panel is the only preview.
        finally:
            self.stdscr.timeout(-1)
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _verify_job_many(manager, entries, cb):
        cb('verify', 0, len(entries), '')
        for i, entry in enumerate(entries):
            cb('verify', i, len(entries), entry.path.name)
            manager.verify(entry.kind, entry.path)
            cb('verify', i+1, len(entries), entry.path.name)
        cb('complete', len(entries), len(entries), '')
        return f'{len(entries)} backups verified'

    @staticmethod
    def _delete_selected_job(manager, paths, cb):
        count, freed = manager.delete_backups(paths, progress=cb)
        return f'{count} backups deleted; {freed} bytes released'

    @staticmethod
    def _verify_job(manager, entry, cb):
        cb('verify',0,1,'')
        manager.verify(entry.kind,entry.path)
        cb('complete',1,1,'')
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
        if initial_view == 'manage':
            self._manage()
            return 0
        if initial_view == 'backups':
            self._backups()
            return 0
        return self._main()


def run_tui(t: Translator, config_path: Path = CONFIG_PATH, initial_view: str = 'main') -> int:
    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        pass
    return curses.wrapper(lambda stdscr: CursesMenu(stdscr, t, config_path).run(initial_view))
