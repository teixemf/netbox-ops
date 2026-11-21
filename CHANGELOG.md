# netbox-ops — Changelog

## NBH-xxx — RHEL/Debian compatibility and TUI improvements (netbox-ops v0.4.0)

**Previous state:** v0.3.6 assumed Linux units (`postgresql.service`, `redis.service`) and PostgreSQL utilities mainly compatible with RHEL installations. On Debian, PostgreSQL services may use cluster-specific units, Redis typically uses `redis-server.service`, and the system's default Python version is not necessarily 3.12. In addition, incorrect confirmation returned without an alert, the backup table did not distinguish manually created backups, the detail pane did not follow focus when other backups were marked, and unused space in the lower screen remained empty.

**Changes made:**
- Added detection of RHEL and Debian distribution families, including relevant derivatives, using `/etc/os-release`.
- Added automatic, non-destructive, cached discovery of PostgreSQL and Redis systemd units; ambiguous PostgreSQL clusters or versioned RHEL units are not selected arbitrarily.
- Added support for PGDG binary paths and optional PostgreSQL cluster port configuration (`backup.pg_bin_directory` and `backup.postgres_port`), respecting explicit JSON service units without changing PostgreSQL.
- Updated `install.sh` and `upgrade.sh` for both distribution families, keeping **Python 3.12** in the dedicated virtual environment and the launchers under `/usr/local/sbin` and `/usr/sbin`.
- Added the read-only `netbox-ops platform` diagnostic command to report the distribution, service units, and PostgreSQL client utilities.
- Added an explicit alert when the confirmation phrase is incorrect; the TUI returns to the previous menu without starting an operation.
- Added a `MANUAL` label for regular backups and retained `PRÉ-REST.` for pre-restore safety backups, without changing the archive format.
- Updated the right detail pane to follow the focused backup even while other backups are marked; multi-selection and preview remain independent.
- Moved backup actions to the footer, integrated the Braille storage chart into the left pane, and redesigned the full-backup restore-scope selection screen.
- Preserved existing security controls, selective restores, integrity checks, and Redis policy; updated Portuguese/English translations and regression tests.

**Rationale:** provide safer administration of equivalent NetBox installations on RHEL and Debian without operating the wrong services or changing the intended Python version, while improving TUI readability and safeguards.

**Validation:** 168 isolated automated tests covering both platform profiles and UI changes. No real PostgreSQL recovery tests were performed on RHEL/Debian VMs, and end-to-end Python 3.12 validation was not performed in the original development environment. Functional VM validation remains necessary before production use.

---

## NBH-xxx — Backup management and restore fixes (netbox-ops v0.3.6)

**Previous state:** in v0.3.5, backup entries were sorted alphabetically by filename, mixing types without respecting creation dates. Database-only restore from a full backup failed because the dump was not extracted before import. Management included a standalone Delete All operation, and the Verify action was not prominent, especially for legacy archives.

**Changes made:**
- Sorted backups chronologically, newest first, regardless of type or pre-restore label. Filenames without recognizable dates fall back to their modification timestamps.
- Removed Delete All from the interactive menu, CLI, and backup manager; explicit selective deletion, including batches, remains available with confirmation.
- Highlighted and repositioned Verify in the action bar; retained the V shortcut and added integrity-only verification for `legacy` archives, without enabling restore.
- Fixed PostgreSQL dump extraction for `full --only db` restores and restore navigation when exactly one backup is selected but no row is focused.
- Added explanatory messages for temporarily unavailable or incompatible restore actions.
- Updated documentation and ran regression tests along with new isolated tests.

**Rationale:** improve clarity, reduce accidental deletion risk, and fix data restoration workflows.

**Validation:** automated tests with mocked PostgreSQL and systemd. Real restore, rollback, and Redis consistency checks on the RHEL VM remain pending.

---

## NBH-xxx — Labeled pre-restore backups and TUI live logs (netbox-ops v0.3.5)

**Previous state:** v0.3.4 did not distinguish safety backups created automatically before a restore. Backup table headers were left-aligned. The lower part of service management was underused, actions sat above the footer, and there was no continuously visible journal preview or live-log option. The donut chart legend colors did not match its segments.

