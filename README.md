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
- Python **3.12** with `venv` support
- systemd and PostgreSQL client tools
- A terminal with Python `curses` support

The application uses a separate Python virtual environment and does not modify NetBox's Python environment.

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
