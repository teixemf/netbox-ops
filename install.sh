#!/usr/bin/env bash
# Initial local installation following a privileged Git clone to /opt/netbox-ops.
set -euo pipefail


# The installer only offers packages already available in configured OS repositories.
# It never adds repositories or changes the system's default Python interpreter.
apt_has_candidate() {
    local candidate
    candidate="$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/^[[:space:]]*Candidate:/{print $2; exit}')" || return 1
    [[ -n "${candidate}" && "${candidate}" != '(none)' ]]
}

confirm_python_install() {
    local response=''
    if [[ ! -t 0 ]]; then
        echo 'Python installation requires an interactive terminal; no changes were made.' >&2
        return 1
    fi
    read -r -p 'Python 3.12 is available. Install it now? [y/N] ' response || return 1
    case "${response,,}" in
        y|yes) return 0 ;;
        *) echo 'Python installation declined; no packages were installed.' >&2; return 1 ;;
    esac
}

install_python_if_requested() {
    local family="$1"
    local -a install_command=()
    case "${family}" in
        debian)
            if ! command -v apt-cache >/dev/null 2>&1 ||
               ! command -v apt-get >/dev/null 2>&1 ||
               ! apt_has_candidate python3.12 ||
               ! apt_has_candidate python3.12-venv; then
                echo 'Python 3.12 and python3.12-venv are not both available in the configured APT repositories.' >&2
                return 1
            fi
            install_command=(apt-get --no-remove install --yes --no-install-recommends python3.12 python3.12-venv)
            ;;
        rhel)
            if ! command -v dnf >/dev/null 2>&1 ||
               ! dnf -q repoquery --available python3.12 >/dev/null 2>&1 ||
               ! dnf -q repoquery --available python3.12-pip >/dev/null 2>&1; then
                echo 'Python 3.12 and python3.12-pip are not both available in the configured DNF repositories.' >&2
                return 1
            fi
            install_command=(dnf -y --setopt=install_weak_deps=False install python3.12 python3.12-pip)
            ;;
        *)
            echo 'Cannot offer automatic Python installation on this distribution.' >&2
            return 1
            ;;
    esac

    confirm_python_install || return 1
    printf 'Installing Python 3.12 from configured repositories...\n'
    if ! "${install_command[@]}"; then
        echo 'Python package installation failed. Check the package-manager output above.' >&2
        return 1
    fi
}

# Allow isolated tests to source the helpers without running a privileged installer.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

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
# Offer an opt-in Python 3.12 installation only when no compatible interpreter
# is present and the user has not explicitly selected a Python via PYTHON_BIN.
if [[ -z "${PYTHON_BIN:-}" ]]; then
    # Sourcing upgrade.sh only loads its Python-detection helpers.
    . "${ROOT}/upgrade.sh"
    if ! (select_python >/dev/null 2>&1); then
        if ! install_python_if_requested "${FAMILY}"; then
            echo 'No compatible Python installed. See README.md for supported alternatives.' >&2
            exit 1
        fi
    fi
fi
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
