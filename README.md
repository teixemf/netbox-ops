# netbox-ops

**netbox-ops** is a Python CLI and terminal UI (TUI) for managing and maintaining local [NetBox](https://github.com/netbox-community/netbox) installations.

It brings common administration tasks into one terminal-based tool, designed for Linux servers running NetBox with systemd, PostgreSQL, and Redis.

## Features

- View service status, resource usage, and logs; manage systemd services.
- Create, list, verify, and delete PostgreSQL database and NetBox media backups.
- Restore database, media, or both, with explicit confirmations and safety checks.
- Use an interactive terminal interface or CLI commands.
- Switch between English and European Portuguese.
- Detect common RHEL- and Debian-family service configurations.

**In development:** NetBox and plugin upgrade/update operations are not yet implemented.

## Requirements

- RHEL-family or Debian-family Linux with a local NetBox installation
- Python **3.12 or newer** (Python 3.x), with working `venv` and `pip` support
- systemd and PostgreSQL client tools
- A terminal with Python `curses` support

The application uses a separate Python virtual environment and does not modify NetBox's Python environment.

Install Python from your distribution's official repositories when a compatible version and matching `venv` package are available. On older releases, including Ubuntu 22.04 (which ships Python 3.10 by default), the official repositories may not provide Python 3.12 or newer. In that case, consider upgrading the operating system or installing a separate Python from a trusted source. Do not replace the system's default Python.

The installer looks for compatible Python versions in `/usr/bin` and `/usr/local/bin`, then checks `python3` on `PATH`. To choose an interpreter explicitly, set `PYTHON_BIN`:

```bash
sudo PYTHON_BIN=/path/to/python3.13 /opt/netbox-ops/install.sh
```

Before rebuilding an existing environment, `upgrade.sh` checks that the chosen interpreter can create a temporary virtual environment with pip. When no compatible Python interpreter is found and `PYTHON_BIN` is not set, `install.sh` can offer to install Python 3.12 and its virtual-environment support from the repositories already configured on the system. It prompts before installing anything; a declined or non-interactive run makes no package changes. When the packages are unavailable, it stops with guidance (Ubuntu 22.04 does not provide Python 3.12 in its standard repositories). The installer never adds external repositories or changes the system default Python. `upgrade.sh` does not offer to install system packages.

## Getting started

Install from a prepared release archive on the NetBox server:

```bash
sudo unzip -o netbox-ops-v0.4.0.zip -d /opt
sudo /opt/netbox-ops/install.sh
sudo netbox-ops
```

Some useful CLI commands:

```bash
sudo netbox-ops --version
sudo netbox-ops platform
sudo netbox-ops services status
sudo netbox-ops backup list
```

Review `config.example.json` and the configuration stored under `/etc/netbox-ops` before performing operations. Backups are stored under `/var/lib/netbox-ops/backups` by default.

## Project status

**Current version: 0.4.0 — early development.**

Automated tests cover isolated scenarios, but **real restore procedures have not yet been validated on RHEL and Debian systems**. Do not use restore functionality on production systems until you have tested recovery in a disposable environment with appropriate snapshots or non-production data.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [CHANGELOG.md](CHANGELOG.md) for release history.
