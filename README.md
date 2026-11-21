# netbox-ops v0.4.0

Local NetBox administration utility for **RHEL and Debian (including derivatives)**, with PostgreSQL and Redis running on the VM. It uses **Python 3.12 in a dedicated virtual environment** (`/opt/netbox-ops/venv`), independent of NetBox's Python environment. The application supports Portuguese (Portugal) and English through both a curses TUI and a CLI.

> **Validation status:** automated tests and terminal emulation have been completed, but **real restores have not yet been tested on RHEL or Debian**. Do not restore a production installation before testing rollback in a disposable VM with non-production data or a suitable snapshot.

## Installation and updates

Copy the ZIP archive to the VM and run:

```bash
sudo unzip -o netbox-ops-v0.4.0.zip -d /opt
sudo /opt/netbox-ops/install.sh
sudo netbox-ops --version
sudo netbox-ops
```

The installer rebuilds **only** the `netbox-ops` virtual environment. It does not install system-wide libraries, change NetBox's environment, or restart NetBox services. It maintains the launcher at `/usr/local/sbin/netbox-ops` and the `/usr/sbin/netbox-ops` symlink required by restricted `sudo` paths. Configuration and backups under `/etc/netbox-ops` and `/var/lib/netbox-ops` survive Git-based reinstalls.

