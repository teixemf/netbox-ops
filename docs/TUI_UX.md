# Terminal UI conventions

Use the existing curses frame, styles and keyboard handling. Keep the screen
usable over SSH (minimum 50 columns by 16 rows), after resize, in both
catalogues, and without colour. The upgrade views are the first application;
other views should adopt these conventions incrementally, not via a rewrite.

## Layout and interaction

- Header: product/version/language and operation title. Footer: contextual
  keyboard hints. Keep separators and headings consistent with Services and
  Backups.
- Keep a bounded overview above the scrollable action list. Show the installed
  and selected versions and explicit status first. Long lists and diagnostics
  belong in the scrollable details view; never silently imply missing checks
  succeeded. Use consistent left alignment and avoid fixed terminal widths.
- Use arrows (or j/k), Home/End and Page Up/Down for navigation, Enter for the
  focused action and Esc to go back. No initial executable focus. Q exits
  read-only navigation; it is not a confirmation token or a way to interrupt
  maintenance. Use the existing literal token confirmation, inline errors
  and cancellation behavior (#164).
- Text carries status, colour only reinforces it. Distinguish ready/verified,
  uncertain, blocked and not checked. Compatibility is not authorization.
  Do not offer execution for blocked or absent plans.
- Any target/selection edit or refresh invalidates its review. Review the
  complete plan before requesting approval; execution uses the exact reviewed
  fingerprint and the backend rechecks it under lock. Keep release notes,
  unchanged components, prerequisite evidence, warnings and recovery details
  accessible even when a terminal cannot fit the full overview.
- Progress reports actual phases/completed stages, not a percentage based on
  elapsed time. Display validated completion or failure/recovery guidance.
  Self-upgrade submission is not installation success; show the recorded
  outcome and recovery state on reopening.

## Confirmation and input panels

Use the same top-aligned content-sized panel for service and backup
confirmation, upgrade/self-upgrade approval, pin review and URL/credential
input. Keep the exact challenge in bold teal (bold in monochrome), the field
and local validation message together. Preserve literal case-sensitive tokens,
masked credentials, cancellation and the existing operation gates.
Wrap complete summary and target text; scroll the review with arrows or
Page Up/Down while keeping the input visible. Keep the leading warning
visible when it fits, with a compact warning and scrollable full text on
short screens. Resize clamps the review viewport. The normal keyboard footer
remains separate from the input panel.

## Unified NetBox and plugin upgrades

The main Upgrade NetBox entry opens a version-target dashboard. NetBox and
reliably mapped configured plugins are the only rows. Runtime and database
versions are prerequisites in details; self-upgrade remains separate.

```text
NETBOX-OPS                    v0.6.1 EN
Upgrade management
PLANNED CHANGES
Core                 CURRENT    TARGET     STATUS
  NetBox             4.4.9      *4.7.6     OK

Plugins
  example-plugin     1.0.0      *2.0.0     Warning
Components: 2 | Changes: 2
UPGRADE OPERATIONS
[Suggest targets] [Reset targets] [Validate] [Refresh]
 Review and execute
COMPONENT DETAILS                 UPGRADE PLAN
example-plugin                    Plan: Not checked
1.0.0 -> 2.0.0                   NetBox: 4.4.9 -> 4.7.6
Warning                           Stages: ?
Q Quit | Esc Back | Enter Open | Tab Actions | D Plan | E Evidence | P Pins | F5 Refresh
```

The aligned table sizes columns to content and groups Core and Plugins without
selectable group headings. On short screens, the current group remains visible
above the scroll window. Counts and operations follow the visible rows. Use the Services/Backups
frame-background operation bar and separators, sharing separator rows with
headings on short terminals. Enabled actions have brackets; a disabled
focused action keeps its muted label and a distinct focus marker. Details
remain accessible with D, including complete prerequisite reasons and stage evidence.
An asterisk marks a changed target, not a checkbox. Move with arrows/j/k,
Home/End and Page Up/Down. Enter opens the version picker, D opens scrollable
the plan overview, E opens complete technical evidence (including the fingerprint),
P reviews local pins, and Tab or Shift+Tab switches between rows and actions.
The plan overview prioritizes readiness, blockers and required actions ahead of
warnings and stage validation. A contextual Review pins action appears only for
correctable diagnostics. Unrun checks never appear as passed. On compact
screens the readiness is retained in the overview and D provides scrollable
untruncated reasons; the full execution review and approval are unchanged.

The version picker opens with Keep installed version first, followed by all
eligible discovered releases in descending order. It uses the available screen
height and continuous navigation; Page Up/Down moves by the visible viewport.
Listing and preliminary compatibility assessment run separately in one
read-only worker. The selected batch is assessed using existing bounded
requests, without replacing the picker or blocking navigation. Unassessed rows
stay explicitly unchecked. Only the version cell highlights focus; status
colours remain semantic and notes prioritize actual blocking/warning evidence.
The installed plugin is assessed against the selected NetBox target too.
Plugin evidence includes conventional exact-tag compatibility tables and,
where publisher repository/package/module identity and a literal exported
PluginConfig can be mapped, explicit runtime minimum/maximum NetBox versions.
Out-of-range documentation is a warning; a recognized runtime gate or
declared dependency conflict blocks the target. Tagged README tables are a
fallback only; remote source is parsed, never imported or executed. If the
publisher link is missing or stale, a bounded static fallback can check a
small universal wheel and matching source distribution from the exact PyPI
release. Both must match the published SHA256 digests and agree on the literal
runtime limits. Downloads use the fixed PyPI file host without redirects; no
installation, build hooks or on-disk archive extraction is performed. Other
artifact layouts, missing, ambiguous or dynamic declarations remain uncertain. The core-aware dependency
resolver and execution-time review remain mandatory.
D opens complete evidence, F5 retries release metadata and assessments within
the inventory session (dashboard F5 refreshes official NetBox releases), Enter
selects and Esc cancels. Cancellation may wait for a bounded in-flight lookup
to finish before reusing the model. Discovery failure still permits retaining
the installed version. Incomplete evidence is not approval.
Upgrade NetBox opens directly into the planned changes dashboard. Operations
stay immediately below the scrollable target table. Reserve rows for overall
readiness, the primary reason and the footer before expanding the table.
Use side-by-side component and plan panels on wider screens; use a compact
plan summary on narrow or short screens. D/E expose complete evidence. Exact
validation runs in the existing read-only worker while retaining the dashboard;
execution stays disabled until the matching plan has been accepted. For an
actual offline curses capture (synthetic plan, no live installation), run
`python -m tests.upgrade.terminal_dashboard_capture /tmp/dashboard.json en`
in a terminal from the repository root; use `pt` and `NO_COLOR=1` as needed.
The regression suite captures 50x16, 80x24, 120x30 and 160x45, including resize.
Dynamic
pin actions retain focus by identity, never implicitly move focus to execution.
With no changed targets, use neutral Unchanged labels unless there is a real
compatibility, runtime or inventory problem; this is not verification. Raw
evidence stays available on E. Entry, refresh and explicit suggestions use a
separate discovery progress view with PROGRESS, TASKS / STATUS and RESULT.
Show observed inventory, release lookup, suggestions and assessment states,
with a successful-task count and no invented progress. Complete discovery
opens the dashboard automatically. Incomplete evidence permits explicit Enter
only for an accepted current inventory and snapshot; failed discovery stays
on the progress view with D details, F5 retry and Esc back. Local target edits
and reset retain the dashboard; no discovery panel replaces plan details.
A failed inventory refresh invalidates the previous review and hides stale rows.
The worker uses the
existing bounded requests and cooperative cancellation on exit, not forced
termination of a running external command.
Initial suggestions and reassessment run in a single bounded read-only worker;
while it runs, changes and execution are disabled. Suggest continues a partial
search or explicitly recomputes plugin targets without changing NetBox.
Reset restores installed versions; refresh preserves valid intent and clears
review. Full validation derives the exact changed pins and existing operation.
A failed local requirements check names the affected package, declaration,
installed version, stage, reason and next action in both CLI and TUI.
Use D then Enter Review pins (or P / Review pins in the dashboard) for correctable exact
pins, restricted to reliably mapped configured plugins. Non-plugin local
declarations (including bare names, exact pins, extras and comments) remain
unchanged. Resolve supported declarations together with exact core and plugin
pins, preferring installed bare-package versions when feasible. Review all
resolved version changes before approval; bind the full report to temporary
execution constraints, not permanent non-plugin pins. For a declared LDAP
requirement, a known exact python-ldap version may be built in a temporary
unprivileged systemd unit with a private temporary directory. Missing build
prerequisites or an unverified wheel block execution; no arbitrary source
builds or automatic OS package installation are allowed. Conflicting core pins,
ambiguous plugin mapping and complex/unsafe declarations require manual review.
Only explicitly approved plugin pin changes are written under the
existing lock and inventory revalidation; original requirements and their
permissions/hash are retained in a protected pin-recovery directory outside
the NetBox checkout before replacement. A successful correction invalidates
the prior plan, refreshes inventory and rebuilds validation; no package
installation or service action is authorized by pin review.
Only an executable plan matching the current selection enables review and
execution. The existing exact fingerprint, confirmation, revalidation, health
and recovery gates remain unchanged. On narrow terminals use D for full
untruncated details. Q/Esc leave read-only navigation without executing.

## netbox-ops self-upgrade

```text
NETBOX-OPS                     v0.6.1 EN
Upgrade netbox-ops (self-upgrade)
────────────────────────────────────────────────
Installed netbox-ops: 0.6.1
Target release: v0.7.0
Compatible — review required
Required upgrade path: v0.7.0
Last self-upgrade result: No result recorded
────────────────────────────────────────────────
  Select target release
  Release notes and compatibility
  Review selected release
  Last self-upgrade result
  Refresh installed versions and releases
────────────────────────────────────────────────
Q Quit | ↑↓ Select | Enter Open | Esc Back
```

The release picker lists stable releases and their actual compatibility.
A compatible reviewed release may expose Install reviewed next step; unknown,
blocked, absent or recovery-pending targets cannot be installed. Refresh
clears the review. Check pending handoff/transaction and trusted result before
offering installation and again before execution; the backend still owns the
lock, archive verification, fresh plan validation and transactional handoff.
Reopening the view shows the last recorded status, never an inferred success.

## Selection menus

Simple selectors share a content-sized panel, a section heading, a consistent
selection highlight and the standard keyboard footer. They do not arm a
selection until the operator navigates; Enter alone cannot choose a default.
For fixed short menus, show matching numeric shortcuts and the localized
open hint; numbers select an option, never confirm a protected operation.
Keep contextual details next to the menu on wide terminals and stack them on
narrow terminals. Database and media-only backups do not stop NetBox. Media
is a live archive, not an atomic snapshot; combined backups temporarily stop
active NetBox and RQ writers for database/media consistency. None includes
configuration. Restore scope keeps its selected backup and precautionary backup
warning visible and Esc cancels immediately. Other selectors retain their
existing Esc behavior, scrolling and confirmation gates. For real offline
curses captures, run `python -m tests.tui.terminal_selector_capture /tmp/selectors.json en`
in a terminal from the repository root; use `pt` and `NO_COLOR=1` as needed.
The regression suite exercises actual curses at 50x16 and 140x34 without
performing backups or restores.

## Backup task progress

Backup create, restore, verify and delete show planned tasks before starting.
The backend emits explicit pending, running, done, failed, skipped,
not-applicable and not-run states; a later task never implies earlier success.
For combined archives, extraction is required even for database-only restore;
a PostgreSQL dump does not require extraction. Tasks that are not applicable
or skipped do not contribute to the completed/total task counts. Batch file
tasks have distinct identities. Recovery and cleanup tasks are appended only
when attempted. Overall success requires a successful worker result, including
required service restart and cleanup. Legacy progress callbacks retain the
four-argument signature with task-based counters.

Present PROGRESS, TASKS / STATUS and RESULT as separate top-aligned panels.
Keep applicable planned rows stable. Show a muted hollow circle (○) with no
status text before a task starts, and omit non-applicable tasks. Use a filled
circle (●) for observed states. Show Running, OK, Failed,
Skipped and Not run when meaningful; never infer completion. The result panel
has a neutral dash until an outcome exists. Colour reinforces the state,
not the entire row. Wrap long names and result
messages without losing characters. Up/Down and Page Up/Down scroll tasks,
with automatic active-task following until the operator scrolls manually.
After completion, Tab switches between task and result scrolling; a visible
focus marker works without colour. Enter/Esc/Space acknowledges the final
result. Q and other cancellation keys remain disabled while the worker runs.
Resize clamps both viewports and keeps the outcome accessible at 50x16.
Apply the same presentation to planned service actions and upgrade discovery.
Service batches show the applicable service list and observed outcomes;
read-only discovery omits non-applicable phases. Upgrade execution and
self-upgrade report actual phases, without adding fabricated task lists.
Recovery-pending conditions remain explicit; internal states are unchanged.

## Backup inventory counters

Available Backups is a standalone heading. An empty inventory displays only
the empty message, not table headers. The read-only Selected counter follows
Select all in the operation bar and counts marked entries, not keyboard focus.
Backup Storage counts database, media and combined archives once each, with
legacy and unclassified rows only when present. Total comes from the same
in-memory list; the separate disk measurement supplies bytes, not inventory
counts. An inventory refresh failure is reported separately from an empty list.
Use B for full storage and inventory details, including multi-selection size
and restore restrictions, on terminals where the counters do not fit.

## Main dashboard

The platform table uses translated headings and content-sized columns with a
three-cell gap when space permits, compact spacing otherwise, and right-aligned
CPU/RAM values. On narrow terminals, omit Version first, then RAM and CPU;
retain service identity and text state. An ellipsis indicates a version lookup
in progress or a truncated long version; a dash means unavailable. Failed,
inactive and transitioning services have distinct text states, without depending
on colour. Refresh errors replace stale observations.

Use three separate panel backgrounds (Menu, Platform Status, Backup Storage),
with frame-background gaps, no internal border lines, and a shared lower edge
for Menu and Backup Storage when space permits. Keep the live CPU/RAM note
inside Platform Status below the table. The four numeric TUI menu entries are
Services, Backups, Upgrade NetBox, Upgrade netbox-ops; leave one blank row before
the last group (4, L, Q). CLI commands and menu numbering remain unchanged.
Update discovery is available in the corresponding upgrade views.

The storage panel uses the existing disk chart only when its legend fits;
otherwise show compact textual usage/count. When the legend column is wide
enough and the full details fit, place Latest Backup under the legend beside
the chart, separated by one empty row. Size the panel to the taller content.
Otherwise keep the vertical layout or compact B details. The latest local
backup section has aligned Date, Name, Type and Tag fields. It selects the
newest recognized local archive using the same chronological inventory as
backup management. For the selected file only, read a bounded regular JSON
sidecar when available. Prefer its validated timezone-aware creation timestamp
and recognized purpose; otherwise show the filename timestamp or filesystem
modification time explicitly. Imported filenames without managed provenance
have unknown tags. Escape non-ASCII/control characters in untrusted filenames
for unambiguous terminal display. No archive contents are opened or verified.
This is a local observation, not a guarantee of a successful backup or a
complete recovery point.

Refresh every 15 seconds, on F5/R and after returning from backup management.
Clear the snapshot on refresh errors. Wrap long names without truncation on
larger terminals; on compact terminals that cannot fit all fields, display
B (Backup details) to open the full scrollable read-only summary. The menu,
keyboard hints and footer remain accessible at 50x16. No new dependency or
backup/restore behavior change.

If an inventory refresh fails, discard old marks, preview metadata and storage observations. Require a successful refresh and new selection before operating on existing backups; never show stale entries as current or count an unavailable inventory as zero.

### PostgreSQL ltree prerequisite

For NetBox 4.7+ the read-only plan matches the database against the backup target and distinguishes installed/usable, available/installable, missing files, insufficient migration privileges, unusable schema, mismatched runtime and unverified connections. Detailed review shows role, server version, schema and search path without credentials. Native NetBox migrations create the extension when permitted; execution verifies usability before restarting writers. No extension is created by plan review. Unverified peer/OS-user-dependent migration authentication and unresolved prerequisites require DBA review.

When the migration role cannot install ltree, the contextual Install ltree
action is available only for a verified local loopback connection matching the
configured backup database, a peer-authenticated postgres administrator and
a protected public schema. The CLI equivalent is `upgrade ltree` followed by
`upgrade ltree --install --approve-ltree <fingerprint>`. Both require explicit
review, the operation lock, a verified database backup and a fresh identity
check inside the fixed CREATE EXTENSION transaction. Revalidate the upgrade
plan afterwards. Never grant privileges, drop/relocate extensions or restart
services; remote, ambiguous or writable schemas require DBA intervention.
