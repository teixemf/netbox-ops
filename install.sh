#!/usr/bin/env bash
# Initial local installation following a privileged Git clone to /opt/netbox-ops.
set -euo pipefail


python_not_found_message() {
    cat >&2 <<'MESSAGE'
No compatible Python version (3.12 or newer) was found.
A compatible version was also unavailable in the currently configured software repositories.
To continue installing netbox-ops, install Python 3.12 or newer manually from a safe and trusted source.

If a compatible Python is already installed but was not detected automatically,
specify its interpreter path using PYTHON_BIN.

Example:
  sudo PYTHON_BIN=/path/to/python3.12 /opt/netbox-ops/install.sh
MESSAGE
}

# The installer only offers packages already available in configured OS repositories.
# It never adds repositories or changes the system's default Python interpreter.
apt_has_candidate() {
    local candidate
    candidate="$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/^[[:space:]]*Candidate:/{print $2; exit}')" || return 1
    [[ -n "${candidate}" && "${candidate}" != '(none)' ]]
}

dnf_has_candidate() {
    local matches
    matches="$(dnf -q repoquery --available "$1" 2>/dev/null)" || return 1
    [[ -n "${matches//[[:space:]]/}" ]]
}

confirm_python_install() {
    local response=''
    if [[ ! -t 0 ]]; then
        echo 'Python installation requires an interactive terminal; no changes were made.' >&2
        return 2
    fi
    read -r -p "Python ${PYTHON_PACKAGE_VERSION} is available. Install it now? [y/N] " response || return 1
    case "${response,,}" in
        y|yes) return 0 ;;
        *) echo 'Python installation declined; no packages were installed.' >&2; return 2 ;;
    esac
}

install_python_if_requested() {
    local family="$1"
    local minor interpreter support
    local -a install_command=()
    PYTHON_PACKAGE_VERSION=''

    # Prefer the oldest supported version with all required packages available.
    # A bounded set avoids scanning repositories or installing untested majors.
    for minor in {12..20}; do
        interpreter="python3.${minor}"
        case "${family}" in
            debian)
                support="${interpreter}-venv"
                if command -v apt-cache >/dev/null 2>&1 &&
                   command -v apt-get >/dev/null 2>&1 &&
                   apt_has_candidate "${interpreter}" &&
                   apt_has_candidate "${support}"; then
                    install_command=(apt-get --no-remove install --yes --no-install-recommends "${interpreter}" "${support}")
                else
                    continue
                fi
                ;;
            rhel)
                if command -v dnf >/dev/null 2>&1 &&
                   dnf_has_candidate "${interpreter}"; then
                    install_command=(dnf -y --setopt=install_weak_deps=False install "${interpreter}")
                else
                    continue
                fi
                ;;
            *)
                echo 'Cannot offer automatic Python installation on this distribution.' >&2
                return 1
                ;;
        esac
        PYTHON_PACKAGE_VERSION="3.${minor}"
        break
    done

    if [[ -z "${PYTHON_PACKAGE_VERSION}" ]]; then
        echo 'No compatible Python packages with virtual-environment support are available in the configured repositories.' >&2
        return 1
    fi

    confirm_python_install || return $?
    printf 'Installing Python %s from configured repositories...\n' "${PYTHON_PACKAGE_VERSION}"
    if ! "${install_command[@]}"; then
        echo 'Python package installation failed. Check the package-manager output above.' >&2
        return 3
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
        if install_python_if_requested "${FAMILY}"; then
            : # Re-run detection in upgrade.sh after package installation.
        else
            status=$?
            if [[ "${status}" -eq 1 ]]; then
                python_not_found_message
            fi
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
