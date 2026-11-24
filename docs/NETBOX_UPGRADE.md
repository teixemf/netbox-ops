# NetBox upgrade backend

The NetBox-only executor in `netbox_ops.upgrade_orchestration` consumes an
approved upgrade plan. CLI/TUI wiring and real upgrade VM tests are separate
work (#87 and #88).

## Supported setup

- A recognized, root-controlled Git checkout of the official NetBox repository
  at `/opt/netbox`, with the existing supported Python runtime.
- Configured NetBox web and RQ services, local PostgreSQL, and a media directory
  matching the running installation. Both services must be active before the
  upgrade.
- Configured plugins must be identifiable and their installed versions exactly
  pinned in `local_requirements.txt`. Review unpinned or ambiguous requirements
  first. Compatibility warnings still require the plan's strong confirmation.
- Provide the installation's explicit `/api/status/` URL. Local HTTP without
  credentials or HTTPS with normal certificate validation is supported. A
  required Authorization header is supplied in memory, never in the recovery
  record or audit messages.

The caller supplies a fresh plan builder and the explicit approval, including
the plan fingerprint and acknowledgement of uncertain compatibility. The
executor verifies that the service and upgrade environments refer to the same
database, media and configured plugins, and that the selected base Python will
survive the official script's virtualenv rebuild. It does not install or
upgrade system Python, PostgreSQL, Redis or Nginx.

## Execution and recovery

The executor holds the shared operation lock, creates a persistent pending
marker and stops only NetBox writers. It captures and verifies a combined
database/media backup before changing the checkout, and stores the local
configuration, plugin requirements, old Git revision and exact stage commits
in a private `pre-upgrade-*` directory under the configured backup directory.
Recovery directories are root:root 0700; files are root:root 0600. They are
separate from the ordinary backup listing.

Each required intermediate release is fetched from the official remote,
checked against its pinned commit, checked out without forcing or overwriting
ignored files, and processed by the official NetBox upgrade script. The
executor checks Django, migrations and plugin packages before moving to the
next stage. Writers stay stopped across intermediate stages. The final start
must pass service checks and the NetBox HTTP status, plugin and RQ worker
checks before the pending marker is removed.

If a step fails, the operation stops and reports the phase and recovery
directory, if available. Check the NetBox service state and the audit log
(`/var/log/netbox-ops/operations.log`) before taking further action. Inspect
`recovery.json` and verify the combined archive before relying on it. The
original checkout revision and saved requirements are recovery evidence, not
a database rollback. Once migrations may have run, recovering only the Git
checkout or Python environment is unsafe.

A pending marker at `/var/lib/netbox-ops/upgrade.pending` blocks another
upgrade until an administrator has investigated and safely resolved the
interrupted operation. Do not remove it simply to retry. The executor never
automatically restores the database or claims a successful rollback.
