# netbox-ops Tests

Tests use Python's standard-library `unittest` framework. There are two independent suites:

- **Unit and regression tests:** no real PostgreSQL or systemd required; run in the existing Python matrix.
- **Integration tests:** real PostgreSQL backups, restores and staging with disposable media, in Ubuntu 22.04 and Rocky Linux 9 containers. NetBox is not installed.

Some tests require UID 0 and validate root-owned paths. **Only run them in disposable environments, never directly on a production server.**

## Unit and regression tests

From the repository root with Python 3.12 or later:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

To reproduce the unit-test CI environment with Docker:

```bash
docker run --rm -v "$PWD:/workspace:ro" -w /workspace python:3.12-bookworm \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

To run specific domains:

```bash
python -m unittest tests.backups.test_backups -v
python -m unittest tests.services.test_services -v
python -m unittest tests.tui.test_tui -v
```

## PostgreSQL integration tests

The separate `integration` CI job runs the same suite on Ubuntu 22.04 and Rocky Linux 9 when database, backup/restore, shared runtime or integration-test changes need it. It runs independently from unit tests and Ruff. See [integration/README.md](integration/README.md) for the local Docker commands and what is simulated.

## CI test selection

Development PRs and pushes classify the complete Git diff against the event's base commit using `scripts/ci/test_scope.py`. Each workflow uses the same decision. Existing test job names stay unchanged; irrelevant jobs are skipped instead of leaving a required check pending.

- Markdown-only changes: lightweight scope and release-version metadata checks.
- Python, translations and unit tests: the Python matrix and Ruff.
- Backup/restore and PostgreSQL changes: the real PostgreSQL matrix and affected NetBox VM suites.
- NetBox/plugin upgrades: NetBox upgrade VM suite.
- Self-upgrade and package changes: relevant archive checks and self-upgrade VM suite.
- Shared runtime, installation, CI routing, version changes or unknown paths: conservative broader coverage.

The classifier combines all affected suites. Renames are treated as deletion plus addition. A missing comparison, classifier failure, manual run, release workflow call or PR/push to main uses full coverage. The upgrade VM suites run on relevant development PRs, and on integrated pushes that change the version, workflow or routing policy. Manual VM runs always execute the full selected workflow. Release validation on the exact candidate remains mandatory.

When adding or moving application modules or test fixtures, review the scope rules and update `tests/test_ci_scope.py` if needed. Do not narrow a shared dependency without checking its callers.

## Directory layout

- `backups/`: offline backup, validation and restore tests.
- `integration/`: real PostgreSQL backup/restore tests and container setup.
- `cli/`: CLI commands and confirmation prompts.
- `config/`: configuration validation and loading.
- `i18n/`: translations and locale keys.
- `install/`: installation and package handling.
- `services/`: systemd service logic.
- `tui/`: navigation and rendering.
- `helpers/`: reusable simulated objects.
- `regression/`: historical version-specific tests.

Version-specific regression tests can gradually move into their respective domains in future PRs. Avoid changing assertions in the same commit that moves files.
