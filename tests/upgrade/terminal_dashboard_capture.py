"""Capture the real curses dashboard using offline, synthetic plan data.

Run from the repository root in a terminal:
    python -m tests.upgrade.terminal_dashboard_capture /tmp/dashboard.json en
Set NO_COLOR=1 for the monochrome variant. No NetBox or network access.
"""
from __future__ import annotations

import curses
import json
import sys
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netbox_ops.i18n import Translator
from netbox_ops.plugin_compatibility import CompatibilityEvidence
from netbox_ops.tui import CursesMenu
from netbox_ops.upgrade_dashboard import UpgradeDashboard
from netbox_ops.upgrade_targets import TargetAssessment, TargetRow, TargetSnapshot
from tests.upgrade.test_upgrade_ui import UpgradeTuiTests


def capture(screen, language):
    fixture = UpgradeTuiTests()
    fixture.setUp()
    try:
        menu = CursesMenu(screen, Translator(language))
        menu._init_screen()
        dashboard = UpgradeDashboard(menu)
        dashboard.cfg = fixture.cfg
        dashboard.model = fixture._target_model()
        dashboard.plan = replace(fixture.build(), blockers=('Incompatible plugin: requires NetBox 4.7.0',))
        dashboard.plan_selection = dashboard.model.selection
        rows = (TargetRow(None, '4.4.0', '4.4.2', TargetAssessment('supported', 'verified')),
                *(TargetRow(f'plugin-{i:02}-long-name', '1.0.0', '2.0.0',
                            TargetAssessment('warning', 'verified')) for i in range(1, 25)))
        dashboard.cursor = 24
        result = []
        with patch.object(dashboard, '_rows', return_value=rows):
            for h, w in ((45, 160), (30, 120), (24, 80), (16, 50), (45, 160)):
                curses.resizeterm(h, w)
                assert dashboard._draw()
                assert dashboard.cursor == 24 and not dashboard._enabled('execute')
                result.append(dict(scenario='blocked', height=h, width=w, color=bool(menu.colors),
                                   lines=[screen.instr(y, 0).decode('utf-8', errors='replace').rstrip()
                                          for y in range(h)]))
        dashboard.model.reset()
        dashboard._invalidate_plan()
        dashboard.cursor = dashboard.scroll = 0
        request = dashboard.model.request('assess')
        warning = TargetAssessment('warning', 'installed', (
            CompatibilityEvidence('PyPI', 'Release metadata unavailable', 'unknown'),
        ))
        rows = tuple(TargetRow(row.component, row.current, row.current, warning)
                     for row in dashboard._rows())
        dashboard.snapshot = TargetSnapshot(request, request.selection, rows, 'warning', (), False)
        for h, w in ((30, 120), (16, 50)):
            curses.resizeterm(h, w)
            assert dashboard._draw()
            assert not dashboard._enabled('validate') and not dashboard._enabled('execute')
            result.append(dict(scenario='unchanged', height=h, width=w, color=bool(menu.colors),
                               lines=[screen.instr(y, 0).decode('utf-8', errors='replace').rstrip()
                                      for y in range(h)]))
        dashboard._discovery_screen = 'full'
        dashboard.future = Future()
        dashboard._discovery_states = {'inventory': 'done', 'releases': 'running',
                                       'suggestions': 'pending', 'assessment': 'pending'}
        for h, w in ((30, 120), (16, 50), (30, 120)):
            curses.resizeterm(h, w)
            assert dashboard._draw()
            result.append(dict(scenario='discovery', height=h, width=w, color=bool(menu.colors),
                               lines=[screen.instr(y, 0).decode('utf-8', errors='replace').rstrip()
                                      for y in range(h)]))
        # Read actual curses cell attributes, not only the FakeScreen calls.
        # Compare low/peak phases at the same size so only the marker can vary.
        curses.resizeterm(30, 120)
        for instant in (0.2, 1.4):
            with patch('netbox_ops.tui.time.monotonic', return_value=instant):
                assert dashboard._draw()
            result.append(dict(scenario='pulse', height=30, width=120, color=bool(menu.colors),
                               lines=[screen.instr(y, 0).decode('utf-8', errors='replace').rstrip()
                                      for y in range(30)],
                               attributes=[[screen.inch(y, x) & curses.A_COLOR for x in range(119)]
                                           for y in range(30)]))
        return result
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    output = Path(sys.argv[1])
    output.write_text(json.dumps(curses.wrapper(capture, sys.argv[2]), ensure_ascii=False),
                      encoding='utf-8')
