#!/usr/bin/env bash
# Rebuild the netbox-ops venv in place, in the same spirit as NetBox upgrade.sh.
# Does NOT update NetBox or restart any services.
set -euo pipefail

python_is_supported() {
    local version
    version="$("$1" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))' 2>/dev/null)" || return 1
    [[ "${version}" =~ ^3\.([0-9]+)$ ]] || return 1
    (( 10#${BASH_REMATCH[1]} >= 12 ))
}

python_installation_guidance() {
    local distro_ids=''
    if [[ -r /etc/os-release ]]; then
        # Used for guidance only; no packages are installed by this script.
        distro_ids=" $(. /etc/os-release; printf '%s %s' "${ID:-}" "${ID_LIKE:-}") "
    fi
    case "${distro_ids}" in
        *' debian '*|*' ubuntu '*)
            echo 'Check your distribution repositories for Python 3.12+ and its matching venv package.' >&2
            echo 'If available, install both with apt (for example: python3.12 and python3.12-venv).' >&2
            ;;
        *' rhel '*|*' centos '*|*' fedora '*)
            echo 'Check your distribution repositories for Python 3.12+ and its matching pip/venv support.' >&2
            echo 'If available, install the packages with dnf.' >&2
            ;;
        *)
            echo 'Check your distribution official repositories for Python 3.12+ with venv and pip support.' >&2
            ;;
    esac
    echo 'If your OS release does not provide it, consider upgrading the OS or installing a separate Python from a trusted source.' >&2
    echo 'Set PYTHON_BIN to the compatible interpreter path; do not replace the system Python.' >&2
}

select_python() {
    local candidate
    PYTHON=''
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        candidate="$(command -v -- "${PYTHON_BIN}" 2>/dev/null || true)"
        if [[ -z "${candidate}" || ! -x "${candidate}" ]]; then
            printf 'Error: PYTHON_BIN interpreter not found or not executable: %s\n' "${PYTHON_BIN}" >&2
            return 1
        fi
        if ! python_is_supported "${candidate}"; then
            printf 'Error: PYTHON_BIN must point to Python 3.12 or newer (Python 3.x): %s\n' "${candidate}" >&2
            return 1
        fi
        PYTHON="${candidate}"
        return 0
    fi

    # Prefer versioned interpreters from standard system locations. This also
    # handles installations whose /usr/local/bin is absent from root's PATH.
    for candidate in /usr/bin/python3.[0-9]* /usr/local/bin/python3.[0-9]*; do
        if [[ -x "${candidate}" ]] && python_is_supported "${candidate}"; then
            PYTHON="${candidate}"
            return 0
        fi
    done
    # Other installations can provide a compatible default Python in PATH.
    candidate="$(command -v python3 2>/dev/null || true)"
    if [[ -n "${candidate}" ]] && python_is_supported "${candidate}"; then
        PYTHON="${candidate}"
        return 0
    fi
    echo 'Error: Python 3.12 or newer was not found.' >&2
    python_installation_guidance
    return 1
}

PRECHECK_VENV=''
cleanup_preflight() {
    if [[ -n "${PRECHECK_VENV}" ]]; then
        rm -rf -- "${PRECHECK_VENV}"
        PRECHECK_VENV=''
    fi
}

preflight_python() {
    PRECHECK_VENV="$(mktemp -d "${ROOT}/.venv-check.XXXXXXXX")" || {
        echo 'Error: Could not create a temporary directory for the Python check.' >&2
        return 1
    }
    if ! "${PYTHON}" -m venv "${PRECHECK_VENV}" >/dev/null 2>&1; then
        echo 'Error: Python could not create a temporary virtual environment.' >&2
        echo 'Check venv/ensurepip support, directory permissions, and free disk space.' >&2
        echo 'Install the matching venv package from your OS repositories, if needed and available.' >&2
        return 1
    fi
    if ! "${PRECHECK_VENV}/bin/python" -m pip --version >/dev/null 2>&1; then
        echo 'Error: pip is unavailable in the new virtual environment.' >&2
        echo 'Install the matching venv/pip support from your OS repositories, if available.' >&2
        return 1
    fi
    cleanup_preflight
}

# When sourced, expose the helpers for isolated tests without changing a venv.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo 'Run as root: sudo /opt/netbox-ops/upgrade.sh' >&2
    exit 1
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="${ROOT}/venv"
if [[ "${VENV}" == '/' || -L "${VENV}" ]]; then
    echo 'Error: Invalid virtual environment path.' >&2
    exit 1
fi

for f in requirements.txt local_requirements.txt; do
    [[ -f "${ROOT}/${f}" ]] || { printf 'Error: Missing file: %s\n' "${f}" >&2; exit 1; }
done

select_python || exit 1
trap cleanup_preflight EXIT
preflight_python || exit 1

printf 'Rebuilding %s using %s\n' "${VENV}" "${PYTHON}"
rm -rf -- "${VENV}"
"${PYTHON}" -m venv "${VENV}"
"${VENV}/bin/python" -m pip --disable-pip-version-check --no-input install \
    -r "${ROOT}/requirements.txt" \
    -r "${ROOT}/local_requirements.txt"
"${VENV}/bin/python" -m compileall -q "${ROOT}/netbox_ops" "${ROOT}/netbox_ops.py"
"${VENV}/bin/python" "${ROOT}/netbox_ops.py" --version
printf 'Virtual environment rebuilt successfully.\n'
