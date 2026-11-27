# Changelog

Notable changes to netbox-ops are listed below. The project is currently in early development.

## [0.6.1] — Upgrade reliability and user experience

- Replaced the TUI upgrade submenu with one exact-target NetBox and plugin dashboard, initial stable version suggestions, paged version pickers and preliminary compatibility evidence. Full dependency validation and explicit approval remain mandatory.
- Restricted plugin upgrade targets to reliably mapped installed configured plugins. Explain overlapping local and core requirements; retain identical simple non-plugin pins only after successful dependency resolution.
- Fixed plugin-only plan fingerprint consistency between TUI review and locked execution without relaxing inventory or approval checks.
- Refined dashboard platform and latest backup information, service runtime/startup actions, inline confirmation errors and backup inventory layout. Show per-type totals and marked counts; discard stale selections when inventory refresh fails.
- Improved CI impact-based test selection and differential coverage reporting, with manual and weekly full regression workflows. Release validation remains mandatory.
- Added disposable Ubuntu 24.04 VM tests of real TUI upgrade selection, validation, cancellation, approval and execution for NetBox-only, plugin-only and combined upgrades, retaining CLI, cross-minor, controlled failure and recovery checks.

**Upgrade from v0.6.0:** Use the transactional self-upgrade mechanism for supported archive installations after the official v0.6.1 release is published. Review the compatibility plan and verified release assets, and follow [self-upgrade and recovery](https://github.com/teixemf/netbox-ops/blob/v0.6.1/docs/SELF_UPGRADE_RECOVERY.md). Do not extract over an existing installation. Upgrading from v0.5.0 requires manual replacement.

**Validation scope and limitations:** NetBox upgrade E2E uses disposable Ubuntu 24.04 guests and a synthetic terminal for TUI input/rendering, with real planning, execution, service health and recovery. PostgreSQL integration on Ubuntu 22.04 and Rocky Linux 9 is separate evidence, not full upgrade E2E on those platforms. Automatic NetBox upgrade rollback (#63), published-release self-upgrade pair testing (#132), and broader maintenance coordination (#99) remain outstanding.

## [0.6.0] — Upgrade management and recoverability

- Added editable CLI and TUI plans for NetBox-only, plugin-only and combined upgrades, with exact target versions, compatibility warnings and explicit approval.
- Added plugin inventory, alternative version selection and safe local requirement pin review.
- Hardened upgrade execution with dependency preflight and prepared offline artifacts, verified database/media backups, persistent recovery records, service and health checks, and safe interruption handling.
- Added netbox-ops self-upgrade for supported archive installations, including official release browsing, compatibility and checksum validation, mandatory intermediate steps, transactional installation, rollback and independent recovery.
- Added a safe interactive uninstaller and weekly audit log rotation that preserves customized policies.
- Improved TUI navigation, service logs, global quit and automatic English/PT-PT locale selection.
- Added release compatibility manifests, validated release packaging and manual GitHub Release publication with safe retries.
- Expanded disposable VM coverage for database and media restores, NetBox and plugin upgrades, and self-upgrade. CI selects relevant suites for development changes and retains full release validation.

**Upgrade from v0.5.0:** Download and verify the official archive and checksum, then follow the manual replacement procedure in [self-upgrade and recovery](https://github.com/teixemf/netbox-ops/blob/v0.6.0/docs/SELF_UPGRADE_RECOVERY.md#initial-upgrade-from-v050). Do not extract over the existing installation. Python 3.12+ with venv support is required; transactional self-upgrade also requires the recognized archive layout and working Linux `pidfd_open`.

**NetBox upgrade requirements:** The executor supports reviewed NetBox 4.x releases in a recognized Git installation at `/opt/netbox`, with compatible existing Python, exact local package pins and an explicit API status URL. Archive, container and custom-path NetBox installations are not supported by the executor. Review [NetBox upgrades](https://github.com/teixemf/netbox-ops/blob/v0.6.0/docs/NETBOX_UPGRADE.md) before use.

**Validation scope and limitations:** The Ubuntu 24.04 VM suites cover core, plugin, combined and cross-minor upgrades, controlled failure, database/media restores and self-upgrade recovery with unpublished candidates. Real PostgreSQL integration also covers Ubuntu 22.04 and Rocky Linux 9, not full upgrade E2E on those platforms. Mandatory intermediate NetBox stages are covered by unit tests using hypothetical reviewed boundaries; no real major-version transition is currently approved. Automatic NetBox upgrade rollback is not implemented (#63), published-release self-upgrade pair testing remains outstanding (#132), and broader maintenance coordination is deferred (#99).

## [0.5.0] — Reliability, security and development foundations

- Hardened configuration file permissions and improved PostgreSQL restore staging and migration checks.
- Improved installation and upgrade scripts, including Python detection and failure handling.
- Refactored shared service logic and improved TUI layouts and action navigation.
- Added PostgreSQL backup and restore integration tests on Ubuntu 22.04 and Rocky Linux 9.
- Added reusable NetBox VM baselines and smoke tests for service restart, HTTP availability and database backup verification.
- Reorganized the test suite, added Ruff checks and documented a lightweight development and release workflow.

**Validation:** CI passed on Python 3.12–3.15 and with real PostgreSQL integration tests. A real NetBox VM smoke test passed for service management and database backup verification. Full end-to-end NetBox restore and recovery testing remains outstanding.

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
