# NetBox and plugin upgrades

The CLI and TUI share an editable upgrade plan and a guarded executor for
NetBox-only, plugin-only and combined upgrades. This guide describes the
development branch intended for v0.6.0, not the published v0.5.0.

## Supported setup

NetBox 4.0 and newer are in scope. NetBox 3.x and earlier, including upgrades
from 3.x to 4.x, are unsupported. New major-version transitions require a
reviewed upstream upgrade rule; no major-version transition is currently
approved. Downgrades are blocked. The reviewed prerequisite table covers
NetBox 4.0 through 4.7; newer releases require additional manual review.

- A recognized, root-controlled Git checkout of the official NetBox repository
  at `/opt/netbox`, with a normal `.git` directory, no tracked changes and
  the existing supported Python runtime. A matching stable release tag is
  preferred; an untagged HEAD requires explicit review. Archive, container,
  linked-worktree and custom-path installations are not supported by this
  executor. The NetBox Python version must satisfy each target's requirements;
  the netbox-ops interpreter is separate. Python runtime changes are not part
  of an upgrade plan.
- Configured NetBox web and RQ services, local PostgreSQL, and a media directory
  matching the running installation. Both services must be active before the
  upgrade.
- Configured plugins must be identifiable. All relevant local requirements,
  including disabled plugins and additional packages, must be exact installed
  pins unless explicitly selected for upgrade. Includes, URLs, editable
  installations, duplicate requirements and ambiguous specifications need
  manual review. Compatibility warnings still require strong confirmation.
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

## Dependency review before downtime

Every executable plan reviews the exact target core requirements and local
package pins for each stage. Target Git requirements are read from private
temporary storage without checking out the live installation. The plan shows
resolved package version changes, conflicts and uncertainty, and binds the
review to its fingerprint. For NetBox 4.7 and later it also checks PostgreSQL
`ltree` availability and privileges without creating the extension.

