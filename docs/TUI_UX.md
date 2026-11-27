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

## Unified NetBox and plugin upgrades

The main Upgrade NetBox entry opens a version-target dashboard. NetBox and
reliably mapped configured plugins are the only rows. Runtime and database
versions are prerequisites in details; self-upgrade remains separate.

```text
NETBOX-OPS                    v0.6.1 EN
Upgrade management
AVAILABLE UPGRADES
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
Q Quit | Esc Back | Enter Open | Tab Actions | D Details | P Pins | F5 Refresh
```

The aligned table sizes columns to content and groups Core and Plugins without
selectable group headings. On short screens, the current group remains visible
above the scroll window. Counts and operations follow the visible rows; details
remain accessible with D, including complete prerequisite reasons and stage evidence.
An asterisk marks a changed target, not a checkbox. Move with arrows/j/k,
Home/End and Page Up/Down. Enter opens the version picker, D opens scrollable
component evidence and the complete plan, P reviews local pins, and Tab or
Shift+Tab switches between rows and actions. The paged picker assesses the
candidate against the selected platform; D shows evidence and F5 retries the
page. Enter selects, Esc cancels. Incomplete evidence is not approval.
Initial suggestions and reassessment run in a single bounded read-only worker;
while it runs, changes and execution are disabled. Suggest continues a partial
search or explicitly recomputes plugin targets without changing NetBox.
Reset restores installed versions; refresh preserves valid intent and clears
review. Full validation derives the exact changed pins and existing operation.
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
