# netbox-ops

**netbox-ops** is a Python CLI and terminal UI (TUI) for managing and maintaining local [NetBox](https://github.com/netbox-community/netbox) installations.

It brings common administration tasks into one terminal-based tool, designed for Linux servers running NetBox with systemd, PostgreSQL, and Redis.

## Features

- View service status, resource usage, and logs; manage systemd services.
- Create, list, verify, and delete PostgreSQL database and NetBox media backups.
- Restore database, media, or both, with explicit confirmations and safety checks.
- Use an interactive terminal interface or CLI commands.
- Automatically select English or European Portuguese from the system locale (English fallback); override with `--lang en` or `--lang pt`.
- Detect common RHEL- and Debian-family service configurations.
- Configure weekly audit log rotation (four archives) during installation and upgrades when the OS `logrotate` package is available. If it is missing, install it and rerun `upgrade.sh`; custom policies are preserved.

- Review and execute guarded NetBox, plugin-only and combined upgrade plans, including dependency preflight, exact plugin pins and verified recovery backups.
- Browse official netbox-ops releases, review compatibility and install verified self-upgrade steps with transactional recovery.

See [NetBox upgrades](docs/NETBOX_UPGRADE.md) and [self-upgrade and recovery](docs/SELF_UPGRADE_RECOVERY.md) for requirements and limitations. These guides are available in the GitHub repository.

## Requirements

- RHEL-family or Debian-family Linux with a local NetBox installation
- NetBox **4.0 or newer** (NetBox 3.x and earlier are unsupported; future versions require compatibility review)
- Python **3.12 or newer** (Python 3.x), with working `venv` support
- systemd and PostgreSQL client tools
- A terminal with Python `curses` support

netbox-ops itself uses a separate Python virtual environment. Service and backup operations do not install packages into NetBox's environment; an explicitly approved NetBox or plugin upgrade does modify that environment.

Install Python from your distribution's official repositories when a compatible version and any required matching `venv` package are available. On older releases, including Ubuntu 22.04 (which ships Python 3.10 by default), the official repositories may not provide Python 3.12 or newer. In that case, consider upgrading the operating system or installing a separate Python from a trusted source. Do not replace the system's default Python.

The installer detects compatible versioned Python executables and `python3` on `PATH`, plus conventional locations in `/usr/bin`, `/usr/local/bin`, and selected `/opt` installation prefixes (without recursively scanning `/opt`). To choose an interpreter explicitly, set `PYTHON_BIN`:

```bash
sudo PYTHON_BIN=/path/to/python3.13 /opt/netbox-ops/install.sh
```

Before rebuilding an existing environment, `upgrade.sh` checks that the chosen interpreter can create and run a temporary virtual environment without pip. When no compatible Python interpreter is found and `PYTHON_BIN` is not set, `install.sh` can offer to install an available Python 3.12 or newer release and any required virtual-environment support from the repositories already configured on the system. It prompts before installing anything; a declined or non-interactive run makes no package changes. When the packages are unavailable, it stops with guidance (Ubuntu 22.04 does not provide Python 3.12 in its standard repositories). The installer never adds external repositories or changes the system default Python. `upgrade.sh` does not offer to install system packages. netbox-ops has no external Python dependencies and does not require pip.

## Getting started

Download `netbox-ops-v0.6.1.tar.gz` and its matching `.sha256` asset from the official GitHub Release. Verify the checksum and inspect the archive before installing it on the NetBox server. Stop if verification fails. The archive contains a top-level `netbox-ops/` directory:

```bash
sha256sum -c netbox-ops-v0.6.1.tar.gz.sha256
tar -tzf netbox-ops-v0.6.1.tar.gz
sudo tar -xzf netbox-ops-v0.6.1.tar.gz -C /opt
sudo /opt/netbox-ops/install.sh
sudo netbox-ops
```

These commands are for a fresh installation. Do not extract over an existing installation. Upgrading from v0.5.0 requires [manual replacement](docs/SELF_UPGRADE_RECOVERY.md#initial-upgrade-from-v050); the transactional self-upgrade mechanism is available from v0.6.0 onwards for supported archive installations.

Some useful CLI commands:

```bash
sudo netbox-ops --version
sudo netbox-ops platform
sudo netbox-ops services status
sudo netbox-ops backup list
sudo netbox-ops backup combined
```

New `combined` backups contain the PostgreSQL database and NetBox media files, not the entire NetBox installation. The CLI also supports separate `db` and `media` backups. Older `full` archives remain listed, verifiable, and restorable without conversion; new combined archives use the `.combined.tar.gz` extension and `kind=combined` metadata. Historical archives keep their original `.full.tar.gz` filename and `kind=full` metadata. Use `backup verify --kind full --file PATH` or `restore full --file PATH` for an older archive. Both `restore full` and `restore combined` support `--only db`, `--only media`, or `--only all`. Restores still require the existing safety checks and explicit confirmations.

In the TUI main menu, select `3` (Upgrade NetBox) to discover installed inventory and available targets, then open unified NetBox and plugin upgrade management. Use the arrow keys and Enter to choose exact target versions; Tab opens the operation bar. Suggestions and preliminary compatibility are read-only. Validate the full plan before Review and execute becomes available. Select `4` for netbox-ops self-upgrade. The CLI commands and text-mode menu are unchanged.

In the TUI Backups screen, use the arrow keys to preview a backup, Space to mark it, and Tab to focus the operations. Press `I` to read the full filename and details, or `B` for storage and inventory counts on a narrow terminal. Neither changes marks or executes an operation.

The upgrade views keep installed/target versions, plan status and available actions visible. Refreshing or editing the selection requires a new review; blocked plans cannot be executed. See [TUI conventions and upgrade layouts](docs/TUI_UX.md).

TUI confirmations require the exact displayed token. An incorrect token clears the input and shows an inline error; type again to retry, or press Esc to cancel without running the operation.

Review `config.example.json` and the configuration stored under `/etc/netbox-ops` before performing operations. Backups are stored under `/var/lib/netbox-ops/backups` by default.

## Uninstall

Run `sudo /opt/netbox-ops/uninstall.sh` from an interactive terminal. Exit other netbox-ops sessions first. The script previews the fixed application paths and requires typing `UNINSTALL`. It preserves configuration, logs and all backups by default. Configuration and known audit logs can be removed only with separate typed confirmations. Customized launchers or logrotate policies are left untouched; unknown installation files, unsafe paths and pending upgrades block removal. Review and move any locally stored data out of the application directory before uninstalling. The script never removes NetBox, system packages or unrelated services. To retry a partial removal, run the uninstaller from a trusted, root-owned extracted release.

## Project status

**Current version: 0.6.1 — early development.**

CI includes real PostgreSQL backup and restore integration tests on Ubuntu 22.04 and Rocky Linux 9, plus disposable Ubuntu 24.04 VM suites for NetBox services, database and media restores, NetBox and plugin upgrades, and netbox-ops self-upgrade. Restore coverage includes combined and selective restores, corrupt archive rejection and rollback after an injected failure. Self-upgrade coverage uses unpublished candidate archives and controlled failure scenarios; testing two published releases is tracked in #132. PostgreSQL integration on Rocky Linux does not establish full upgrade support on that distribution.

NetBox upgrades require a supported Git installation at `/opt/netbox` and manual recovery if an upgrade fails after migrations may have run; automatic NetBox upgrade rollback is not implemented (#63). The self-upgrade mechanism requires a supported archive layout and working Linux `pidfd_open`. Test backup, restore and recovery in a disposable environment with appropriate snapshots or non-production data before using these operations in production.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [CHANGELOG.md](CHANGELOG.md) for release history.
