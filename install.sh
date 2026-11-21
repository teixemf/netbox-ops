#!/usr/bin/env bash
# Initial local installation following a privileged Git clone to /opt/netbox-ops.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
    echo 'Run as root: sudo /opt/netbox-ops/install.sh' >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${ROOT}" != '/opt/netbox-ops' ]]; then
    echo 'Install the project in /opt/netbox-ops before running install.sh.' >&2
    exit 1
fi

# Do not change permissions on a non-root-controlled source tree.
if [[ "$(stat -c %u -- "${ROOT}")" -ne 0 ]]; then
    echo 'The /opt/netbox-ops directory must be owned by root.' >&2
    exit 1
fi
# Supported families: RHEL (including Rocky/Alma) and Debian (incl. Ubuntu).
if [[ ! -r /etc/os-release ]]; then
    echo 'Unable to identify the Linux distribution.' >&2
    exit 1
fi
. /etc/os-release
DISTRO_IDS=" ${ID:-} ${ID_LIKE:-} "
case "${DISTRO_IDS}" in
    *' debian '*|*' ubuntu '*) FAMILY=debian ;;
    *' rhel '*|*' centos '*|*' fedora '*) FAMILY=rhel ;;
    *) echo 'Unsupported distribution: RHEL or Debian (or a derivative) is required.' >&2; exit 1 ;;
esac
for command in systemctl runuser pg_dump pg_restore psql; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        # PGDG clients on RHEL may live outside PATH; the Python backend can
        # discover them, so don't demand bare PG tools at install time.
        if [[ "${command}" == pg_dump || "${command}" == pg_restore || "${command}" == psql ]]; then
            echo "Warning: ${command} is not in PATH; configure backup.pg_bin_directory if using PGDG." >&2
        else
            echo "Missing system dependency: ${command}." >&2
            exit 1
        fi
    fi
done
printf 'Detected distribution: %s (%s)\n' "${FAMILY}" "${PRETTY_NAME:-${ID}}"
"${ROOT}/upgrade.sh"
install -d -o root -g root -m 0700 /etc/netbox-ops /var/log/netbox-ops
# Persistent data is deliberately outside /opt/netbox-ops (Git-rebuildable code).
install -d -o root -g root -m 0700 /var/lib/netbox-ops /var/lib/netbox-ops/backups
if [[ ! -e /etc/netbox-ops/config.json ]]; then
    install -o root -g root -m 0600 "${ROOT}/config.example.json" /etc/netbox-ops/config.json
fi
install -d -o root -g root -m 0755 /usr/local/sbin
cat > /usr/local/sbin/netbox-ops <<'LAUNCHER'
#!/bin/sh
exec /opt/netbox-ops/venv/bin/python /opt/netbox-ops/netbox_ops.py "$@"
LAUNCHER
chown root:root /usr/local/sbin/netbox-ops
chmod 0755 /usr/local/sbin/netbox-ops

# On hardened RHEL installations sudo may use a restricted PATH that omits
# /usr/local/sbin. Keep the canonical launcher there and expose a safe link
# inside /usr/sbin. Never overwrite an unrelated file or symlink.
LINK=/usr/sbin/netbox-ops
TARGET=/usr/local/sbin/netbox-ops
if [[ -L "${LINK}" ]]; then
    if [[ "$(readlink -- "${LINK}")" != "${TARGET}" ]]; then
        echo "Error: ${LINK} already points to another target." >&2
        exit 1
    fi
elif [[ -e "${LINK}" ]]; then
    echo "Error: ${LINK} already exists and will not be replaced." >&2
    exit 1
else
    ln -s -- "${TARGET}" "${LINK}"
fi

printf '\nInstallation complete. Run: sudo netbox-ops\n'