External requirements: **Python 3.12** with `venv`/`ensurepip` (regardless of the system's default Python version), `systemd`, `journalctl`, `runuser`, PostgreSQL client utilities (`psql`, `pg_dump`, `pg_restore`, `createdb`, `dropdb`), and `curses`. The application itself uses only Python's standard library. The installer **does not run apt/dnf**, install or modify PostgreSQL, Redis, or NetBox, or change system Python.

### RHEL / Debian compatibility (0.4.0)

Linux distribution detection uses `/etc/os-release`. Both the RHEL family (including Rocky Linux and AlmaLinux) and the Debian family (including Ubuntu) are supported. Both require a local NetBox installation with `systemd`, a local PostgreSQL server, the `postgres` service account, and VM-appropriate paths; NetBox defaults to `/opt/netbox`.

| Component | RHEL | Debian |
| --- | --- | --- |
| PostgreSQL service | `postgresql.service` or `postgresql-N.service` (PGDG) | `postgresql@N-main.service`, resolved through `pg_lsclusters` |
| Redis service | `redis.service` (fallback: `redis-server.service`) | `redis-server.service` (fallback: `redis.service`) |
| PostgreSQL CLI | Binaries from `PATH` or `/usr/pgsql-N/bin/` (PGDG) | Binaries from `/usr/bin/` and `postgresql-common` tools |
| Python 3.12 | `/usr/bin/python3.12` or `/usr/local/bin/python3.12` | `/usr/bin/python3.12` or `/usr/local/bin/python3.12` |
| NetBox / Nginx services | Standard `netbox.service`, `netbox-rq.service`, and `nginx.service` units | Same; override in JSON if required |

**Debian warning:** `postgresql.service` can be an umbrella unit that does not control a specific database cluster. The application tries to identify the actual cluster using `pg_lsclusters`. If multiple clusters or multiple versioned RHEL units are found, it **does not select one arbitrarily**. Specify `services.postgresql` and `backup.postgres_port`, and validate the configuration before restoring. The PostgreSQL client directory may also be pinned using `backup.pg_bin_directory` when the desired PGDG version is not on `PATH`.

**Configuration:** on new installations, `config.example.json` contains `"services": {}` to enable discovery. Explicit units in `/etc/netbox-ops/config.json` always take precedence. The installer **does not overwrite** an existing JSON configuration. When migrating from RHEL to Debian, review persisted unit overrides, especially PostgreSQL and Redis.

```json
{
  "services": {
    "postgresql": "postgresql@16-main.service",
    "redis": "redis-server.service"
  },
  "backup": {
    "directory": "/var/lib/netbox-ops/backups",
    "database": "netbox",
    "owner": "netbox",
    "media_directory": "/opt/netbox/netbox/media",
    "postgres_port": 5432
  }
}
```

This is an **example for Debian with PostgreSQL cluster 16/main on port 5432**. Adjust the version, port, and units to match the actual installation. The example included in the ZIP enables automatic discovery when no overrides are specified.

**Non-destructive diagnostics:**

```bash
sudo netbox-ops platform
sudo netbox-ops services status
```

The `platform` command reports the Linux family, selected Python interpreter, detected units, and PostgreSQL client binaries. It **does not perform operations or create data**. If Debian's default Python is not 3.12, install Python 3.12 with `venv` support first and set `PYTHON_BIN` if necessary. The installer rejects other versions instead of silently using 3.11 or 3.13.

## Visual design in v0.4.0

The **Palette A — NetBox Dark** theme uses teal highlights, dark blue-green surfaces, and semantic result colors. The curses interface approximates the theme using xterm-256 colors (true-color support is not required), provides an eight-color fallback, and honors `NO_COLOR`. The storage indicator is a **Braille donut chart** showing backups, other data, and free space according to the actual proportions of the filesystem hosting backups. If the terminal does not support these Unicode characters, the numeric values and legend remain authoritative.

Progress-driven operations finish **on the same screen**, keeping the task list and final result or error visible. Press **Enter or Esc** to return to the previous menu after completion. Avoid interrupting operations while they are running. Progress values represent completed stages, files, or services—not elapsed time or an estimate of transferred bytes.

## TUI

The **main menu** includes service management, backup management, update checks, NetBox/plugin upgrades, and self-upgrade. Update-related entries remain **unimplemented**. The right panel displays each service's **current state, version, CPU, and memory**, refreshing approximately every three seconds. The storage area shows a **Braille donut chart** with legend colors matching its segments (teal for backups, blue for other data, gray for free space). The segments represent backup bytes, other occupied bytes on the same filesystem, and free bytes. The center percentage is the share of **total filesystem capacity occupied by backups**, not a share of the files on disk.

CPU usage is calculated from the difference between readings of `CPUUsageNSec` (or the cgroup v2 counter) and can exceed 100% when a service uses multiple CPU cores. Memory represents cgroup usage, not the RSS of an individual process. A missing kernel/systemd metric is displayed as `—`. There is no historical collection, SQLite database, historical performance graph, collector service, or timer.

### Navigation

- `↑/↓` navigate; `Enter` opens; `Esc` first clears the highlight and then returns to the previous screen. On the main menu, `Esc` **does not exit**; `Q` exits only from the main menu.
- Under **Manage Services**, `Space` toggles selection and `Tab` switches between the service list and the horizontal action bar **at the bottom** (`Start`, `Stop`, `Restart`, `Enable`, `Disable`). `Enter` expands logs for the focused service or runs the selected action (changes require confirmation). The right panel shows state, startup mode, PID, current CPU/RAM, version, and uptime. **Logs appear in a lower panel rather than the metrics panel.** The standard view displays the **last 50 log lines** and a **Live log: ON/OFF** option in the footer; `L` also toggles live mode. `Enter` on a service expands the log panel, with `PgUp/PgDn` for scrolling, `L` for live mode, `N` to choose 1–500 lines, and `F5` to refresh. Initial log loading is asynchronous; live mode uses non-blocking `journalctl --follow`. The CLI continues to support `services logs`.
- Actions that have no effect in the current service state are disabled. In a mixed batch, only eligible services are acted upon; skipped services are identified before confirmation. `enable/disable` do not start or stop services.
- Versions for NetBox and NetBox RQ (the same application version), Nginx, PostgreSQL server, and Redis are fetched **once per netbox-ops session** and cached in memory. Queries run in the background; pending values show `…`. **NetBox's version is read from `/opt/netbox/netbox/release.yaml`.** `manage.py --version` returns the **Django** version and is no longer used to identify NetBox. Unknown versions show `—`. Version caching can be invalidated when upgrade support is implemented.
- Under **Manage Backups**, `↑/↓` move the focus; `Space` toggles items for batch operations; `*` or `S` selects/deselects all; and `Tab` switches between the list and the **horizontal action bar below the panels**. In the action bar, `←/→` select an operation, and **`Enter` runs only the focused operation**. Pressing Enter in the list does not open another detail screen. Table headings are centered, and the label column shows `MANUAL` or `PRÉ-REST.` (the pre-restore label in the Portuguese UI). The right panel **always follows the focused row**; selecting one or more backups does not prevent previewing other backups. Batch actions operate **only on marked items** and show a count/size summary when space permits. Unavailable actions are dimmed (for example, restore when several files are selected). The action bar occupies one row in wide terminals and wraps in narrow ones, near the footer. When terminal height permits, the same Braille storage chart is displayed below the list. Shortcuts remain `C` to create, `R` to restore **one file**, `V` to verify all marked files (or the focused file), `D` to delete marked files (or the focused file), and `F5` to refresh. Although `*` selects/deselects all, there is no separate **Delete All** action. Destructive operations always require confirmation; opening a menu does not initiate them. Invalid confirmation shows an **alert** and returns without acting after Enter/Esc. A `full` backup has a separate scope-selection screen (database only, media only, or both), using arrow keys and Enter; there is no implicit default.
- Initial backup listing reads **only names, sizes, and filesystem timestamps**. Selected-item metadata is fetched once and **cached during the menu session**, with invalidation when the file or manifest changes. New `media` and `full` backups include small **JSON sidecars** (`.tar.gz.json`) for near-immediate previews. Older archives without sidecars may take longer to show details, but background loading **does not block arrow-key navigation**. A manifest alone never proves integrity: a backup is marked verified only after a successful `V` operation.
- `F5` refreshes state where available; `L` changes the language on the main screen. `--no-tui` offers a simplified text menu for incompatible terminals.

### Progress bars

Creating, verifying, restoring, and deleting backups uses progress measured by **completed stages or files**, not estimated bytes processed by `pg_dump`, TAR, or `pg_restore`. A task list under the bar marks each task as running, completed, or failed. Batch verification/deletion shows the names of processed archives; service management similarly lists completed services and the current one. The progress screen remains visible on completion with the final status and any errors. Enter/Esc returns to management without opening a redundant results screen. **Do not close the terminal during a restore.**

## What's new in v0.4.0

- **RHEL and Debian:** Linux family detection, PostgreSQL/Redis systemd unit discovery, and PostgreSQL executable discovery; explicit overrides are respected, and Debian restores use the selected cluster's port.
- **Python 3.12 required:** automatic lookup in `/usr/bin` and `/usr/local/bin` with optional `PYTHON_BIN`. Neither NetBox nor system Python is modified.
- **TUI:** invalid confirmations show an alert; backup labels distinguish `MANUAL` and pre-restore backups; the detail panel follows the cursor; and the storage donut uses available space on the left.
- **`full` restore:** clearer selection of database, media, or both, requiring explicit navigation, Enter, and an additional confirmation.
- **Compatibility:** retains the v0.3.6 backup/restore engine and service/Redis policy. Isolated tests **do not replace real-world validation** on either OS.

## Fixes in v0.3.6

- **List ordering:** `db`, `media`, `full`, and `legacy` entries are sorted by UTC timestamps in their names, newest first; unrecognized names fall back to modification time. Listing does not open archives.
- **Verify:** a prominent action-bar button follows Create. `V` verifies the focused backup or all selected backups. Legacy archives support **integrity-only verification**, not restore.
- **Delete All:** removed from the TUI and CLI. Users can still explicitly select and delete multiple backups with confirmation.
- **Database-only restore from a `full` backup:** fixed extraction of `database/netbox.dump` before staging/import. Also fixed restoring a single marked backup when no row has focus. If restore is unavailable, the TUI explains whether metadata is pending, multiple backups are marked, or the archive is incompatible.
- **Security:** checksum and compatibility checks and confirmation remain mandatory. Redis policy and on-VM PostgreSQL restore validation were not changed.

## Backup policy in v0.4.0

| Type | Output files | Contents |
| --- | --- | --- |
| `db` | `netbox-db-....dump` and `.dump.json` | PostgreSQL database and integrity manifest |
| `media` | `netbox-media-....media.tar.gz` and `.media.tar.gz.json` | **Only** `/opt/netbox/netbox/media` (images, attachments, uploads) |
| `full` | `netbox-full-....full.tar.gz` and `.full.tar.gz.json` | **Database + media**, manifest, and hashes |

This release does **not** back up `configuration.py`, `ldap_config.py`, `local_requirements.txt`, Gunicorn, Nginx, certificates, scripts, reports, SELinux, Redis/PostgreSQL configuration, or other OS settings. Only the `media` artifact is accepted for file backup/restore. An earlier `/etc/netbox-ops/config.json` containing `backup.artifacts` is still read, but only its `media` entry is used; other entries are ignored. The recommended configuration uses `backup.media_directory` (see `config.example.json`).

**Automatic pre-restore safeguards:** before replacing the database and/or media, netbox-ops creates a safety backup for each component about to change. These files contain `-pre-restore-` in their name and `purpose: pre_restore` in their manifest and JSON sidecar. The Portuguese TUI shows the label `PRÉ-REST.`, and details identify the backup as an automatic pre-restore safety copy. Manually created backups use `purpose: manual`. Automatic backups created by older versions are not retroactively labeled, because their origin cannot be proven from the available metadata. Safety backups are regular backup files and can be verified, restored, or deleted under the usual rules.

New sidecars are for quick previews only and **do not replace integrity verification**. Deleting a backup also deletes its sidecar.

Backups are stored under `/var/lib/netbox-ops/backups` (owned by `root`, directory permissions `0700`, file permissions `0600`). A `.dump` is a PostgreSQL custom-format archive (`pg_dump -Fc`); plain SQL dump imports are not supported. **Database backups can still contain internal and personal data**, even though configuration files and LDAP credentials are excluded. Repeated warnings are not shown in the menu; applicable internal access-control policies must be enforced.

A `db` backup uses a PostgreSQL snapshot and does not stop services. A `media` backup copies files while the application is active and can be inconsistent if uploads occur concurrently. A `full` backup temporarily stops **NetBox and NetBox RQ**, preserves their previous state, and restarts only services that were running before the operation, coordinating database and media capture.

## CLI

```bash
# Service queries and actions
sudo netbox-ops services status
sudo netbox-ops services logs --service netbox --lines 50
sudo netbox-ops services manage
sudo netbox-ops services restart --service netbox
sudo netbox-ops services enable --service netbox

# Backup management
sudo netbox-ops backup manage
sudo netbox-ops backup db
sudo netbox-ops backup media
sudo netbox-ops backup full
sudo netbox-ops backup list
sudo netbox-ops backup list --kind full
sudo netbox-ops backup verify --kind media --file /var/lib/netbox-ops/backups/NAME.media.tar.gz

# Restores: interactive confirmation is required
sudo netbox-ops restore db --file /path/to/backup.dump
sudo netbox-ops restore media --file /path/to/backup.media.tar.gz
sudo netbox-ops restore full --file /path/to/backup.full.tar.gz --only db
sudo netbox-ops restore full --file /path/to/backup.full.tar.gz --only media
sudo netbox-ops restore full --file /path/to/backup.full.tar.gz --only all

# Deletion (explicit confirmation)
sudo netbox-ops backup delete --file /var/lib/netbox-ops/backups/NAME.dump
```

For automated restores, the CLI requires **both** `--yes` and `--acknowledge-data-loss` (never just one). Service CLI operations involving PostgreSQL/Redis require `--allow-critical` when `--yes` is used.

### Restoring a full backup

In the TUI, select a `full` archive, press `R`, and choose:

- **Database only:** does not replace media.
- **Media only:** does not modify the database.
- **Both:** restores database and media together.

Before replacing data, the application verifies the archive, stages a temporary database when applicable, creates safety backups of the affected components, stops `netbox` and `netbox-rq`, restores the selected data, checks migrations with `manage.py migrate --check`, and restarts **only services that were originally running**. When replacing the database, the previous database is retained as `nbo_prev_*`. Failures trigger an attempted rollback; **PostgreSQL and the filesystem do not share a guaranteed atomic transaction**. Partial restores can leave database references without matching media, or vice versa.

**Redis:** restarting `netbox` and `netbox-rq` does not necessarily clear Redis caches. netbox-ops **deliberately preserves Redis and RQ queues**, because the VM's Redis databases used for caching and tasks have not been validated. Review cache consistency after a database restore, before reopening the application. There is no automatic `FLUSHALL` or `FLUSHDB` that might remove queued jobs or unrelated data. Cache validation must be part of a restore rehearsal; the production recovery process is not yet fully automated.

### NetBox versions recorded in older backups

Up to v0.3.1, the `netbox_version` manifest field was obtained from `manage.py --version`, which reports **Django's version** and cannot establish compatibility with a particular NetBox database. New manifests in v0.3.3 include `netbox_version_source: release.yaml`. Older backups remain listed and can be verified, but their NetBox application version is treated as **unknown**. In the TUI, restoring one requires typing `ACCEPT UNKNOWN VERSION` followed by the specific restore confirmation. The CLI requires `--accept-unknown-version` after external validation. This **does not prove compatibility**: confirm the source version and test in a disposable VM. Existing backups are never rewritten automatically.

### Compatibility with v0.2.0

- Older `db` backups remain accessible and are subject to manifest/version checks.
- Older `.files.tar.gz` archives and `full` archives containing configuration files are listed as **legacy**, but **cannot be restored** by this version to avoid accidentally restoring configuration files or credentials. They can be **verified read-only** (archive structure and file SHA-256 hashes) and explicitly deleted, but not restored. Keep copies before choosing to delete them.
- The application does not upgrade the NetBox schema during restore. Mismatched versions are rejected by default; the CLI offers `--accept-unknown-version` and `--allow-version-mismatch` only for carefully controlled tests.

## Limitations in v0.4.0

- CPU and RAM metrics are **instantaneous**, with no history or graphs. The first CPU sample may display `—` until a second reading (approximately three seconds later).
- NetBox's version is read from the `version` field in `/opt/netbox/netbox/release.yaml` (traditional Git/release installations), **not** from `manage.py --version`, which reports Django. Nginx uses `nginx -v`, Redis uses `redis-server --version`, and PostgreSQL runs `SHOW server_version` against the local instance. Missing versions are shown as `—` without preventing service management.
- Upgrade features, including self-upgrade, are currently **menu/CLI placeholders only**. Automatic version-cache invalidation will be implemented with upgrade support.
- Database restore **does not yet selectively clear Redis**, since the VM's cache/RQ database assignments have not been confirmed. Do not perform a real production restore without a rehearsal and consistency checks.

## Tests

```bash
cd /opt/netbox-ops
/opt/netbox-ops/venv/bin/python -m unittest discover -s tests -v
bash -n install.sh upgrade.sh
```

Tests use temporary directories, mocked systemd/PostgreSQL, and simulated terminals; **they do not prove that a real restore works on RHEL/NetBox**. Test `services status`, `backup media`, `backup db`, and `backup full` in an approved maintenance window. Separately, rehearse full and partial restores on an isolated clone or snapshot of the VM.
