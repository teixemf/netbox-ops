"""Version-target upgrade management; the shared planner owns execution."""
from __future__ import annotations

import curses
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from typing import TYPE_CHECKING

from .config import load_config
from .tui import move_action_focus, move_selection, safe_text
from .upgrade_targets import (
    MAX_PICKER_ROWS, TargetAssessment, TargetCatalogue, TargetModel, TargetRow, TargetSnapshot,
    discover_target_context,
)
from .upgrade_plan import UpgradePlanError
from .upgrade_tui import netbox_overview
from .upgrade_ui import make_plan, summary_lines

if TYPE_CHECKING:
    from .tui import CursesMenu

_ENTER = (10, 13, curses.KEY_ENTER)
_MOVE = (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END,
         curses.KEY_NPAGE, curses.KEY_PPAGE, ord('j'), ord('k'))
_ACTIONS = ('suggest', 'reset', 'validate', 'refresh', 'execute')
_STYLES = {'supported': 'success', 'warning': 'warning', 'conflict': 'failure', 'unchecked': 'muted'}


def _clip(value, width):
    value = safe_text(value)
    return value if len(value) <= width else value[:max(0, width - 1)] + '…'


class UpgradeDashboard:
    """UI-thread state and one bounded read-only worker per discovered session."""

    def __init__(self, menu: CursesMenu):
        self.menu = menu
        self.t = menu.t
        self.title = self.t('ut_title')
        self.cfg = None
        self.model = None
        self.snapshot = None
        self.plan = None
        self.plan_selection = None
        self.suggested = None
        self.future = None
        self.cancel = Event()
        self.error = ''
        self.cursor = self.scroll = self.action = 0
        self.actions_focused = False
        self.positions = []

    def _invalidate_plan(self):
        self.plan = self.plan_selection = None

    def _rows(self):
        if self.snapshot is not None and self.snapshot.selection == self.model.selection:
            return self.snapshot.rows
        current = dict(self.model.installed.plugins)
        selection = self.model.selection
        unchecked = TargetAssessment('unchecked', 'unknown', pending=self.future is not None)
        return (TargetRow(None, self.model.installed.netbox, selection.netbox, unchecked),
                *(TargetRow(name, current[name], version, unchecked) for name, version in selection.plugins))

    def _plan_matches(self):
        if self.plan is None or self.plan_selection != self.model.selection:
            return False
        operation, target, pins = self.model.selection.plan_arguments(self.model.installed)
        return (operation is not None
                and self.plan.current_netbox == self.model.installed.netbox
                and self.plan.target_netbox == (target or self.model.installed.netbox)
                and dict(self.plan.selected_plugins) == pins)

    def _enabled(self, action):
        if self.future is not None:
            return False
        if action == 'execute':
            return self._plan_matches() and self.plan.can_execute
        if action == 'validate':
            return self.model.selection != self.model.installed
        return True

    def _discover(self):
        self._invalidate_plan()
        self.snapshot = self.suggested = None
        self.cfg = load_config(self.menu.config_path)
        context = self.menu._upgrade_job(self.title, lambda _: discover_target_context(self.cfg))
        if context is None:
            return False
        self.model = self.model.refreshed(context) if self.model else TargetModel(context)
        self.cursor = min(self.cursor, len(self.model.installed.plugins))
        self.error = ''
        return True

    def _start(self, pool, action):
        self._invalidate_plan()
        self.snapshot = None
        self.error = ''
        request = self.model.request(action)
        self.future = pool.submit(copy_context().run, self.model.evaluate, request,
                                  cancelled=self.cancel.is_set)

    def _poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        try:
            result = future.result()
            if isinstance(result, TargetSnapshot) and self.model.accept(result):
                self.snapshot = result
                if result.request.action == 'suggest':
                    self.suggested = result.selection
        except CancelledError:
            pass
        except Exception:
            self.error = self.t('upgrade_discovery_error')

    def _status(self, assessment):
        if assessment.status == 'unchecked' and self.future is not None:
            return self.t('ut_checking')
        return self.t('ut_' + assessment.status)

    def _name(self, component):
        return component or 'NetBox'

    def _row_text(self, values, widths):
        return ' '.join(_clip(value, width).ljust(width) for value, width in zip(values, widths))

    def _plan_lines(self):
        selection = self.model.selection
        operation, target, pins = selection.plan_arguments(self.model.installed)
        plan = self.plan if self._plan_matches() else None
        overview = netbox_overview(self.model.context, target, pins, plan, self.t,
                                   operation=operation or 'plugins')
        if operation is None:
            overview[1] = (self.t('ut_no_changes'), 'muted')
        preliminary = (self.snapshot.status if self.snapshot is not None else 'unchecked')
        rows = [overview[1], (self.t('ut_preliminary_status', status=self.t('ut_' + preliminary)),
                              _STYLES[preliminary]), overview[0], overview[2]]
        if plan:
            # Show actual stage and dependency outcomes, with the full evidence in D.
            reviews = {review.version: review for review in plan.dependency_reviews}
            for stage in plan.stages:
                review = reviews.get(stage.netbox_version)
                status = review.resolution.status if review else 'not-run'
                if review and status == 'passed' and not review.resolution.checked_core:
                    status = 'unknown'
                rows.append((self.t('upgrade_dependency_status', version=stage.netbox_version,
                                    status=self.t('upgrade_dependency_' + status)),
                             {'passed': 'success', 'blocked': 'failure'}.get(status, 'warning')))
        return [rows[1], rows[2], rows[3], rows[0], *rows[4:]]

    def _component_lines(self, row):
        return [(self._name(row.component), 'title'),
                (f'{self.t("ut_col_current")}: {row.current}', 'muted'),
                (f'{self.t("ut_col_target")}: {row.target}', 'muted'),
                (self._status(row.assessment), _STYLES[row.assessment.status]),
                *((f'{item.source}: {item.detail}',
                   {'blocks': 'failure', 'warns': 'warning', 'unknown': 'warning'}.get(item.status, 'muted'))
                  for item in row.assessment.evidence)]

    def _compact_component_lines(self, row, width):
        lines = self._component_lines(row)
        evidence = lines[4:]
        result = lines[:4]
        # Put blocking/uncertain reasons before ordinary supporting evidence.
        attention = [line for line in evidence if line[1] in ('warning', 'failure')]
        if attention:
            result.append(attention[0])
        checks = row.assessment.prerequisites
        if checks and width >= 34:
            headers = [self.t('ut_col_status'), self.t('ut_col_component'),
                       self.t('ut_col_current'), self.t('ut_col_required')]
            status_width = max(len(headers[0]), len(self.t('ut_warning')), len(self.t('ut_conflict')))
            current_width = max(len(headers[2]), 7)
            required_width = max(len(headers[3]), 8)
            name_width = width - status_width - current_width - required_width - 3
            if name_width >= 8:
                widths = (status_width, name_width, current_width, required_width)
                result.append((self._row_text(headers, widths), 'muted'))
                # The full stage-specific explanations remain in D.
                for stage, item in sorted(checks, key=lambda check: check[1].status == 'ok'):
                    state = {'ok': 'supported', 'blocked': 'conflict', 'unknown': 'warning'}[item.status]
                    name = item.name if len({version for version, _ in checks}) == 1 else f'{stage}: {item.name}'
                    result.append((self._row_text((self.t('ut_' + state), name,
                                                   item.installed or '?', item.required), widths),
                                   _STYLES[state]))
        if not checks:
            result.extend(line for line in evidence if line not in attention[:1])
        return result

    def _details(self, row):
        lines = [(self.t('ut_preliminary'), 'section'), *self._component_lines(row)]
        if self.snapshot:
            lines.extend((warning, 'warning') for warning in self.snapshot.warnings)
        if self.error:
            lines.append((self.error, 'failure'))
        lines.extend([(self.t('ut_plan'), 'section'), *self._plan_lines()])
        if self._plan_matches():
            lines.extend((line, '') for line in summary_lines(self.plan, self.t, details=True))
        self.menu._show_lines(self.title, lines, allow_quit=True)

    def _table_widths(self, rows, width):
        t = self.t
        names = [t('ut_group_core'), t('ut_group_plugins'),
                 *('  ' + self._name(row.component) for row in rows)]
        headers = [t('ut_col_' + key) for key in ('current', 'target', 'status')]
        minimum = [max(8, len(t('ut_group_plugins'))), len(headers[0]), len(headers[1]), len(headers[2])]
        preferred = [max(map(len, names)), max(len(headers[0]), *(len(row.current) for row in rows)),
                     max(len(headers[1]), *(len(row.target) + 1 for row in rows)),
                     max(len(headers[2]), *(len(self._status(row.assessment)) for row in rows))]
        widths = [max(low, high) for low, high in zip(minimum, preferred)]
        gap = 3 if sum(widths) + 9 <= width else 1
        excess = max(0, sum(widths) + 3 * gap - width)
        for index in (0, 1, 2, 3):
            reduction = min(excess, widths[index] - minimum[index])
            widths[index] -= reduction
            excess -= reduction
        return widths, gap

    @staticmethod
    def _visible_components(rows, start, budget):
        """Group labels consume screen rows, not selectable component indices."""
        visible = []
        remaining = budget
        for index in range(start, len(rows)):
            cost = 3 if index == 1 and start == 0 else 1
            if remaining < cost:
                break
            visible.append(index)
            remaining -= cost
        return visible

    def _draw(self):
        menu, t = self.menu, self.t
        hint = t('ut_hint') if menu.stdscr.getmaxyx()[1] >= 95 else t('ut_hint_short')
        frame = menu._frame(self.title, hint)
        if not frame:
            return False
        h, w = frame
        width = w - 6
        rows = self._rows()
        self.cursor = min(self.cursor, len(rows) - 1)
        compact = w < 76
        labels = [t('ut_action_' + a + ('_short' if compact else '')) for a in _ACTIONS]
        self.positions = menu._backup_action_positions(labels, width)
        action_height = 2 + self.positions[-1][1]
        gap = 1 if h >= 22 else 0
        side_by_side = w >= 82 and h >= 21
        detail_height = min(12, max(5, (h - 12) // 2)) if side_by_side else 2
        budget = max(1, h - 2 - 3 - 3 - action_height - detail_height - 2 * gap)
        self.scroll = max(0, min(self.scroll, self.cursor, len(rows) - 1))
        visible = self._visible_components(rows, self.scroll, budget)
        while self.cursor not in visible:
            self.scroll += 1
            visible = self._visible_components(rows, self.scroll, budget)
        # Reclaim earlier rows when a resize gives us more room.
        while self.scroll > 0:
            earlier = self._visible_components(rows, self.scroll - 1, budget)
            if self.cursor not in earlier:
                break
            self.scroll -= 1
            visible = earlier
        table_rows = len(visible) + (2 if 0 in visible and 1 in visible else 0)
        counts_y = 5 + table_rows
        action_y = counts_y + 1 + gap
        detail_y = action_y + action_height + gap
        menu._paint(3, 2, w - 4, counts_y - 2)
        menu._paint(action_y, 2, w - 4, action_height)
        menu._put(3, 3, t('ut_available'), 'section')
        widths, column_gap = self._table_widths(rows, width)
        offsets = [0]
        for cell in widths[:-1]:
            offsets.append(offsets[-1] + cell + column_gap)
        table_width = offsets[-1] + widths[-1]
        group = t('ut_group_core' if self.scroll == 0 else 'ut_group_plugins')
        menu._put(4, 3, group, 'section')
        for i, key in enumerate(('current', 'target', 'status'), 1):
            menu._put(4, 3 + offsets[i], t('ut_col_' + key), 'muted')
        y = 5
        for index in visible:
            row = rows[index]
            if index == 1 and self.scroll == 0:
                y += 1
                menu._put(y, 3, t('ut_group_plugins'), 'section')
                y += 1
            selected = index == self.cursor and not self.actions_focused
            target = ('*' if row.target != row.current else '') + row.target
            values = ('  ' + self._name(row.component), row.current, target, self._status(row.assessment))
            if selected:
                menu._put(y, 3, ' ' * table_width, 'selected')
            for i, value in enumerate(values):
                style = 'selected' if selected else ''
                if i == 3 and (not selected or menu.colors):
                    style = _STYLES[row.assessment.status]
                menu._put(y, 3 + offsets[i], _clip(value, widths[i]), style)
            y += 1
        count = sum(row.current != row.target for row in rows)
        note = (t('ut_checking') if self.future is not None else t('ut_partial')
                if self.snapshot and self.snapshot.partial else t('ut_details_key')
                if self.error or self.snapshot and self.snapshot.warnings else t('ut_preliminary_short'))
        counts = t('ut_counts', count=len(rows), changed=count)
        menu._put(counts_y, 3, _clip(f'{counts} | {note}', width), 'muted')
        menu._put(action_y, 3, t('ut_operations'), 'section')
        for i, (x, line) in enumerate(self.positions):
            enabled = self._enabled(_ACTIONS[i])
            label = f'[{labels[i]}]' if enabled else f' {labels[i]} '
            focused = self.actions_focused and i == self.action
            menu._put(action_y + 1 + line, 3 + x, label,
                      'selected' if focused else 'title' if enabled else 'muted')
        row = rows[self.cursor]
        if detail_y < h - 2:
            if side_by_side:
                split = w // 2
                component = self._compact_component_lines(row, split - 5)
                plan = self._plan_lines()
                height = min(h - 2 - detail_y, max(len(component), len(plan)) + 1, detail_height)
                menu._paint(detail_y, 2, split - 3, height)
                menu._paint(detail_y, split + 1, w - split - 3, height)
                menu._put(detail_y, 3, t('ut_component_details'), 'section')
                menu._put(detail_y, split + 2, t('ut_plan'), 'section')
                for i, (line, style) in enumerate(component[:height - 1]):
                    menu._put(detail_y + 1 + i, 3, _clip(line, split - 5), style)
                for i, (line, style) in enumerate(plan[:height - 1]):
                    menu._put(detail_y + 1 + i, split + 2, _clip(line, w - split - 5), style)
            else:
                menu._paint(detail_y, 2, w - 4, min(2, h - 2 - detail_y))
                menu._put(detail_y, 3, _clip(t('ut_details_key') + ': ' + self._name(row.component), width), 'section')
                if detail_y + 1 < h - 2:
                    line, style = self._plan_lines()[3]
                    menu._put(detail_y + 1, 3, _clip(line, width), style)
        menu.stdscr.refresh()
        return True

    def _picker(self, component):
        menu, t = self.menu, self.t
        offset = cursor = 0
        scroll = 0
        selected = self.model.selection.netbox if component is None else dict(self.model.selection.plugins)[component]
        suggested = (self.suggested.netbox if component is None else dict(self.suggested.plugins).get(component)
                     ) if self.suggested else None
        catalogue = None
        title = t('ut_picker', component=self._name(component))
        while True:
            if catalogue is None:
                request = self.model.request('catalogue', component, offset=offset)
                result = menu._upgrade_job(title, lambda _: self.model.evaluate(request))
                if not isinstance(result, TargetCatalogue) or not self.model.accept(result):
                    return None
                catalogue = result
                if not catalogue.rows:
                    menu._show_lines(title, [(line, 'warning') for line in catalogue.warnings],
                                     allow_quit=True)
                    return None
                cursor = min(cursor, len(catalogue.rows) - 1)
            frame = menu._frame(title, t('ut_picker_hint'))
            if frame:
                h, w = frame
                width = w - 6
                first = min(17, max(12, width // 3))
                compatibility = max(12, len(t('ut_col_compatibility')))
                widths = (first, compatibility, width - first - compatibility - 2)
                visible = max(1, h - 7)
                scroll = max(0, min(scroll, len(catalogue.rows) - visible))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + visible:
                    scroll = cursor - visible + 1
                menu._paint(3, 2, w - 4, h - 5)
                menu._put(3, 3, self._row_text(
                    (t('ut_col_version'), t('ut_col_compatibility'), t('ut_col_notes')), widths), 'muted')
                for i, row in enumerate(catalogue.rows[scroll:scroll + visible], scroll):
                    notes = [t('ut_note_' + name) for name, match in (
                        ('installed', row.target == row.current), ('latest', row.target == catalogue.latest),
                        ('suggested', row.target == suggested), ('target', row.target == selected)) if match]
                    if row.assessment.availability == 'unavailable':
                        notes.append(t('ut_unavailable'))
                    y = 4 + i - scroll
                    if i == cursor:
                        menu._put(y, 3, ' ' * width, 'selected')
                    menu._put(y, 3, self._row_text((row.target, self._status(row.assessment),
                                                   ', '.join(notes)), widths),
                              'selected' if i == cursor else _STYLES[row.assessment.status])
                menu._put(h - 3, 3, _clip(f'{offset + cursor + 1}/{catalogue.total} '
                                          + (t('ut_partial') if catalogue.partial else t('ut_details_key')), width), 'muted')
                menu.stdscr.refresh()
            key = menu.stdscr.getch()
            menu._check_quit(key)
            if key in (27, 3):
                return None
            if not frame:
                continue
            if key in _ENTER:
                row = catalogue.rows[cursor]
                if row.assessment.availability == 'unavailable':
                    menu._show_lines(title, [(t('ut_unavailable'), 'warning')], allow_quit=True)
                    continue
                return row.target
            if key in (ord('d'), ord('D')):
                row = catalogue.rows[cursor]
                menu._show_lines(title, [*self._component_lines(row),
                                        *((line, 'warning') for line in catalogue.warnings)], allow_quit=True)
            elif key == curses.KEY_F5:
                catalogue = None
            elif key in _MOVE:
                position = offset + cursor
                if key == curses.KEY_NPAGE:
                    position = min(catalogue.total - 1, position + MAX_PICKER_ROWS)
                elif key == curses.KEY_PPAGE:
                    position = max(0, position - MAX_PICKER_ROWS)
                else:
                    position = move_selection(position, key, catalogue.total)
                new_offset = position // MAX_PICKER_ROWS * MAX_PICKER_ROWS
                cursor = position - new_offset
                if new_offset != offset:
                    offset, catalogue, scroll = new_offset, None, 0

    def _validate(self):
        self._invalidate_plan()
        try:
            operation, target, pins = self.model.selection.plan_arguments(self.model.installed)
        except UpgradePlanError as exc:
            self.menu._show_lines(self.title, [(safe_text(exc), 'failure')])
            return
        if operation is None:
            return
        selection = self.model.selection
        self.plan = self.menu._upgrade_job(self.title, lambda _: make_plan(self.model.context, operation, target, pins))
        self.plan_selection = selection if self.plan is not None else None
        if self.plan is not None and not self._plan_matches():
            self._invalidate_plan()
            self.menu._show_lines(self.title, [(self.t('ut_plan_mismatch'), 'failure')])

    def run(self):
        menu = self.menu
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nbo-upgrade-targets')
        try:
            if not self._discover():
                return
            self._start(pool, 'suggest')
            while True:
                self._poll()
                menu.stdscr.timeout(150 if self.future is not None else -1)
                frame = self._draw()
                key = menu.stdscr.getch()
                menu._check_quit(key)
                if key in (27, 3):
                    if self.actions_focused and key == 27:
                        self.actions_focused = False
                        continue
                    return
                if not frame or key == -1:
                    continue
                if key in (9, curses.KEY_BTAB):
                    self.actions_focused = not self.actions_focused
                    self.action = 0
                elif key in (ord('d'), ord('D')):
                    if self.future is None:
                        self._details(self._rows()[self.cursor])
                elif key in (ord('p'), ord('P')):
                    if self.future is None and menu._review_plugin_pins(self.title, self.model.context.inventory):
                        if not self._discover():
                            return
                        self._start(pool, 'assess')
                elif key == curses.KEY_F5:
                    if self.future is None:
                        if not self._discover():
                            return
                        self._start(pool, 'assess')
                elif key in _ENTER:
                    if self.future is not None:
                        continue
                    if not self.actions_focused:
                        component = self._rows()[self.cursor].component
                        version = self._picker(component)
                        if version is not None:
                            try:
                                self.model.select(component, version)
                            except UpgradePlanError as exc:
                                menu._show_lines(self.title, [(safe_text(exc), 'failure')])
                                continue
                            self.suggested = None
                            self._start(pool, 'assess')
                    else:
                        action = _ACTIONS[self.action]
                        if not self._enabled(action):
                            continue
                        if action == 'suggest':
                            self._start(pool, 'suggest')
                        elif action == 'reset':
                            self.model.reset()
                            self.suggested = None
                            self._start(pool, 'assess')
                        elif action == 'validate':
                            self._validate()
                        elif action == 'refresh':
                            if not self._discover():
                                return
                            self._start(pool, 'assess')
                        elif action == 'execute':
                            operation, _, _ = self.model.selection.plan_arguments(self.model.installed)
                            if menu._execute_reviewed_upgrade(self.title, self.cfg, operation, self.plan):
                                return
                elif self.actions_focused:
                    self.action = move_action_focus(self.action, key, self.positions)
                    if key == curses.KEY_HOME:
                        self.action = 0
                    elif key == curses.KEY_END:
                        self.action = len(_ACTIONS) - 1
                elif key in _MOVE:
                    self.cursor = move_selection(self.cursor, key, len(self._rows()))
        except Exception:
            menu.stdscr.timeout(-1)
            menu._show_lines(self.title, [(self.t('upgrade_discovery_error'), 'failure')])
        finally:
            self.cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
            menu.stdscr.timeout(-1)
