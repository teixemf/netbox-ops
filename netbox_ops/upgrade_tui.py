"""Read-only summaries for the persistent upgrade TUI views."""
from __future__ import annotations

from .i18n import Translator
from .upgrade_plan import UpgradePlan
from .upgrade_ui import UpgradeContext


def netbox_overview(context: UpgradeContext, target: str | None,
                    selected: dict[str, str | None], plan: UpgradePlan | None,
                    t: Translator, *, operation: str) -> list[tuple[str, str]]:
    """Show actual observations and reviewed evidence, never speculative success."""
    current = context.installation.version
    wanted = current if operation == 'plugins' else target or '?'
    if plan is None:
        status = 'not_checked'
        style = 'muted'
    elif not plan.can_execute:
        status = 'blocked'
        style = 'failure'
    else:
        status = plan.status
        style = 'warning' if plan.requires_strong_confirmation else 'success'
    rows = [
        (t('upgrade_versions', current=current, target=wanted), 'section'),
        (t('upgrade_plan_status', status=t('upgrade_status_' + status)), style),
        (t('upgrade_overview_counts', stages=len(plan.stages) if plan else '?',
           selected=len(selected), configured=len(context.plugins)), 'muted'),
        (f'Python: {context.installation.python_version}; PostgreSQL: {context.postgres or "?"}; '
         f'Redis: {context.redis or "?"}', 'muted'),
    ]
    if context.plugin_selection_incomplete:
        rows.append((t('upgrade_plugin_selection_warning'), 'warning'))
    if plan:
        reviews = {r.version: r for r in plan.dependency_reviews}
        for index, stage in enumerate(plan.stages, 1):
            rows.append((t('upgrade_stage', index=index, total=len(plan.stages),
                           version=stage.netbox_version), 'section'))
            for prerequisite in stage.prerequisites:
                status = prerequisite.status
                rows.append((f'  {prerequisite.name}: {prerequisite.installed or "?"} / '
                             f'{prerequisite.required} [{status}]',
                             {'ok': 'success', 'blocked': 'failure'}.get(status, 'warning')))
            for check in stage.plugin_checks:
                rows.append((f'  {check.package}=={check.version} [{check.status}]',
                             {'compatible': 'success', 'blocked': 'failure'}.get(check.status, 'warning')))
            review = reviews.get(stage.netbox_version)
            dependency = review.resolution.status if review else 'not-run'
            rows.append((t('upgrade_dependency_status', version=stage.netbox_version,
                           status=t('upgrade_dependency_' + dependency)),
                         {'passed': 'success', 'blocked': 'failure'}.get(dependency, 'warning')))
            if review and review.ltree != 'not-required':
                rows.append((t('upgrade_ltree_status', status=t('upgrade_ltree_' + review.ltree)),
                             'success' if review.ltree in ('installed', 'available') else 'warning'))
        planned = {p.package: p for p in plan.plugins}
    else:
        planned = {}
    for name, installed in context.plugins:
        if name not in selected:
            rows.append((f'{name}: {installed or "?"} ({t("upgrade_unchanged")})', 'muted'))
        else:
            proposed = planned[name].proposed_version if name in planned else selected[name]
            version = proposed or (t('upgrade_latest') if plan is None else '?')
            suffix = f' ({t("upgrade_status_not_checked")})' if plan is None else ''
            rows.append((f'{name}: {installed or "?"} -> {version}{suffix}', 'title'))
    return rows
