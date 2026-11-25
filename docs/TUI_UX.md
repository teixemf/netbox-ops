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

## NetBox upgrades (all three modes)

Illustrative terminal layout, not a fixed-width rendering:

```text
NETBOX-OPS                     v0.6.1 EN
Upgrade NetBox and plugins
────────────────────────────────────────────────
NetBox: 4.4.9 -> 4.7.6
Plan: Requires additional confirmation
Stages: 1 | Plugins: 1 selected / 2 configured
... Full overview in Details
────────────────────────────────────────────────
  Target NetBox: 4.7.6
  Select plugins (1 selected)
  Build / recalculate plan
  Compatibility details and recovery safeguards
  Approve and execute
  Review local plugin pins
  Refresh installed versions and releases
────────────────────────────────────────────────
Q Quit | ↑↓ Select | Enter Open | Esc Back
```

Details show exact plugin versions, unchanged plugins, required stages,
prerequisites, compatibility evidence, dependency and ltree checks (including
skipped resolution and overlaps), release notes, downtime, backups and manual
recovery. No execution action is present before a runnable plan exists.

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
