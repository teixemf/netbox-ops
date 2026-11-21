# netbox-ops Tests

The tests use Python's standard-library `unittest` framework and do not require real systemd or PostgreSQL services. Some tests verify root-owned directory permissions, so the complete suite must run in a disposable environment with UID 0 (for example, a CI container), **never directly on a production server**.

## Run the full test suite

From the repository root, using Python 3.12 or later:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

To reproduce the full CI environment locally on a development machine with Docker:

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace python:3.12-bookworm \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

## Run tests for a specific domain

```bash
python -m unittest tests.backups.test_backups -v
python -m unittest tests.services.test_services -v
python -m unittest tests.tui.test_tui -v
```

## Directory layout

- `backups/`: backup creation, validation, and restore.
- `cli/`: CLI commands, argument parsing, and confirmation prompts.
- `config/`: configuration validation and loading.
- `i18n/`: translations and locale key coverage.
- `install/`: installation and symbolic link handling.
- `services/`: systemd service operations and safeguards.
- `tui/`: navigation, rendering, and TUI operations.
- `helpers/`: reusable simulated objects for tests.
- `regression/`: historical version-specific regression tests, temporarily kept under their original names for traceability.

Version-specific regression tests can be gradually moved to their respective domains in future PRs. Avoid changing assertions in the same commit that moves test files.
