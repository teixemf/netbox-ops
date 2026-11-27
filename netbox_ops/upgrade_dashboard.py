"""Version-target upgrade management; the shared planner owns execution."""
from __future__ import annotations

import curses
from queue import SimpleQueue
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from typing import TYPE_CHECKING

from .config import load_config
from .plugin_inventory import PluginInventoryError, inventory_plugins
from .tui import move_action_focus, move_selection, safe_text
from .upgrade_targets import (
    MAX_PICKER_ROWS, TargetAssessment, TargetCatalogue, TargetModel, TargetRow, TargetSnapshot,
    discover_target_context,
)
from .upgrade_plan import UpgradePlanError
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
        self._drawn_actions = _ACTIONS
        self._validation_selection = None
        self._inventory_valid = True
        self._discovery_action = None
        self._discovery_states = {}
        self._discovery_events = SimpleQueue()
        self._preview_context = None
        self._revalidate_after_pins = False

    def _invalidate_plan(self):
        self.plan = self.plan_selection = None

    def _rows(self):
        if not self._inventory_valid or self.model is None:
            assessment = TargetAssessment('unchecked', 'unknown', pending=self.future is not None)
            context = self._preview_context
            if context is None:
                return (TargetRow(None, '?', '?', assessment),)
            return (TargetRow(None, context.installation.version, '?', assessment),
                    *(TargetRow(name, version, '?', assessment)
                      for name, version in sorted(context.plugins) if isinstance(version, str)))
        if self.snapshot is not None and self.snapshot.selection == self.model.selection:
            return self.snapshot.rows
        current = dict(self.model.installed.plugins)
        selection = self.model.selection
        unchecked = TargetAssessment('unchecked', 'unknown', pending=self.future is not None)
        return (TargetRow(None, self.model.installed.netbox, selection.netbox, unchecked),
                *(TargetRow(name, current[name], version, unchecked) for name, version in selection.plugins))

    def _plan_matches(self):
        if (not self._inventory_valid or self.model is None or self.plan is None
                or self.plan_selection != self.model.selection):
            return False
        operation, target, pins = self.model.selection.plan_arguments(self.model.installed)
        return (operation is not None
                and self.plan.current_netbox == self.model.installed.netbox
                and self.plan.target_netbox == (target or self.model.installed.netbox)
                and dict(self.plan.selected_plugins) == pins)

    def _actions(self):
        return (*_ACTIONS[:-1], 'pins', _ACTIONS[-1]) if self._correctable_pins() else _ACTIONS

    def _enabled(self, action):
        if self.future is not None:
            return False
        if self.model is None or not self._inventory_valid:
            return action == 'refresh'
        if action == 'execute':
            return self._plan_matches() and self.plan.can_execute
        if action == 'pins':
            return bool(self._correctable_pins())
        if action == 'validate':
            return self.model.selection != self.model.installed
        return True

    def _discover(self, pool, action='assess'):
        """Start read-only discovery without leaving the management dashboard."""
        self._invalidate_plan()
        self.snapshot = self.suggested = None
        self._inventory_valid = False
        self._preview_context = None
        self.error = ''
        self._discovery_action = action
        self._discovery_states = {'inventory': 'pending', 'releases': 'pending',
                                  'suggestions': 'pending' if action == 'suggest' else 'not_applicable',
                                  'assessment': 'pending'}
        self._discovery_events = SimpleQueue()

        def worker():
            cfg = load_config(self.menu.config_path)
            context = discover_target_context(cfg, progress=self._discovery_event,
                                              cancelled=self.cancel.is_set)
            return cfg, context

        self.future = pool.submit(copy_context().run, worker)

    def _discovery_event(self, task, state, context):
        # Only the UI thread consumes these events or updates the model.
        self._discovery_events.put((task, state, context))

    def _drain_discovery(self):
        while not self._discovery_events.empty():
            task, state, context = self._discovery_events.get_nowait()
            self._discovery_states[task] = state
            if context is not None:
                self._preview_context = context

    def _start(self, pool, action):
        self._invalidate_plan()
        self.snapshot = None
        self.error = ''
        request = self.model.request(action)
        if not self._discovery_states:
            self._discovery_states = {'suggestions': 'pending' if action == 'suggest' else 'not_applicable',
                                      'assessment': 'pending'}
        else:
            self._discovery_states.update(suggestions='pending' if action == 'suggest' else 'not_applicable',
                                          assessment='pending')
        self.future = pool.submit(copy_context().run, self.model.evaluate, request,
                                  cancelled=self.cancel.is_set, progress=self._discovery_event)

    def _poll(self, pool=None):
        self._drain_discovery()
        while self.future is not None and self.future.done():
            future, self.future = self.future, None
            discovering = self._discovery_action is not None
            if self._validation_selection is not None:
                selection, self._validation_selection = self._validation_selection, None
                try:
                    self._accept_validation(future.result(), selection)
                except Exception as exc:
                    self._invalidate_plan()
                    self.error = f'{self.t("ut_validation_error")} {safe_text(exc)}'
                continue
            try:
                result = future.result()
                self._drain_discovery()
                if discovering:
                    self.cfg, context = result
                    action, self._discovery_action = self._discovery_action, None
                    self._discovery_states.update(
                        inventory='partial' if context.plugin_selection_incomplete else 'done',
                        releases='done' if context.tags else 'failed')
                    self.model = self.model.refreshed(context) if self.model else TargetModel(context)
                    self._inventory_valid = True
                    self._preview_context = None
                    self.cursor = min(self.cursor, len(self.model.installed.plugins))
                    self._start(pool, action)
                elif isinstance(result, TargetSnapshot) and self.model.accept(result):
                    self.snapshot = result
                    if result.request.action == 'suggest':
                        self.suggested = result.selection
                    if result.partial and all(state != 'partial' for state in self._discovery_states.values()):
                        self._discovery_states['assessment'] = 'partial'
                    if (not result.partial
                            and all(state in ('done', 'not_applicable')
                                    for state in self._discovery_states.values())):
                        self._discovery_states.clear()
                else:
                    # Never report a rejected/stale request as completed discovery.
                    self.error = self.t('upgrade_discovery_error')
                    for task in ('suggestions', 'assessment'):
                        if self._discovery_states.get(task) != 'not_applicable':
                            self._discovery_states[task] = 'not_run'
            except CancelledError:
                self._discovery_action = None
                self.error = self.t('upgrade_discovery_error')
                for task, state in self._discovery_states.items():
                    if state in ('running', 'pending'):
                        self._discovery_states[task] = 'not_run'
            except Exception:
                self._discovery_action = None
                self.error = self.t('upgrade_discovery_error')
                running = [task for task, state in self._discovery_states.items() if state == 'running']
                for task in running or ['inventory' if discovering else 'assessment']:
                    self._discovery_states[task] = 'failed'
                for task, state in self._discovery_states.items():
                    if state == 'pending':
                        self._discovery_states[task] = 'not_run'

    def _status(self, assessment):
        if assessment.status == 'unchecked' and self.future is not None:
            return self.t('ut_checking')
        return self.t('ut_' + assessment.status)

    def _name(self, component):
        return component or 'NetBox'

    def _row_text(self, values, widths):
        return ' '.join(_clip(value, width).ljust(width) for value, width in zip(values, widths))

    def _readiness(self):
        if self._validation_selection is not None:
            return self.t('ut_validating'), 'title'
        if self.model is not None and self._inventory_valid:
            if self.model.selection == self.model.installed:
                return self.t('ut_no_changes'), 'muted'
            if self._plan_matches():
                status = self.plan.status
                if not self.plan.can_execute:
                    status = 'blocked'
                return (self.t('upgrade_plan_status', status=self.t('upgrade_status_' + status)),
                        {'ready': 'success', 'blocked': 'failure', 'needs_confirmation': 'warning'}[status])
        return self.t('upgrade_plan_status', status=self.t('upgrade_status_not_checked')), 'muted'

    def _plan_lines(self):
        t = self.t
        lines = [self._readiness()]
        if not self._plan_matches():
            if self._enabled('validate'):
                lines.append((t('ut_validate_next'), 'muted'))
            return lines
        plan = self.plan
        if plan.blockers or plan.warnings:
            lines.append((t('ut_required_actions'), 'section'))
            lines.extend((blocker, 'failure') for blocker in dict.fromkeys(plan.blockers))
            if self._correctable_pins():
                lines.append((t('ut_pin_review_from_plan'), 'title'))
            lines.extend((warning, 'warning') for warning in dict.fromkeys(plan.warnings))
        lines.append((t('ut_validation'), 'section'))
        reviews = {review.version: review for review in plan.dependency_reviews}
        for stage in plan.stages:
            review = reviews.get(stage.netbox_version)
            status = review.resolution.status if review else 'not-run'
            if review and status == 'passed' and not review.resolution.checked_core:
                status = 'unknown'
            lines.append((t('upgrade_dependency_status', version=stage.netbox_version,
                            status=t('upgrade_dependency_' + status)),
                          {'passed': 'success', 'blocked': 'failure', 'unknown': 'warning'}.get(status, 'muted')))
            for item in stage.prerequisites:
                lines.append((f'{stage.netbox_version}: {item.name}: {item.installed or "?"} / {item.required} '
                              f'[{t("ut_check_" + item.status)}]',
                              {'ok': 'success', 'blocked': 'failure', 'unknown': 'warning'}[item.status]))
            if review and review.ltree != 'not-required':
                lines.append((t('upgrade_ltree_status', status=t('upgrade_ltree_' + review.ltree)),
                              'success' if review.ltree in ('installed', 'available') else 'warning'))
        return lines

    def _overview_lines(self):
        """Always expose overall readiness before allocating space to evidence."""
        t = self.t
        lines = [self._readiness()]
        if self.error:
            lines.append((self.error, 'failure'))
        elif self._plan_matches():
            plan = self.plan
            if plan.blockers:
                lines.append((plan.blockers[0], 'failure'))
            elif plan.warnings:
                lines.append((plan.warnings[0], 'warning'))
            elif plan.can_execute:
                lines.append((t('ut_review_next'), 'muted'))
            if self._correctable_pins():
                lines.append((t('ut_pin_review_from_plan'), 'title'))
        elif self.snapshot is not None and self.model is not None and self.snapshot.selection == self.model.selection:
            status = self.snapshot.status
            preliminary = (t('ut_preliminary_status', status=t('ut_' + status)), _STYLES[status])
            for row in self.snapshot.rows:
                reasons = [e for e in row.assessment.evidence if e.status == 'blocks']
                if reasons:
                    lines.append((f'{self._name(row.component)}: {reasons[0].detail}', 'failure'))
                    break
            else:
                if self.snapshot.warnings:
                    lines.append((self.snapshot.warnings[0], 'warning'))
            lines.append(preliminary)
            if self._enabled('validate'):
                lines.append((t('ut_validate_next'), 'muted'))
        elif self._enabled('validate'):
            lines.append((t('ut_validate_next'), 'muted'))
        return lines

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
        t = self.t
        lines = [(t('ut_available'), 'section')]
        for component in self._rows():
            if component.current != component.target:
                lines.append((f'{self._name(component.component)}: {component.current} → {component.target} '
                              f'[{self._status(component.assessment)}]', _STYLES[component.assessment.status]))
        lines.extend([(t('ut_plan'), 'section'), *self._plan_lines()])
        if not self._plan_matches():
            lines.extend(self._overview_lines()[1:])
        if self._plan_matches():
            for review in self.plan.dependency_reviews:
                for item in review.local_diagnostics:
                    lines.append((t('upgrade_local_diagnostic', name=item.name, declared=item.declaration,
                                    installed=item.installed or '?', stage=item.stage,
                                    reason=t('upgrade_local_' + item.reason),
                                    action=t('upgrade_local_action_' + item.action.replace('-', '_'))), 'failure'))
        if self.snapshot:
            lines.extend((warning, 'warning') for warning in self.snapshot.warnings)
        if self.error:
            lines.append((self.error, 'failure'))
        lines.append((t('ut_evidence_next'), 'muted'))
        if self._correctable_pins():
            return self.menu._show_lines(self.title, lines, t('ut_pin_details_hint'), allow_quit=True)
        self.menu._show_lines(self.title, lines, allow_quit=True)
        return False

    def _evidence(self, row):
        lines = [(self.t('ut_preliminary'), 'section'), *self._component_lines(row)]
        if self._plan_matches():
            lines.extend([(self.t('ut_plan'), 'section'),
                          *((line, '') for line in summary_lines(self.plan, self.t, details=True))])
        self.menu._show_lines(self.title, lines, allow_quit=True)

    def _correctable_pins(self):
        if not self._plan_matches():
            return set()
        diagnostics = [item for review in self.plan.dependency_reviews for item in review.local_diagnostics]
        manual = {item.name for item in diagnostics if item.action != 'review-pin'}
        return {item.name for item in diagnostics if item.action == 'review-pin'} - manual

    def _review_plan_pins(self, pool):
        menu = self.menu
        names = self._correctable_pins()
        context = self.model.context
        if not names:
            if self._plan_matches() and any(review.local_diagnostics for review in self.plan.dependency_reviews):
                menu._show_lines(self.title, [(self.t('upgrade_local_action_manual_review'), 'warning')],
                                 allow_quit=True)
                return
            inventory = context.inventory
        else:
            try:
                inventory = inventory_plugins(context.installation, local_pin_names=tuple(sorted(names)))
            except (PluginInventoryError, OSError, UnicodeError):
                menu._show_lines(self.title, [(self.t('upgrade_discovery_error'), 'failure')])
                return
            if (inventory.requirements_hash, inventory.enabled, inventory.packages) != (
                    context.inventory.requirements_hash, context.inventory.enabled, context.inventory.packages):
                self._invalidate_plan()
                menu._show_lines(self.title, [(self.t('ut_plan_mismatch'), 'failure')])
                self._discover(pool)
                return
        if menu._review_plugin_pins(self.title, inventory, eligible=names or None):
            self._revalidate_after_pins = self._plan_matches()
            self._invalidate_plan()
            self._discover(pool)

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
        actions = self._actions()
        previous = self._drawn_actions[min(self.action, len(self._drawn_actions) - 1)]
        self.action = actions.index(previous) if previous in actions else actions.index('validate')
        self._drawn_actions = actions
        labels = [t('ut_action_' + a + ('_short' if compact else '')) for a in actions]
        self.positions = menu._backup_action_positions(labels, width)
        action_height = 2 + self.positions[-1][1]
        gap = 1 if h >= 22 else 0
        side_by_side = w >= 100 and h >= 24
        discovery = (bool(self._discovery_states) and self._validation_selection is None
                     and not self._plan_matches())
        discovery_lines = self._discovery_lines(width, compact=h < 21 or width < 74) if discovery else []
        detail_height = (len(discovery_lines) + 1 if discovery else
                         min(12, max(5, (h - 12) // 2)) if side_by_side else 3)
        # Reserve operations, readiness/evidence and footer before table rows.
        budget = max(1, h - 8 - action_height - detail_height - 2 * gap)
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
            target = ('*' if row.target != '?' and row.target != row.current else '') + row.target
            values = ('  ' + self._name(row.component), row.current, target, self._status(row.assessment))
            if selected:
                menu._put(y, 3, ' ' * table_width, 'selected')
            for i, value in enumerate(values):
                style = 'selected' if selected else ''
                if i == 3 and (not selected or menu.colors):
                    style = _STYLES[row.assessment.status]
                menu._put(y, 3 + offsets[i], _clip(value, widths[i]), style)
            y += 1
        count = sum(row.target != '?' and row.current != row.target for row in rows)
        note = (t('ut_checking') if self.future is not None else t('ut_discovery_retry_short')
                if discovery else t('ut_partial')
                if self.snapshot and self.snapshot.partial else t('ut_details_key')
                if self.error or self.snapshot and self.snapshot.warnings else t('ut_preliminary_short'))
        if self._validation_selection is not None:
            note = t('ut_validating')
        counts = (t('ut_counts', count=len(rows), changed=count) if self._inventory_valid and self.model
                  else t('ut_inventory_pending'))
        position = f'{self.cursor + 1}/{len(rows)} | ' if len(visible) < len(rows) else ''
        menu._put(counts_y, 3, _clip(f'{position}{counts} | {note}', width), 'muted')
        menu._put(action_y, 3, t('ut_operations'), 'section')
        for i, (x, line) in enumerate(self.positions):
            enabled = self._enabled(actions[i])
            label = f'[{labels[i]}]' if enabled else f' {labels[i]} '
            focused = self.actions_focused and i == self.action
            menu._put(action_y + 1 + line, 3 + x, label,
                      'selected' if focused else 'title' if enabled else 'muted')
        row = rows[self.cursor]
        if detail_y < h - 2:
            if discovery:
                menu._paint(detail_y, 2, w - 4, min(detail_height, h - 2 - detail_y))
                menu._put(detail_y, 3, t('ut_discovery_heading'), 'section')
                for y, (line, style) in enumerate(discovery_lines, detail_y + 1):
                    if y < h - 2:
                        menu._put(y, 3, _clip(line, width), style)
            elif side_by_side:
                split = w // 2
                component = self._compact_component_lines(row, split - 5)
                plan = self._overview_lines()
                plan.extend(line for line in self._plan_lines()[1:] if line not in plan)
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
                height = min(detail_height, h - 2 - detail_y)
                menu._paint(detail_y, 2, w - 4, height)
                menu._put(detail_y, 3, _clip(t('ut_plan') + ' | ' + t('ut_details_key'), width), 'section')
                lines = self._overview_lines()
                for i, (line, style) in enumerate(lines[:height - 1]):
                    menu._put(detail_y + 1 + i, 3, _clip(line, width), style)
        menu.stdscr.refresh()
        return True

    def _discovery_lines(self, width, *, compact):
        styles = {'done': 'success', 'running': 'title', 'failed': 'failure',
                  'partial': 'warning'}
        tasks = [(task, state) for task, state in self._discovery_states.items()
                 if state != 'not_applicable']
        if not compact:
            labels = [((self.t('nbo_task_state_' + state) if state != 'partial'
                        else self.t('ut_discovery_partial')) if state != 'pending' else '',
                       self.t('ut_discovery_' + task), styles.get(state, 'muted'))
                      for task, state in tasks]
            state_width = max((len(state) for state, _, _ in labels), default=0)
            # Reserve the column even when every task is still unstarted.
            state_width = max(state_width, *(len(self.t('nbo_task_state_' + state))
                                             for state in ('running', 'done', 'failed', 'not_run')))
            lines = [(f'{state:<{state_width}}  {task}', style) for state, task, style in labels]
            if self.future is None:
                lines.append((self.t('ut_discovery_retry'), 'warning'))
            return lines
        # Compact layout keeps unstarted tasks visible without a status label.
        labels = [((self.t('ut_discovery_state_' + state) + ' ') if state != 'pending' else '')
                  + self.t('ut_discovery_short_' + task) for task, state in tasks]
        return [(' | '.join(labels[i:i + 2]), 'muted')
                for i in range(0, len(labels), 2)]

    def _picker_notes(self, row, latest, suggested, selected):
        """Prioritize actual reasons over release markers, without coloring the row."""
        t = self.t
        notes = []
        if row.target == row.current:
            notes.append(f'{row.current} ({t("ut_note_installed")})')
        if row.assessment.availability == 'unavailable':
            notes.append(t('ut_unavailable'))
        reasons = sorted((item for item in row.assessment.evidence
                          if item.status in ('blocks', 'warns', 'unknown')),
                         key=lambda item: {'blocks': 0, 'warns': 1, 'unknown': 2}[item.status])
        if reasons:
            notes.append(f'{reasons[0].source}: {reasons[0].detail}')
        for name, match in (('latest', row.target == latest),
                            ('suggested', row.target == suggested),
                            ('target', row.target == selected)):
            if match:
                notes.append(t('ut_note_' + name))
        return ' | '.join(notes)

    def _draw_picker(self, title, rows, cursor, scroll, latest, suggested, selected,
                     busy, warnings, *, leaving=False):
        menu, t = self.menu, self.t
        hint = t('ut_picker_hint')
        if sum(len(part) + 2 for part in hint.split(' | ')) > menu.stdscr.getmaxyx()[1] - 3:
            hint = t('ut_picker_hint_short')
        frame = menu._frame(title, hint)
        if not frame:
            return None, scroll, 1
        h, w = frame
        width = w - 6
        labels = [t('ut_keep_installed') if row.target == row.current else row.target for row in rows]
        headers = (t('ut_col_version'), t('ut_col_compatibility'), t('ut_col_notes'))
        status_width = max(len(headers[1]), *(len(t('ut_' + state)) for state in _STYLES),
                           len(t('ut_checking')))
        version_width = min(max(len(headers[0]), *map(len, labels)),
                            max(len(headers[0]), width - status_width - 12))
        if version_width < len(labels[0]):
            labels[0] = t('ut_keep_installed_short')
        notes_width = width - version_width - status_width - 2
        # Keep a compact selected reason below the table on taller screens.
        detail = h >= 20
        visible = max(1, h - 7 - (2 if detail else 0))
        scroll = max(0, min(scroll, len(rows) - visible))
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + visible:
            scroll = cursor - visible + 1
        shown = min(visible, len(rows) - scroll)
        count_y = 4 + shown
        menu._paint(3, 2, w - 4, shown + 2 + (2 if detail else 0))
        menu._put(3, 3, self._row_text(headers, (version_width, status_width, notes_width)), 'muted')
        for index in range(scroll, scroll + shown):
            row = rows[index]
            y = 4 + index - scroll
            label = _clip(labels[index], version_width)
            # Only the VERSION cell carries focus. The status retains its own
            # semantic foreground and neutral notes remain on the panel.
            menu._put(y, 3, label.ljust(version_width) if index == cursor else label,
                      'selected' if index == cursor else 'title' if index == 0 else '')
            status = t('ut_' + row.assessment.status)
            menu._put(y, 4 + version_width, _clip(status, status_width), _STYLES[row.assessment.status])
            notes = self._picker_notes(row, latest, suggested, selected)
            menu._put(y, 5 + version_width + status_width, _clip(notes, notes_width))
        note = t('ut_picker_returning') if leaving else t('ut_checking') if busy else (
            t('ut_picker_discovery_warning') if warnings else t('ut_details_key'))
        menu._put(count_y, 3, _clip(f'{cursor + 1}/{len(rows)} | {note}', width), 'muted')
        if detail:
            menu._put(count_y + 1, 3, t('ut_component_details'), 'section')
            row = rows[cursor]
            notes = self._picker_notes(row, latest, suggested, selected)
            menu._put(count_y + 2, 3, _clip(f'{row.target} | {notes or t("ut_" + row.assessment.status)}', width))
        menu.stdscr.refresh()
        return frame, scroll, visible

    def _picker(self, component, pool=None):
        """One continuous catalogue; only the selected batch is evaluated lazily."""
        menu, t = self.menu, self.t
        own_pool = pool is None
        if own_pool:
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nbo-upgrade-picker')
        current = self.model.installed.netbox if component is None else dict(self.model.installed.plugins)[component]
        selected = self.model.selection.netbox if component is None else dict(self.model.selection.plugins)[component]
        suggested = (self.suggested.netbox if component is None else dict(self.suggested.plugins).get(component)
                     ) if self.suggested else None
        rows = [TargetRow(component, current, current, TargetAssessment('unchecked', 'installed'))]
        title = t('ut_picker', component=self._name(component))
        cursor = scroll = 0
        latest = None
        warnings = ()
        attempted = set()
        cancel = Event()
        future = None
        request = None
        choice = None
        listing = True
        restore_version = None

        def submit(action, offset=0):
            nonlocal request, future
            request = self.model.request(action, component, offset=offset)
            future = pool.submit(copy_context().run, self.model.evaluate, request, cancelled=cancel.is_set)

        try:
            submit('listing')
            while True:
                if future is not None and future.done():
                    finished, future = future, None
                    try:
                        result = finished.result()
                        if not isinstance(result, TargetCatalogue) or not self.model.accept(result):
                            return None
                        warnings = tuple(dict.fromkeys((*warnings, *result.warnings)))
                        if request.action in ('listing', 'relist'):
                            rows = list(result.rows) or rows
                            latest = result.latest
                            listing = False
                            cursor = next((i for i, row in enumerate(rows) if row.target == restore_version), 0)
                            restore_version = None
                            if choice is not None:
                                cursor = next((i for i, row in enumerate(rows) if row.target == choice), cursor)
                        else:
                            # The catalogue can shrink after a failed release lookup.
                            # Merge by exact version, never by an assumed row index.
                            assessed = {row.target: row for row in result.rows}
                            rows = [assessed.get(row.target, row) for row in rows]
                            attempted.add(request.offset)
                    except Exception:
                        warnings = (*warnings, t('upgrade_discovery_error'))
                        if listing:
                            rows = [TargetRow(component, current, current, TargetAssessment('unchecked', 'installed'))]
                            cursor = scroll = 0
                            latest = None
                            listing = False
                        else:
                            attempted.add(request.offset)
                if future is None and not listing:
                    batch = cursor // MAX_PICKER_ROWS * MAX_PICKER_ROWS
                    if batch not in attempted:
                        submit('catalogue', batch)
                    elif choice is not None:
                        row = rows[cursor]
                        if row.target == choice and row.assessment.availability != 'unavailable':
                            return choice
                        choice = None
                        if row.assessment.availability == 'unavailable':
                            menu.stdscr.timeout(-1)
                            menu._show_lines(title, [(t('ut_unavailable'), 'warning')], allow_quit=True)
                menu.stdscr.timeout(150 if future is not None else -1)
                frame, scroll, visible = self._draw_picker(title, rows, cursor, scroll, latest,
                                                          suggested, selected, future is not None, warnings)
                key = menu.stdscr.getch()
                menu._check_quit(key)
                if key in (27, 3):
                    return None
                if not frame or key == -1:
                    continue
                if key in _ENTER:
                    if rows[cursor].assessment.availability == 'unavailable':
                        menu.stdscr.timeout(-1)
                        menu._show_lines(title, [(t('ut_unavailable'), 'warning')], allow_quit=True)
                    else:
                        choice = rows[cursor].target
                elif key in (ord('d'), ord('D')):
                    menu.stdscr.timeout(-1)
                    menu._show_lines(title, [*self._component_lines(rows[cursor]),
                                            *((line, 'warning') for line in warnings)], allow_quit=True)
                elif key == curses.KEY_F5 and future is None:
                    attempted.clear()
                    warnings = ()
                    choice = None
                    # Refresh release metadata and retry evidence in this inventory session.
                    listing = True
                    restore_version = rows[cursor].target
                    rows = [TargetRow(component, current, current, TargetAssessment('unchecked', 'installed'))]
                    cursor = scroll = 0
                    submit('relist')
                elif key in _MOVE:
                    choice = None
                    if key == curses.KEY_NPAGE:
                        cursor = min(len(rows) - 1, cursor + visible)
                    elif key == curses.KEY_PPAGE:
                        cursor = max(0, cursor - visible)
                    else:
                        cursor = move_selection(cursor, key, len(rows))
        finally:
            cancel.set()
            if future is not None:
                # This model's caches have one owner. Do not return to a caller
                # that could mutate/evaluate them while a lookup is still running.
                if not future.done():
                    self._draw_picker(title, rows, cursor, scroll, latest, suggested, selected,
                                      True, warnings, leaving=True)
                try:
                    future.result()
                except Exception:
                    pass
            if own_pool:
                pool.shutdown(wait=False, cancel_futures=True)
            menu.stdscr.timeout(-1)

    def _accept_validation(self, plan, selection):
        self.plan = plan
        self.plan_selection = selection if plan is not None else None
        if plan is None:
            self.error = self.t('ut_validation_error')
        elif not self._plan_matches():
            self._invalidate_plan()
            self.error = self.t('ut_plan_mismatch')

    def _validate(self, pool=None):
        self._invalidate_plan()
        self.error = ''
        try:
            operation, target, pins = self.model.selection.plan_arguments(self.model.installed)
        except UpgradePlanError as exc:
            self.error = safe_text(exc)
            return
        if operation is None:
            return
        selection, context = self.model.selection, self.model.context
        if pool is None:
            # Synchronous invocation retained for offline tests and the VM harness.
            self._accept_validation(self.menu._upgrade_job(
                self.title, lambda _: make_plan(context, operation, target, pins)), selection)
            return
        self._validation_selection = selection
        self.future = pool.submit(copy_context().run, make_plan, context, operation, target, pins)

    def run(self):
        menu = self.menu
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nbo-upgrade-targets')
        try:
            self._discover(pool, 'suggest')
            self._draw()
            while True:
                self._poll(pool)
                if self.future is None and self._revalidate_after_pins:
                    self._revalidate_after_pins = False
                    if self._inventory_valid and self.model is not None and not self.error:
                        self._validate(pool)
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
                    if self.future is None and self._details(self._rows()[self.cursor]):
                        self._review_plan_pins(pool)
                elif key in (ord('e'), ord('E')):
                    if self.future is None:
                        self._evidence(self._rows()[self.cursor])
                elif key in (ord('p'), ord('P')):
                    if self.future is None and self._inventory_valid and self.model is not None:
                        self._review_plan_pins(pool)
                elif key == curses.KEY_F5:
                    if self.future is None:
                        self._discover(pool, 'assess' if self.model else 'suggest')
                elif key in _ENTER:
                    if self.future is not None:
                        continue
                    if not self.actions_focused:
                        if not self._inventory_valid or self.model is None:
                            continue
                        component = self._rows()[self.cursor].component
                        version = self._picker(component, pool)
                        if version is not None:
                            try:
                                self.model.select(component, version)
                            except UpgradePlanError as exc:
                                menu._show_lines(self.title, [(safe_text(exc), 'failure')])
                                continue
                            self.suggested = None
                            self._start(pool, 'assess')
                    else:
                        action = self._actions()[self.action]
                        if not self._enabled(action):
                            continue
                        if action == 'suggest':
                            self._start(pool, 'suggest')
                        elif action == 'reset':
                            self.model.reset()
                            self.suggested = None
                            self._start(pool, 'assess')
                        elif action == 'validate':
                            self._validate(pool)
                        elif action == 'refresh':
                            self._discover(pool, 'assess' if self.model else 'suggest')
                        elif action == 'pins':
                            self._review_plan_pins(pool)
                        elif action == 'execute':
                            operation, _, _ = self.model.selection.plan_arguments(self.model.installed)
                            if menu._execute_reviewed_upgrade(self.title, self.cfg, operation, self.plan):
                                return
                elif self.actions_focused:
                    self.action = move_action_focus(self.action, key, self.positions)
                    if key == curses.KEY_HOME:
                        self.action = 0
                    elif key == curses.KEY_END:
                        self.action = len(self._actions()) - 1
                elif key in _MOVE:
                    self.cursor = move_selection(self.cursor, key, len(self._rows()))
        except Exception:
            menu.stdscr.timeout(-1)
            menu._show_lines(self.title, [(self.t('upgrade_discovery_error'), 'failure')])
        finally:
            self.cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
            menu.stdscr.timeout(-1)