**Changes made:**
- Identified pre-restore safety backups with `pre-restore` in their filenames and `purpose: pre_restore` in both the manifest and JSON sidecar, while regular backups use `purpose: manual`.
- The quick listing recognizes safety backups by filename without reintroducing costly archive reads. The table and detail pane show their origin; older archives are not labeled retroactively.
- Centered the type, timestamp, size, label, and name column headers within the backup table.
- Reorganized service management with a horizontal bottom action bar, metrics at the top, the journal at the bottom, and Enter to expand the log panel.
- Added `Live log: ON/OFF` and the L shortcut using a non-blocking `journalctl --follow` subprocess and a bounded buffer. Static log reads run in a worker to keep navigation responsive.
- Corrected the donut legend to match the segments: backups (teal), other data (blue), and free space (gray).
- Preserved restrictions on service operations and restore/deletion confirmations. Updated PT-PT/EN translations, isolated tests, and documentation.

**Rationale:** clearly identify safety backups, improve menu readability and usability, and allow operators to monitor logs without leaving service management or sacrificing keyboard responsiveness.

**Validation:** isolated automated tests, syntax checks, and ZIP integrity checks. Visual and operational validation on an RHEL/Python 3.12 VM and real restore testing are pending. No changes were made to the VM during preparation.

---

## NBH-xxx — Persistent backup detail pane and horizontal actions (netbox-ops v0.3.4)

**Previous state:** in v0.3.3, backup actions occupied fixed positions across two rows, and pressing Enter on a list entry opened a separate detail screen. The right pane did not summarize multi-selection, leading to unnecessary navigation and uncertainty about which files were affected.

**Changes made:**
- Reorganized backup actions into a horizontal bar below the list and detail pane, automatically adapting to terminal width.
- Removed the Enter-triggered detail screen; a preview is always available in the right pane for the focused or single marked file.
- Added a multi-selection summary showing count, types, and total size, clarifying that several backups cannot be restored simultaneously.
- Disabled unavailable actions, including restores of legacy/incompatible files or files whose metadata is still loading.
- Preserved Space toggling, select-all, shortcuts, progress indicators, confirmations, and integrity safeguards.
- Fixed F5 so refreshing the backup list reads it only once.
- Updated PT-PT/EN translation JSON, README, and automated tests.
- Preserved backup/restore logic and isolated Python environments; no changes were made to the RHEL VM.

**Rationale:** make backup management consistent with service management, reduce redundant screens, and avoid confusion between the focused file and files marked for batch operations.

**Validation:** 132 isolated automated tests using Python 3.13, including simulated TUI navigation and operations. Visual and live operational validation on RHEL/Python 3.12 remained pending.

---

## NBH-xxx — NetBox Dark theme and integrated progress results (netbox-ops v0.3.3)

**Previous state:** v0.3.2 had an inconsistent terminal color palette and a storage chart made of `●` characters that did not form a convincing circle. After service or backup operations, the TUI replaced the progress screen with another results screen, duplicating information.

**Changes made:**
- Adopted Palette A (NetBox Dark): dark background, differentiated panels, teal highlights, and reserved colors for success, failure, and warnings.
- Implemented xterm-256 palette approximations with an eight-color fallback and retained `NO_COLOR` support.
- Replaced the dotted circle with a Unicode Braille donut chart showing backups, other data, and free space, with the backup percentage at the center.
- Kept the final result of service operations visible on the same screen as the progress bar and task list until Enter/Esc.
- Applied the same persistent progress screen to backup creation, restore, verification, and deletion, including final paths, results, or errors; removed the redundant results page.
- Updated PT-PT/EN translations, isolated tests, and documentation while retaining existing business logic and making no changes to NetBox or VM configuration.

**Rationale:** improve visual consistency, readability, and usability in SSH terminals, and avoid unnecessary navigation after long-running operations.

**Validation:** 118 isolated automated tests and a startup test in an emulated 256-color terminal. SSH-client theme validation and real backup/restore tests on the RHEL VM remained pending.

