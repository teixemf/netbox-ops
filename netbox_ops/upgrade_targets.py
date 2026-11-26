"""Read-only exact upgrade intent and bounded preliminary evidence for the TUI.

A TargetModel belongs to one discovered inventory. Only its worker evaluates
requests; the UI owns request/edit/accept and replaces the model on refresh.
Neither suggestions nor preliminary support authorize execution.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Callable
from urllib.error import URLError

from .config import Configuration
from .plugin_compatibility import (
    CompatibilityError, CompatibilityEvidence, ReleaseCatalog, _release_key,
    discover_stable_releases, fetch_pypi_json, fetch_release_declaration, review_plugin_upgrade,
)
from .upgrade_discovery import check_prerequisites
from .upgrade_execution import UpgradeExecutionError
from .upgrade_plan import UpgradePlanError, _number, required_netbox_path
from .upgrade_ui import UpgradeContext, discover_context, official_tags

# One evaluation is bounded; the caller can continue with another request.
# A documentation lookup may try two release tags.
MAX_REMOTE_LOOKUPS = 12
MAX_PICKER_ROWS = 8


class _BudgetExhausted(Exception):
    pass


class _Budget:
    def __init__(self, limit: int):
        self.remaining = limit
        self.failed = set()

    def take(self, cost: int) -> None:
        if self.remaining < cost:
            raise _BudgetExhausted
        self.remaining -= cost


@dataclass(frozen=True)
class TargetSelection:
    netbox: str
    plugins: tuple[tuple[str, str], ...]

    def plan_arguments(self, installed: TargetSelection) -> tuple[str | None, str | None, dict[str, str]]:
        """Derive existing planner mode and only the exact changed plugin pins."""
        current = dict(installed.plugins)
        proposed = dict(self.plugins)
        if current.keys() != proposed.keys() or len(proposed) != len(self.plugins):
            raise UpgradePlanError('Target selection does not match configured inventory')
        if _number(self.netbox) < _number(installed.netbox):
            raise UpgradePlanError('NetBox downgrades are not supported')
        changed = {}
        for package, target in proposed.items():
            if target == current[package]:
                continue
            original_key, target_key = _release_key(current[package]), _release_key(target)
            if original_key is None or target_key is None or target_key <= original_key:
                raise UpgradePlanError('Select a stable newer plugin release')
            changed[package] = target
        netbox = self.netbox if self.netbox != installed.netbox else None
        operation = ('stack' if netbox and changed else 'netbox' if netbox
                     else 'plugins' if changed else None)
        return operation, netbox, changed


@dataclass(frozen=True)
class TargetAssessment:
    status: str  # supported | warning | conflict | unchecked
    availability: str  # installed | verified | unavailable | unknown
    evidence: tuple[CompatibilityEvidence, ...] = ()
    pending: bool = False


@dataclass(frozen=True)
class TargetRow:
    component: str | None  # None is NetBox, otherwise an eligible distribution
    current: str
    target: str
    assessment: TargetAssessment


@dataclass(frozen=True)
class TargetRequest:
    session: object
    revision: int
    selection: TargetSelection
    action: str  # assess | suggest | catalogue
    component: str | None = None
    offset: int = 0
    limit: int = MAX_PICKER_ROWS


@dataclass(frozen=True)
class TargetSnapshot:
    request: TargetRequest
    selection: TargetSelection
    rows: tuple[TargetRow, ...]
    status: str
    warnings: tuple[str, ...]
    partial: bool


@dataclass(frozen=True)
class TargetCatalogue:
    request: TargetRequest
    rows: tuple[TargetRow, ...]
    total: int
    latest: str | None
    warnings: tuple[str, ...]
    partial: bool


@dataclass(frozen=True)
class _SuggestionProgress:
    offset: int = 0
    uncertain: str | None = None
    unresolved_newer: bool = False
    choice: str | None = None


def discover_target_context(cfg: Configuration) -> UpgradeContext:
    """Keep installed inventory available if official release discovery fails."""
    context = discover_context(cfg, releases=False)
    try:
        tags = official_tags()
    except UpgradeExecutionError:
        tags = ()
    return replace(context, tags=tags)


def _evidence(source: str, detail: str, status: str) -> CompatibilityEvidence:
    # Evidence is untrusted text, not terminal markup or an instruction.
    def clean(value):
        return ''.join(c if c.isprintable() else ' ' for c in str(value))[:512]
    return CompatibilityEvidence(clean(source), clean(detail), status)


def _status(items) -> str:
    statuses = set(items)
    for status in ('conflict', 'warning', 'unchecked'):
        if status in statuses:
            return status
    return 'supported'


def _unchecked(detail='Metadata lookup budget reached; continue or retry') -> TargetAssessment:
    return TargetAssessment('unchecked', 'unknown', (_evidence('Discovery', detail, 'unknown'),), True)


class TargetModel:
    """Worker-backed read-only session with immutable requests/results.

    A fresh request invalidates previous ones; accept() never installs a stale
    response. Raw metadata and assessed combinations use bounded LRU caches.
    A failed fetch may be retried on the next request.
    """
    def __init__(self, context: UpgradeContext, *, fetcher: Callable = fetch_pypi_json,
                 docs_fetcher: Callable = fetch_release_declaration,
                 max_remote_lookups: int = MAX_REMOTE_LOOKUPS,
                 previous: TargetSelection | None = None):
        if not 2 <= max_remote_lookups <= MAX_REMOTE_LOOKUPS:
            raise ValueError('Invalid metadata lookup budget')
        self.context = context
        self.installed = TargetSelection(context.installation.version, tuple(sorted(
            (name, version) for name, version in context.plugins if isinstance(version, str))))
        self.netbox_versions = tuple(sorted(set(context.targets), key=_number, reverse=True))
        self.selection = TargetSelection(
            self.netbox_versions[0] if self.netbox_versions else self.installed.netbox,
            self.installed.plugins,
        )
        self._refresh_warnings = []
        if previous is not None:
            target = previous.netbox
            if _number(target) < _number(self.installed.netbox):
                target = self.installed.netbox
                self._refresh_warnings.append('Previous NetBox target is older than the installed version')
            old = dict(previous.plugins)
            plugins = []
            for name, installed in self.installed.plugins:
                version = old.get(name, installed)
                original_key, target_key = _release_key(installed), _release_key(version)
                if version != installed and (original_key is None or target_key is None or target_key <= original_key):
                    version = installed
                    self._refresh_warnings.append(f'{name}: previous target no longer valid')
                plugins.append((name, version))
            if set(old) - set(dict(plugins)):
                self._refresh_warnings.append('Previously selected plugins are no longer eligible')
            self.selection = TargetSelection(target, tuple(plugins))
        self._fetcher, self._docs_fetcher = fetcher, docs_fetcher
        self._limit = max_remote_lookups
        self._session = object()
        self._revision = 0
        self._active: TargetRequest | None = None
        self._projects = OrderedDict()
        self._releases = OrderedDict()
        self._documents = OrderedDict()
        self._assessments = OrderedDict()
        self._catalogues: dict[str, ReleaseCatalog] = {}
        self._suggestions = OrderedDict()

    def refreshed(self, context: UpgradeContext) -> TargetModel:
        return TargetModel(context, fetcher=self._fetcher, docs_fetcher=self._docs_fetcher,
                           max_remote_lookups=self._limit, previous=self.selection)

    def _invalidate(self) -> None:
        self._revision += 1
        self._active = None

    def reset(self) -> None:
        self.selection = self.installed
        self._invalidate()

    def select(self, component: str | None, version: str) -> None:
        if component is None:
            if version not in (*self.netbox_versions, self.installed.netbox):
                raise UpgradePlanError('Select an installed or discovered stable NetBox version')
            self.selection = replace(self.selection, netbox=version)
        else:
            current = dict(self.installed.plugins)
            if component not in current:
                raise UpgradePlanError('Select an eligible configured plugin')
            listing = self._catalogues.get(component)
            if version != current[component] and (
                listing is None or version not in self._plugin_versions(component, listing)
            ):
                raise UpgradePlanError('Select an installed or published newer stable plugin version')
            known = self._assessments.get((component, version, self.selection.netbox,
                                           self.context.installation.python_version))
            if version != current[component] and known is not None and known.availability == 'unavailable':
                raise UpgradePlanError('Selected plugin release is no longer available')
            targets = dict(self.selection.plugins)
            targets[component] = version
            self.selection = replace(self.selection, plugins=tuple(sorted(targets.items())))
        self._invalidate()

    def request(self, action='assess', component: str | None = None,
                *, offset=0, limit=MAX_PICKER_ROWS) -> TargetRequest:
        if action not in ('assess', 'suggest', 'catalogue'):
            raise ValueError('Unknown target request')
        if component is not None and component not in dict(self.installed.plugins):
            raise UpgradePlanError('Select an eligible configured plugin')
        if not 0 <= offset or not 1 <= limit <= MAX_PICKER_ROWS:
            raise ValueError('Invalid catalogue window')
        self._revision += 1
        request = TargetRequest(self._session, self._revision, self.selection, action,
                                component, offset, limit)
        self._active = request
        return request

    def accept(self, result: TargetSnapshot | TargetCatalogue) -> bool:
        if result.request is not self._active or result.request.session is not self._session:
            return False
        if isinstance(result, TargetSnapshot):
            self.selection = result.selection
        self._active = None
        return True

    @staticmethod
    def _cache_put(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def _lookup(self, cache, key, loader, budget, *, limit, cost=1):
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        identity = (id(cache), key)
        if identity in budget.failed:
            raise CompatibilityError('Metadata retrieval failed in this request')
        budget.take(cost)
        try:
            value = loader()
        except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
            budget.failed.add(identity)
            raise
        return self._cache_put(cache, key, value, limit)

    def _fetch(self, package, version, budget):
        if version is None:
            return self._lookup(self._projects, package, lambda: self._fetcher(package),
                                budget, limit=6)
        return self._lookup(self._releases, (package, version),
                            lambda: self._fetcher(package, version), budget, limit=12)

    def _catalogue(self, package, budget) -> ReleaseCatalog:
        if package not in self._catalogues:
            listing = discover_stable_releases(
                package, self._fetch(package, None, budget), self.context.installation.python_version)
            self._catalogues[package] = listing
        return self._catalogues[package]

    def _plugin_versions(self, package, listing: ReleaseCatalog) -> tuple[str, ...]:
        installed = dict(self.installed.plugins)[package]
        key = _release_key(installed)
        newer = tuple(item.version for item in listing.stable
                      if key is not None and _release_key(item.version) > key)
        return (*newer, installed)

    def _plugin(self, package, version, netbox, budget) -> TargetAssessment:
        installed = dict(self.installed.plugins)[package]
        python = self.context.installation.python_version
        key = (package, version, netbox, python)
        if key in self._assessments:
            self._assessments.move_to_end(key)
            return self._assessments[key]
        if _release_key(version) is None:
            return TargetAssessment('warning', 'installed' if version == installed else 'unknown',
                                    (_evidence('Release', 'Installed version cannot be compared safely', 'unknown'),))
        def docs(metadata, selected):
            return self._lookup(self._documents, (package, selected),
                                lambda: self._docs_fetcher(metadata, selected),
                                budget, limit=32, cost=2)
        try:
            review = review_plugin_upgrade(
                package, [netbox], [python], version=version,
                fetcher=lambda name, selected=None: self._fetch(name, selected, budget),
                docs_fetcher=docs,
            )
        except _BudgetExhausted:
            return _unchecked()
        except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
            return TargetAssessment('warning', 'unknown',
                                    (_evidence('Discovery', 'Plugin compatibility unavailable', 'unknown'),))
        availability = 'installed' if version == installed else review.release_status
        evidence = [_evidence('Release', warning, 'warns') for warning in review.warnings]
        if review.stages:
            stage = review.stages[0]
            evidence.extend(_evidence(item.source, item.detail, item.status) for item in stage.evidence)
            status = stage.preliminary_status
        else:
            status = 'warning'
        if status != 'conflict' and availability not in ('verified', 'installed'):
            status = 'warning'
        result = TargetAssessment(status, availability, tuple(evidence))
        # Cache completed evidence, but allow network failures to be retried.
        if review.release_status in ('verified', 'unavailable') and not any(
            'Cannot retrieve' in item or 'Cannot verify' in item for item in review.warnings
        ):
            self._cache_put(self._assessments, key, result, 128)
        return result

    def _netbox(self, selection, plugin_rows, budget) -> TargetAssessment:
        target = selection.netbox
        availability = ('installed' if target == self.installed.netbox else
                        'verified' if target in self.netbox_versions else
                        'unavailable' if self.context.tags else 'unknown')
        evidence = []
        statuses = []
        if not self.context.tags:
            statuses.append('warning')
            evidence.append(_evidence('Release discovery', 'Official NetBox releases unavailable', 'unknown'))
        if availability not in ('installed', 'verified'):
            statuses.append('warning')
            evidence.append(_evidence('Release', 'Target release availability is unverified', 'unknown'))
            path = (target,)
        else:
            try:
                path = required_netbox_path(self.installed.netbox, target, self.context.tags) or (target,)
            except UpgradePlanError as exc:
                path = (target,)
                statuses.append('conflict')
                evidence.append(_evidence('Upgrade path', str(exc), 'blocks'))
        for stage in path:
            for item in check_prerequisites(
                stage, self.context.installation.python_version,
                postgres_version=self.context.postgres, redis_version=self.context.redis,
            ):
                statuses.append({'ok': 'supported', 'blocked': 'conflict', 'unknown': 'warning'}[item.status])
                evidence.append(_evidence(f'{stage}: {item.name}',
                                          f'{item.detail} ({item.installed or "?"}; {item.required})',
                                          {'ok': 'supports', 'blocked': 'blocks', 'unknown': 'unknown'}[item.status]))
        # Earlier stages still run installed plugin versions, not final pins.
        earlier = [TargetRow(name, version, version, self._plugin(name, version, stage, budget))
                   for stage in path[:-1] for name, version in self.installed.plugins]
        for row in (*plugin_rows, *earlier):
            statuses.append(row.assessment.status)
            if row.assessment.status != 'supported':
                evidence.append(_evidence(row.component or 'NetBox',
                                          f'{row.target}: {row.assessment.status}',
                                          'blocks' if row.assessment.status == 'conflict' else 'warns'))
        if self.context.plugin_selection_incomplete:
            statuses.append('warning')
            evidence.append(_evidence('Inventory', 'Some configured plugins could not be reliably mapped', 'unknown'))
        return TargetAssessment(_status(statuses), availability, tuple(evidence),
                                any(row.assessment.pending for row in (*plugin_rows, *earlier)))

    def _rows(self, selection, budget):
        current = dict(self.installed.plugins)
        plugins = tuple(TargetRow(name, current[name], target, self._plugin(name, target, selection.netbox, budget))
                        for name, target in selection.plugins)
        netbox = TargetRow(None, self.installed.netbox, selection.netbox,
                           self._netbox(selection, plugins, budget))
        return (netbox, *plugins)

    def _suggest(self, selection, budget):
        proposed = dict(selection.plugins)
        notes = []
        partial = False
        for name, installed in self.installed.plugins:
            try:
                listing = self._catalogue(name, budget)
            except _BudgetExhausted:
                partial = True
                notes.append(f'{name}: release listing not yet checked')
                continue
            except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
                notes.append(f'{name}: release discovery unavailable; target unchanged')
                continue
            notes.extend(f'{name}: {note}' for note in listing.warnings)
            versions = self._plugin_versions(name, listing)
            key = (name, selection.netbox)
            progress = self._suggestions.get(key, _SuggestionProgress(unresolved_newer=bool(listing.warnings)))
            # Continue after previous completed candidates instead of replaying
            # the first page after the bounded metadata cache has evicted it.
            if progress.choice is None:
                for index in range(progress.offset, len(versions)):
                    version = versions[index]
                    check = self._plugin(name, version, selection.netbox, budget)
                    if check.pending:
                        break
                    progress = replace(progress, offset=index + 1)
                    if check.status == 'supported' and check.availability in ('verified', 'installed'):
                        progress = replace(progress, choice=version)
                        break
                    if version != installed and check.status == 'warning':
                        progress = replace(progress, unresolved_newer=True,
                                           uncertain=progress.uncertain or (
                                               version if check.availability == 'verified' else None))
                    if version == installed:
                        progress = replace(progress, choice=progress.uncertain or installed)
            if progress.choice is None:
                self._cache_put(self._suggestions, key, progress, 64)
                partial = True
                notes.append(f'{name}: suggestion incomplete; target unchanged, continue to assess more releases')
            else:
                # Retain completed searches until the entire suggestion request
                # finishes. Otherwise a later plugin can force an evicted long
                # catalogue to be scanned again on every continuation.
                self._cache_put(self._suggestions, key, progress, 64)
                selected = self._plugin(name, progress.choice, selection.netbox, budget)
                if selected.pending:
                    partial = True
                if progress.choice != installed and (
                    selected.pending or selected.availability != 'verified' or selected.status == 'conflict'
                ):
                    if not selected.pending:
                        self._suggestions.pop(key, None)
                    notes.append(f'{name}: suggested release could not be reconfirmed; target unchanged')
                    continue
                proposed[name] = progress.choice
                if selected.status == 'supported' and progress.unresolved_newer:
                    notes.append(f'{name}: supported target found, but newer releases were not fully assessed')
                elif selected.status == 'warning' and progress.choice != installed:
                    notes.append(f'{name}: no supported target found; suggested release remains uncertain')
        return replace(selection, plugins=tuple(sorted(proposed.items()))), notes, partial

    def evaluate(self, request: TargetRequest) -> TargetSnapshot | TargetCatalogue:
        """Call in one worker; accept only on the UI thread for the active request."""
        if request.session is not self._session:
            raise ValueError('Request belongs to another inventory')
        budget = _Budget(self._limit)
        selection = request.selection
        notes = list(self._refresh_warnings)
        if not self.context.tags:
            notes.append('Official NetBox release discovery unavailable')
        if self.context.plugin_selection_incomplete:
            notes.append('Some configured plugins could not be reliably mapped')
        if request.action == 'catalogue':
            return self._picker(request, budget, notes)
        partial = False
        if request.action == 'suggest':
            selection, warnings, partial = self._suggest(selection, budget)
            notes.extend(warnings)
        rows = self._rows(selection, budget)
        partial = partial or any(row.assessment.pending for row in rows)
        if request.action == 'suggest' and not partial:
            # A subsequent explicit suggestion can retry uncertain evidence;
            # only continuations reuse the completed search decisions.
            for name, _ in self.installed.plugins:
                self._suggestions.pop((name, selection.netbox), None)
        return TargetSnapshot(request, selection, rows,
                              _status(row.assessment.status for row in rows),
                              tuple(dict.fromkeys(notes)), partial)

    def _picker(self, request, budget, notes):
        name = request.component
        selection = request.selection
        current = self.installed.netbox if name is None else dict(self.installed.plugins)[name]
        latest = None
        if name is None:
            versions = (*self.netbox_versions, current)
            latest = self.netbox_versions[0] if self.netbox_versions else None
        else:
            try:
                listing = self._catalogue(name, budget)
                versions = self._plugin_versions(name, listing)
                latest = listing.latest
                notes.extend(listing.warnings)
            except _BudgetExhausted:
                return TargetCatalogue(request, (), 0, None,
                                       tuple(notes + ['Release listing not yet checked']), True)
            except (CompatibilityError, OSError, URLError, TimeoutError, ValueError):
                versions = (current,)
                notes.append('Plugin release discovery unavailable')
        rows = []
        for version in versions[request.offset:request.offset + request.limit]:
            if name is None:
                candidate = replace(selection, netbox=version)
                assessment = self._rows(candidate, budget)[0].assessment
            else:
                assessment = self._plugin(name, version, selection.netbox, budget)
            rows.append(TargetRow(name, current, version, assessment))
        return TargetCatalogue(request, tuple(rows), len(versions), latest,
                               tuple(dict.fromkeys(notes)),
                               any(row.assessment.pending for row in rows))
