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

The separate `integration` CI job runs the same suite on Ubuntu 22.04 and Rocky Linux 9. It runs independently from unit tests and Ruff. See [integration/README.md](integration/README.md) for the local Docker commands and what is simulated.

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