---

## NBH-xxx — Operational TUI and reliable version information (netbox-ops v0.3.2)

**Previous state:** v0.3.1 displayed `5.2.9` as the NetBox version, although it was the Django version. Service details did not offer log browsing, the backup menu lacked multi-selection, and progress bars did not identify tasks clearly enough.

**Changes made:**
- Fixed NetBox version detection by reading `release.yaml`, caching the value per session, and displaying versions on the dashboard.
- Added version-source metadata to new backup manifests; older manifests are treated as having an unknown NetBox version and require additional confirmation when restoring.
- Added recent logs to the expanded service detail screen, including a viewer and configurable line count, without bringing logs back into the operational preview.
- Added a text-based backup storage chart showing backup files, other filesystem data, and free space.
- Added multi-selection, select-all, verification, and deletion of selected backups with progress reporting; individual restores and selective `full` restores remain supported.
- Added a task list showing per-task status during service, backup, restore, verification, and deletion progress.
- Preserved the bilingual TUI, CLI, and `db`/`media`/`full` policy.
- Updated tests and documentation without changing NetBox on the VM.

**Rationale:** correct misleading version information, improve operational readability, and reduce the risk of unintended batch operations.

**Validation:** isolated automated tests and syntax checks. RHEL VM validation and actual restore tests remained pending.

---

## NBH-xxx — TUI performance and metadata caching (netbox-ops v0.3.1)

**Previous state:** v0.3.0 displayed service logs in operational service management, offered actions not always appropriate for the current state, and repeatedly opened compressed backup manifests whenever the cursor moved, causing noticeable navigation delays.

**Changes made:**
- Reorganized the main-menu and service-management panels, keeping operations close to the service list.
- Replaced logs in the detail panel with CPU, RAM, uptime, PID, startup mode, and component version.
- Added asynchronous version lookup with one query per session and caching; NetBox and NetBox RQ share the application version.
- Disabled operations incompatible with the current service state; only eligible services in mixed batches proceed to confirmation.
- Made backup listing lightweight, without opening archives.
- Added per-file metadata caching and invalidation when a file or its sidecar changes.
- Added JSON sidecars to new media and full backups for display metadata; integrity still requires independent verification.
- Preserved all three backup types (`db`, `media`, `full`) and selective restore from full archives.
- Added automated tests for performance, versions, service state, and metadata.

**Rationale:** improve SSH usability, avoid repeated work on large archives, and show relevant operational information without changing the approved backup/restore model.

**Validation:** isolated automated tests completed. RHEL VM validation and real restore rehearsal remained pending.

---

# netbox-ops — v0.3.0

NBH-xxx — TUI reorganization and unified backup management

**Previous state:**
- v0.2.0 exposed services and backups as separate main-menu entries.
- The main screen did not use available space for metrics.
- Artifact backups included NetBox and LDAP configuration files by default.
- Restoring a full backup did not allow the database and media to be selected independently.

**Changes made:**
- Added a main-menu status panel with per-service CPU/RAM and backup storage used/free figures.
- Reorganized service management with multi-selection, detail panels, and persistent operations in the footer.
- Added a unified backup management menu offering listing, metadata, creation, verification, restore, individual deletion, and delete-all.
- Limited artifact backups to images and uploaded files under `media`, excluding configuration and other artifacts.
- Changed combined backups to include **database + media**.
- Implemented selective restore from full backups: database only, media only, or both.
- Added stage-based progress bars for backup/restore operations and destructive-action confirmations.
- Retained compatibility with older configuration files without importing artifacts outside the new scope.
- Classified older archives containing configuration files as legacy, without allowing restore in the new release.
- Preserved coordinated NetBox service shutdown/startup during restore and the existing Redis/RQ preservation policy; selective cache cleanup still requires environment validation.
- Updated PT-PT and English translations, documentation, and automated tests.

**Rationale:** improve terminal navigation and operational visibility, reduce accidental operations, and narrow backup scope to application data approved for the release.

**Operational validation on the RHEL VM:** pending, especially PostgreSQL restores and Redis cache consistency.
