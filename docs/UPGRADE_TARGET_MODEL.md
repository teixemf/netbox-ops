# Preliminary upgrade targets

The unified upgrade screen uses a read-only model separate from the full
planner and executor. The model never installs packages, changes pins, checks
out Git tags or invokes pip preflight.

## Session contract

- Discover with `discover_target_context(cfg)`. A failed official tag listing
  preserves the installed inventory and reports discovery uncertainty.
- Create `TargetModel(context)`. NetBox starts at the newest discovered stable
  version, or installed when no newer release is available. Each reliably
  mapped configured plugin starts at its installed version.
- Create a request on the UI thread with `model.request('suggest')`, then call
  `model.evaluate(request)` in a single worker. Apply the returned immutable
  snapshot only when `model.accept(result)` succeeds. Keep loading/partial
  states visible and offer another suggestion request to continue a bounded
  search. A normal `'assess'` request never changes targets.
- `request('catalogue', component, offset=0, limit=8)` evaluates a page of
  stable, non-downgrade candidates plus the installed version. A component of
  None denotes NetBox, not a plugin. Plugin candidates use the selected NetBox
  target and its installed Python; NetBox candidates check the current plugin
  targets and server prerequisites. `select(component, version)` changes only
  that target. `reset()` restores installed versions.
- Re-discover inventory on refresh and replace the session with
  `model.refreshed(context)`. This creates fresh caches, preserves still-valid
  intent, reports removed/obsolete selections, and rejects responses from the
  previous session. Do not run more than one worker evaluation per model.
- Every edit, refresh, reset or new request invalidates older responses. Keep
  any fully validated plan separately and invalidate it when intent or
  inventory changes, including accepted suggestions. Do not execute a snapshot.

## Evidence and bounds

Each request permits at most 12 uncached lookups, charging a documentation
lookup for its potential two requests. Release/documentation fetches retain
the existing eight-second per-request timeout and fixed hosts. Picker pages
have at most eight rows. Raw responses use small bounded LRU caches; completed
exact assessments are keyed by package, version, NetBox target and Python in
one inventory session. Partial suggestion progress avoids rescanning earlier
candidates. Refresh invalidates all cached evidence.

Suggestions inspect newer stable published releases in descending order,
stop at preliminary support and include the installed version as an unchanged
candidate. If support is not established, only a verified published uncertain
release may be suggested, with an explicit warning; otherwise retain installed
or unchanged intent. A partial search does not claim latest compatibility.
Release availability is separate from compatibility. Missing discovery,
unmapped configured plugins, unknown prerequisites and uncertain declarations
are not confirmed incompatibilities.

The same plugin compatibility rules supply preliminary status: supported,
warning, conflict or unchecked. A green preliminary result does not satisfy
core-aware dependency resolution, ltree checks, stage validation or approval.
The existing `PluginCompatibility.status` contract remains unchanged.

## Full validation

Use `selection.plan_arguments(model.installed)` to derive NetBox-only,
plugin-only, combined or no-op intent. It emits only exact changed plugin pins.
Unchanged configured plugins remain part of the full planner's compatibility
review. Pass the derived arguments to the existing `make_plan` and retain
its complete dependency review, fingerprint, explicit confirmations,
execution-time revalidation and recovery safeguards. Preliminary evidence
must never authorize a blocked or unvalidated plan.

Python, PostgreSQL and Redis are prerequisites only, not upgrade targets.