A confirmed dependency conflict or incomplete dependency resolution blocks
execution. An uncertain plugin compatibility declaration can still require
explicit additional approval. Recalculate the plan after resolving blockers.
Under the shared operation lock the executor rebuilds the plan, checks the
fingerprint and prepares the approved offline wheel sets before downtime.
The only source-build exception is the official core `psycopg[c]` dependency
requiring `psycopg-c`; existing build prerequisites are needed. No other
source-only plugin distributions are supported.

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
upgrade and ordinary start/restart actions for NetBox writers until an
administrator has investigated and safely resolved the interrupted operation.
Pin changes and uninstall are blocked. A restore while recovery is pending
requires both writers already stopped and does not start them or clear the
marker. The executor's controlled final start is a separate path. Do not remove it simply to retry. The executor never
automatically restores the database or claims a successful rollback (#63).

Managed upgrade subprocesses run in separate process groups. On timeout or
catchable interruption the executor requests termination, escalates to SIGKILL
and verifies that live group members are gone before releasing its lock.
SIGINT and SIGTERM are handled cooperatively, not as a guarantee of immediate
cancellation. A process stuck in uninterruptible kernel sleep can prolong the
wait; detached descendants, SIGKILL of the controlling process, host crashes
and power loss are not covered by a complete cleanup guarantee. Inspect the
actual process and service state before manual recovery. Failure diagnostics
identify a reviewed command, exit status or timeout when known, stage and
recovery location; arbitrary command output, arguments and credentials are
not logged.


## Plugin-only upgrades

`execute_plugin_upgrade` uses the same approved-plan, maintenance, backup and
recovery workflow without fetching a NetBox release or changing the Git revision.
The planner offers the newest stable plugin release or an explicit alternative;
only selected packages receive new pins.

The current backend supports exact, ordinary PyPI requirements (including
extras). Resolve ambiguous local requirements, duplicate entries or unpinned
unselected packages before execution. All enabled plugins must map to installed
distributions. Unselected local pins and comments are preserved, and installed
core dependency versions must match the current NetBox release requirements.

Before downtime the resolver checks all proposed local pins and exact core
pins using NetBox's current Python, then prepares and verifies the offline
wheel set. Dependency conflicts and incomplete resolution block execution.

Once the verified backup is captured, the executor atomically writes the
approved pins and installs the reviewed dependency set from the prepared
wheelhouse in the existing
NetBox virtualenv. Unlike the NetBox version upgrade script, this does not
rebuild the entire virtualenv. It runs pip's dependency check and verifies
installed package versions before applying Django migrations and collecting
static files. Only NetBox services are started afterward. The final HTTP status
must match the locally loaded plugin registry and unchanged NetBox release.

If installation, migrations, static collection or validation fails, retain the
recovery point and pending marker; do not restart writers automatically or
assume restoring the old requirements is sufficient after database migrations.


## Combined upgrades

`execute_combined_upgrade` uses one approved plan, one operation lock, one
verified recovery point and one maintenance window. It pins the official Git
commits and resolves the exact core and local package requirements of every
required NetBox release before downtime.

Intermediate releases retain the original plugin pins. The final release
receives the selected plugin pins after checkout, before the official
`upgrade.sh` runs. Unselected requirements are preserved, and exact package
constraints are passed to the script to prevent dependency resolution from
silently changing approved versions. The combined backend uses the existing
Python runtime and requires wheels for installation. Before maintenance it
prepares one complete wheel set per stage, including pip and wheel, in a
private temporary directory. The only source-build exception is `psycopg-c`
when required by the official NetBox core pin `psycopg[c]`. This requires an
existing C compiler, Python headers and PostgreSQL client development files;
netbox-ops does not install system build dependencies. Both upstream
installation calls use only that local wheel set, so an unavailable package
fails before service interruption and dependency versions cannot change
between preflight and installation. Temporary wheels are removed on exit.

Before maintenance the refreshed plan verifies compatibility for each stage's
exact versions. Before each stage the executor verifies the live PostgreSQL
and Redis prerequisites and reruns dependency resolution using prepared
artifacts. A new conflict or newly unverified requirement stops
the operation. Each completed stage must pass dependency, package-version,
Django migration and plugin registry checks. The writers remain stopped
between stages; HTTP and worker health are verified after the final start.

Recovery metadata contains the planned Git revisions and plugin versions for
each stage. Failure reports include the number and release of the last
validated stage, if any. Review the pending marker, audit log and recovery
record before performing manual recovery. A failed stage may already have
applied migrations; there is no automatic full rollback.


## CLI and terminal interface

The existing NetBox, plugin and combined upgrade entries open an editable
plan in the TUI. Choose a target release and select plugin packages, optionally
pinning a specific stable release. Recalculate after edits, review the compact
summary and open the compatibility details before approving. An edited
selection invalidates the previous plan. Execution requires the literal
`UPGRADE` confirmation and, for uncertain plans, a second `UNCERTAIN`
confirmation. The API authorization header is optional and entered without echo.

The CLI previews a plan without changing the installation:

```sh
netbox-ops upgrade inspect
netbox-ops upgrade versions example-plugin
netbox-ops upgrade pins
netbox-ops upgrade netbox --version 4.4.2 --details
netbox-ops upgrade plugins --plugin example-plugin==2.0.0
netbox-ops upgrade stack --netbox-version 4.4.2 --plugin example-plugin
```

Use `--all` for plugins or `--plugins all` for a combined plan to select
all discovered configured or declared plugin packages. Repeat `--plugin`
to select multiple packages. Omitting a plugin version selects its latest
stable release. Review the resulting versions and warnings, not just the
requested selection. Published alternatives are candidates, not verified
NetBox compatibility. Choose a version and recalculate the plan.

Use `upgrade pins --plugin example-plugin` or `upgrade pins --all` to review
exact proposed corrections. To write the displayed corrections, repeat the
same selection with `--apply --approve-pins <fingerprint>`. The TUI offers
pin selection, exact before/after review and a literal `PIN` confirmation.
Pin review alone never writes a file; a stale review is rejected under the
operation lock. Recalculate the upgrade plan after applying pins.

To execute, repeat the same preview command with `--execute`,
`--approve-plan <fingerprint>` (copy the complete displayed fingerprint)
and `--health-url https://netbox.example/api/status/`. Add
`--accept-unknowns` only after reviewing uncertain compatibility. The
fingerprint must match the freshly generated plan; a changed plan requires
a new review. Use `--prompt-authorization` if API authentication is needed.
Never place credentials in the URL or command arguments.

Progress counts validated stages, not estimated time or downloaded bytes.
A successful execution displays the recovery directory. On failure, consult
the safe diagnostic, audit log and recovery record before taking further action.
See the disposable VM validation below.


## Disposable VM validation

The `VM E2E - NetBox upgrades` workflow reuses the pinned, checksum-verified
Ubuntu 24.04 baseline. This is not full upgrade validation on Rocky Linux;
Rocky Linux 9 is covered separately by PostgreSQL integration tests. Each matrix job creates an independent QCOW2 overlay and a
fresh database with a real upstream Git release; it never downgrades the
baseline database or publishes a new image.

The scenarios cover NetBox 4.7.2 to 4.7.3, NetBox Inventory 2.7.0 to 2.7.1
without a NetBox change, a combined upgrade, a cross-minor upgrade from
4.4.10 to 4.7.3, and an injected final health validation failure after real
migrations. Required intermediate stages remain covered by unit tests using
hypothetical reviewed major boundaries until a supported real transition
is available.

Each case rejects a wrong plan fingerprint before downtime, verifies the
protected combined archive and recovery metadata, and checks real database
and media fixtures, Git revision, configuration integrity and unchanged
PostgreSQL, Redis and Nginx service start timestamps. Success requires the
expected NetBox status and RQ workers; the failure scenario instead requires
stopped writers, a pending marker and a recovery diagnostic. Failure injection
is confined to the test process; no production failure switch exists.

Run the workflow on a pull request to dev or manually through GitHub Actions.
Never run the guest fixture on an ordinary host. The script requires the
explicit CI marker and QEMU/KVM virtualization, and the host harness checks
the unchanged baseline hash.
