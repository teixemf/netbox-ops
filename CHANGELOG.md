# Changelog

Notable changes to netbox-ops are listed below. The project is currently in early development.

## [0.4.0] — RHEL/Debian compatibility and TUI improvements

- Added RHEL- and Debian-family detection and safer discovery of PostgreSQL and Redis systemd units.
- Improved support for PostgreSQL client paths and cluster ports.
- Updated installation scripts to use an isolated Python 3.12 environment.
- Added the read-only `netbox-ops platform` diagnostic command.
- Improved backup labels, selection details, restore scope selection, and confirmation feedback in the TUI.
- Extended automated regression coverage.

**Validation:** 168 isolated automated tests were reported for this release. Real PostgreSQL restore and end-to-end recovery tests on RHEL/Debian VMs remain outstanding.

## [0.3.6] — Backup management fixes

- Sorted backups by timestamp across backup types.
- Fixed database-only restores from full backup archives.
- Removed the standalone “delete all” operation.
- Improved backup verification and explanations for unavailable restore actions.
- Continued to require explicit confirmation for destructive operations.

## [0.3.5] — Backup provenance and live service logs

- Identified automatically created pre-restore safety backups.
- Added live journal viewing in service management.
- Improved service and backup screen layout and storage chart legends.

## [0.3.4] — Backup management layout

- Added a persistent backup details panel and a responsive horizontal action bar.
- Improved multi-selection summaries and disabled unavailable actions.
- Reduced redundant file reads when refreshing the list.

## [0.3.3] — Terminal theme and progress reporting

- Introduced the NetBox Dark terminal palette and a Braille storage chart.
- Kept operation results visible on the progress screen until dismissed.

## [0.3.2] — Service visibility and backup selection

- Corrected NetBox version detection to use `release.yaml` rather than Django's version.
- Added service log viewing and multi-select backup actions.
- Added progress task lists and stronger handling of older backup metadata.

## [0.3.1] — TUI performance

- Improved service metrics, version caching, and context-sensitive actions.
- Optimized backup listing and metadata caching.
- Added JSON sidecar metadata for new media and full backups.

## [0.3.0] — Unified backup management

- Reorganized the TUI around service and backup management.
- Added combined database and media backups with selective restore options.
- Restricted new media backups to uploaded media rather than configuration files.
- Kept older configuration-bearing archives as legacy, without restore support.

---

**Safety note:** Automated tests do not replace recovery testing on real target environments. Validate backup and restore workflows in disposable systems before production use.
