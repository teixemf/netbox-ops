# Initial Python coverage baseline

Measured on 2026-11-25 for #159, targeting the release after v0.6.0.

- Source commit: `f9b31d3a43a6ee4143219a04c5a8031cb3173a97`.
- [CI run](https://github.com/teixemf/netbox-ops/actions/runs/45617101996) and [coverage artifact](https://github.com/teixemf/netbox-ops/actions/runs/45617101996/artifacts/15021199668).
- Python 3.12 in the root user Debian Bookworm container; coverage.py 7.10.7.
- Command: `python -m coverage run --branch --source=netbox_ops -m unittest discover -s tests -p "test_*.py" -v`.
- 805 tests discovered: 804 passed, one skipped (logrotate executable required), zero failures; 22.315 seconds under instrumentation.
- 9,022 executable statements, 1,516 missed; statement coverage 83.2%.
- 3,682 branch opportunities; 709 partial branches (not the total missed branches). The combined statement/branch coverage reported by coverage.py is 80%, rounded.

This is a unit/regression baseline only. It does not measure the entrypoint outside the package, shell scripts, CI tooling, uninstrumented subprocesses, real PostgreSQL integration or VM E2E execution. The separately measured branch-only percentage is not available from the text log; use the JSON report for exact counts. The artifact expires, so the table below preserves the initial text report counts.

## Per-module results

| Module | Statements | Missed statements | Branches | Partial branches | Combined coverage |
| --- | ---: | ---: | ---: | ---: | ---: |
| `netbox_ops/__init__.py` | 1 | 0 | 0 | 0 | 100% |
| `netbox_ops/backups.py` | 883 | 186 | 372 | 77 | 75% |
| `netbox_ops/cli.py` | 409 | 156 | 138 | 23 | 60% |
| `netbox_ops/config.py` | 163 | 21 | 70 | 18 | 82% |
| `netbox_ops/i18n.py` | 25 | 1 | 6 | 1 | 94% |
| `netbox_ops/journal.py` | 168 | 36 | 48 | 16 | 75% |
| `netbox_ops/logrotate_policy.py` | 55 | 11 | 16 | 4 | 76% |
| `netbox_ops/menu.py` | 1 | 0 | 0 | 0 | 100% |
| `netbox_ops/netbox_version.py` | 34 | 4 | 16 | 2 | 88% |
| `netbox_ops/platform.py` | 108 | 16 | 44 | 9 | 80% |
| `netbox_ops/plugin_compatibility.py` | 494 | 60 | 218 | 43 | 85% |
| `netbox_ops/plugin_inventory.py` | 294 | 37 | 120 | 27 | 85% |
| `netbox_ops/plugin_pin_ui.py` | 66 | 6 | 20 | 3 | 90% |
| `netbox_ops/plugin_upgrade.py` | 248 | 33 | 110 | 26 | 82% |
| `netbox_ops/restore_environment.py` | 161 | 21 | 70 | 12 | 85% |
| `netbox_ops/self_upgrade_archive.py` | 100 | 12 | 52 | 7 | 88% |
| `netbox_ops/self_upgrade_catalogue.py` | 51 | 4 | 12 | 1 | 92% |
| `netbox_ops/self_upgrade_compatibility.py` | 74 | 2 | 32 | 2 | 96% |
| `netbox_ops/self_upgrade_download.py` | 57 | 2 | 18 | 2 | 95% |
| `netbox_ops/self_upgrade_handoff.py` | 284 | 68 | 108 | 21 | 73% |
| `netbox_ops/self_upgrade_helper.py` | 97 | 7 | 22 | 3 | 92% |
| `netbox_ops/self_upgrade_installation.py` | 134 | 18 | 64 | 15 | 82% |
| `netbox_ops/self_upgrade_manifest.py` | 85 | 5 | 28 | 2 | 94% |
| `netbox_ops/self_upgrade_prepare.py` | 73 | 7 | 20 | 4 | 86% |
| `netbox_ops/self_upgrade_releases.py` | 120 | 3 | 36 | 4 | 96% |
| `netbox_ops/self_upgrade_result.py` | 59 | 6 | 12 | 3 | 87% |
| `netbox_ops/self_upgrade_runtime.py` | 101 | 21 | 50 | 13 | 75% |
| `netbox_ops/self_upgrade_stage.py` | 85 | 13 | 46 | 13 | 80% |
| `netbox_ops/self_upgrade_transaction.py` | 155 | 17 | 66 | 15 | 85% |
| `netbox_ops/self_upgrade_ui.py` | 234 | 62 | 90 | 11 | 72% |
| `netbox_ops/services.py` | 308 | 69 | 110 | 19 | 74% |
| `netbox_ops/tui.py` | 1769 | 330 | 818 | 167 | 79% |
| `netbox_ops/uninstall.py` | 264 | 54 | 122 | 18 | 77% |
| `netbox_ops/upgrade_checkout.py` | 47 | 2 | 18 | 2 | 94% |
| `netbox_ops/upgrade_combined.py` | 184 | 37 | 72 | 21 | 72% |
| `netbox_ops/upgrade_diagnostics.py` | 50 | 6 | 18 | 4 | 85% |
| `netbox_ops/upgrade_discovery.py` | 184 | 20 | 82 | 17 | 86% |
| `netbox_ops/upgrade_execution.py` | 95 | 13 | 68 | 11 | 85% |
| `netbox_ops/upgrade_git.py` | 34 | 0 | 8 | 0 | 100% |
| `netbox_ops/upgrade_health.py` | 67 | 4 | 24 | 3 | 92% |
| `netbox_ops/upgrade_maintenance.py` | 35 | 0 | 4 | 0 | 100% |
| `netbox_ops/upgrade_orchestration.py` | 157 | 11 | 36 | 6 | 91% |
| `netbox_ops/upgrade_plan.py` | 310 | 17 | 136 | 18 | 92% |
| `netbox_ops/upgrade_preflight.py` | 71 | 6 | 20 | 4 | 89% |
| `netbox_ops/upgrade_process.py` | 137 | 20 | 52 | 8 | 84% |
| `netbox_ops/upgrade_recovery.py` | 118 | 9 | 40 | 9 | 89% |
| `netbox_ops/upgrade_runtime.py` | 82 | 7 | 38 | 5 | 90% |
| `netbox_ops/upgrade_services.py` | 26 | 0 | 2 | 0 | 100% |
| `netbox_ops/upgrade_stage.py` | 66 | 28 | 26 | 2 | 50% |
| `netbox_ops/upgrade_ui.py` | 199 | 48 | 84 | 18 | 74% |
| **Total** | **9022** | **1516** | **3682** | **709** | **80%** |

## First review priorities

- `upgrade_stage.py`: 50%; lines 26–62 (base interpreter validation) are unexecuted in the unit run. The stage tests inject a mock for that function. Exercise acceptance and rejection of unsafe paths, ownership, permissions, executable checks, version mismatch and failed probes directly.
- `self_upgrade_handoff.py`: 73%; review handoff, recovery and helper entrypoint failure paths; some require subprocess or VM instrumentation.
- `upgrade_combined.py`: 72%; review failures in server checks, preparation and rechecks.
- `backups.py`: 75%; review archive validation, database staging, service transitions and rollback, distinguishing mocked tests from real integration coverage.
- `cli.py` (60%) and `tui.py` (79%): review destructive-command routing and confirmations first; do not prioritize UI rendering solely by missed-line count.

These are coverage review candidates, not confirmed defects.

## Proposed policy (not enforced yet)

- Keep unittest and the existing conservative CI suite selection.
- Start by comparing like-for-like full runs. Capture exact statement and branch counts separately in JSON, pin the measurement environment, and track the baseline against the base revision rather than comparing selected tests to the global baseline.
- Evaluate diff-cover for changed executable lines; explicitly handle no executable changes, missing coverage data and renamed files. Do not claim changed-branch coverage from a line-only tool.
- Start differential coverage in reporting mode; propose thresholds after examining actual PRs. Establish no-regression checks on comparable complete runs, with explicit review for justified exceptions.
- Keep the full matrix for release validation and for uncertain or shared changes. Benchmark incremental test selection against the current 22-second instrumented suite before adding complexity. Keep integration and VM safety gates.
- Review test assertions and failure scenarios; coverage alone does not establish correctness.

Re-measure against the final v0.6.0 baseline before integrating the next-release policy. Do not merge the experimental branch-specific workflow into v0.6.0.

## Follow-up: base interpreter tests

[Run 45618730189](https://github.com/teixemf/netbox-ops/actions/runs/45618730189), commit `e536a3dfb5fea7f98a8e775ba12502e98e8aca2e`: all seven new tests passed. 812 tests discovered, 811 passed, one skipped; 22.359 seconds. The package still has 9,022 statements; missed statements decreased from 1,516 to 1,490. Combined coverage rose from 80% to 81% (rounded), and upgrade_stage.py improved from 50% to 95%. Three statements remain uncovered in that module. The source code is unchanged.

The next experiment adds separate exact totals and diff-cover 10.6.0 in reporting mode against the fixed initial baseline. A temporary Git repository verifies a 50% changed-line result with one covered and one missed line and exercises threshold failure. This is not yet a PR quality gate: base SHA selection, missing reports, renames and no-executable-change cases still require validation. No application changes should yield N/A, not an artificial 100% score.

## Follow-up: differential coverage smoke test

[Run 45619516786](https://github.com/teixemf/netbox-ops/actions/runs/45619516786) succeeded. Exact totals: 7,534/9,022 statements (83.51%), 2,704/3,682 branches (73.44%), 80.59% combined. 812 tests, one skipped, 21.400 seconds. Diff-cover reported 50% for the two-line fixture, accepted 50% and rejected 51%. The application diff was N/A.

A conservative JSON-based differential reporter is now proposed alongside diff-cover. It checks the PR merge parents against event SHAs, refuses missing coverage for changed Python files, reports N/A for no changed executable lines, and deliberately treats renamed Python files as new files (all their executable lines count). It reports line coverage only and enforces no percentage threshold. Its isolated Git fixture tests and branch workflow still need validation. The package is the measurement scope, not shell scripts or the top-level entrypoint. A future PR workflow must generate coverage at the same checked-out SHA and use the actual event payload, not a moving ref.

## Follow-up: guarded report and integration experiment

[Run 45620697014](https://github.com/teixemf/netbox-ops/actions/runs/45620697014) passed all eight new reporter tests: 820 tests discovered, 819 passed, one skipped; 23.423s. Exact totals were 7,532/9,022 statements (83.48%), 2,704/3,682 branches (73.44%), 80.57% combined.

The two-statement difference from run 45619516786 is isolated to upgrade_process.py lines 90–91, the handler for a disappearing /proc entry. Real process enumeration can encounter this race nondeterministically. New mock-based tests exercise the disappearing-entry and unreadable-entry cases directly without replacing the existing real process-group tests. Validate repeatability before enforcing a no-regression threshold.

The branch-only coverage workflow now also proposes separate PostgreSQL coverage jobs for the same Ubuntu 22.04 and Rocky Linux 9 disposable containers used by the existing CI. They reuse tests/integration/run_postgres.sh and the distribution Python, installing coverage.py 7.10.7 only in the disposable container. Record the interpreter version and keep each report separate: these runs use different Python versions from the unit baseline and their percentages cannot be added or compared directly. They still mock some systemd, audit and Django behavior and do not measure NetBox VM E2E or uninstrumented subprocesses. Validation is pending.

## Follow-up: PostgreSQL results and VM pilot

[Run 45621132425](https://github.com/teixemf/netbox-ops/actions/runs/45621132425) passed all three jobs. Unit run: 823 tests, one skipped, 23.151s, 7,536/9,022 statements (83.53%), 2,704/3,682 branches (73.44%), 80.60% combined. All three new deterministic process scan tests passed; upgrade_process.py reached 86% combined.

| Integration environment | Python | PostgreSQL | Tests | Combined package | backups.py | restore_environment.py |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Ubuntu 22.04 | 3.10.12 | 14.24 | 19 | 7% | 58% | 11% |
| Rocky Linux 9 | 3.9.25 | 13.23 | 19 | 7% | 58% | 11% |

Both integration jobs passed. Their global package coverage is intentionally low because the tests target backup/restore; do not add the percentages. The distro interpreters are older than the supported application runtime. These results do not prove application compatibility with those interpreters.

A separate branch-only VM pilot measures the installed CLI during the existing baseline services, backup and restore E2E suite. It installs pinned coverage.py in a separate temporary tool venv, adds an opt-in startup hook only to the disposable application's private venv, and exports reports before VM shutdown. The launcher and application source remain unchanged. Reports exclude direct system-Python calls, uninstrumented subprocesses and self-upgrade; instrumentation may affect timing or behavior, so existing uninstrumented E2E remains authoritative. The pilot is deliberately not used with self-upgrade or upgrade scenarios. Its execution is pending. Do not merge temporary branch triggers into the release.
