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

**In development:** NetBox and plugin upgrade/update operations are not yet implemented.

## Requirements

- RHEL-family or Debian-family Linux with a local NetBox installation
- NetBox **4.0 or newer** (NetBox 3.x and earlier are unsupported; future versions require compatibility review)
- Python **3.12 or newer** (Python 3.x), with working `venv` support
- systemd and PostgreSQL client tools
- A terminal with Python `curses` support

The application uses a separate Python virtual environment and does not modify NetBox's Python environment.

Install Python from your distribution's official repositories when a compatible version and any required matching `venv` package are available. On older releases, including Ubuntu 22.04 (which ships Python 3.10 by default), the official repositories may not provide Python 3.12 or newer. In that case, consider upgrading the operating system or installing a separate Python from a trusted source. Do not replace the system's default Python.

The installer detects compatible versioned Python executables and `python3` on `PATH`, plus conventional locations in `/usr/bin`, `/usr/local/bin`, and selected `/opt` installation prefixes (without recursively scanning `/opt`). To choose an interpreter explicitly, set `PYTHON_BIN`:

```bash
sudo PYTHON_BIN=/path/to/python3.13 /opt/netbox-ops/install.sh
```

Before rebuilding an existing environment, `upgrade.sh` checks that the chosen interpreter can create and run a temporary virtual environment without pip. When no compatible Python interpreter is found and `PYTHON_BIN` is not set, `install.sh` can offer to install an available Python 3.12 or newer release and any required virtual-environment support from the repositories already configured on the system. It prompts before installing anything; a declined or non-interactive run makes no package changes. When the packages are unavailable, it stops with guidance (Ubuntu 22.04 does not provide Python 3.12 in its standard repositories). The installer never adds external repositories or changes the system default Python. `upgrade.sh` does not offer to install system packages. netbox-ops has no external Python dependencies and does not require pip.

## Getting started

Download the `netbox-ops-v0.5.0.tar.gz` asset from the GitHub Release, then install it on the NetBox server. The archive contains a top-level `netbox-ops/` directory:

```bash
sudo tar -xzf netbox-ops-v0.5.0.tar.gz -C /opt
sudo /opt/netbox-ops/install.sh
sudo netbox-ops
```

Some useful CLI commands:

```bash
sudo netbox-ops --version
sudo netbox-ops platform
sudo netbox-ops services status
sudo netbox-ops backup list
sudo netbox-ops backup combined
```

New `combined` backups contain the PostgreSQL database and NetBox media files, not the entire NetBox installation. The CLI also supports separate `db` and `media` backups. Older `full` archives remain listed, verifiable, and restorable without conversion; new combined archives use the `.combined.tar.gz` extension and `kind=combined` metadata. Historical archives keep their original `.full.tar.gz` filename and `kind=full` metadata. Use `backup verify --kind full --file PATH` or `restore full --file PATH` for an older archive. Both `restore full` and `restore combined` support `--only db`, `--only media`, or `--only all`. Restores still require the existing safety checks and explicit confirmations.

Review `config.example.json` and the configuration stored under `/etc/netbox-ops` before performing operations. Backups are stored under `/var/lib/netbox-ops/backups` by default.

## Project status

**Current version: 0.5.0 — early development.**

CI includes real PostgreSQL backup and restore integration tests on Ubuntu 22.04 and Rocky Linux 9. Disposable NetBox VM smoke tests cover service restart and database backup verification, but **full end-to-end NetBox restore and recovery testing remains outstanding**. Do not use restore functionality on production systems until you have tested recovery in a disposable environment with appropriate snapshots or non-production data.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [CHANGELOG.md](CHANGELOG.md) for release history.
