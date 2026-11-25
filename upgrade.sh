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

    # Look in PATH (including versioned executables) and conventional
    # installation directories. Never traverse /opt recursively.
    local -a candidates=()
    local directory path
    local -A seen=()
    local -a directories=()
    IFS=':' read -r -a directories <<< "${PATH:-}"
    directories+=(/usr/bin /usr/local/bin /opt/python/bin /opt/python3/bin /opt/local/bin)
    for directory in "${directories[@]}"; do
        [[ -n "${directory}" && -d "${directory}" ]] || continue
        for path in "${directory}"/python3 "${directory}"/python3.[0-9]*; do
            [[ -x "${path}" && ! -d "${path}" ]] || continue
            [[ -z "${seen["$path"]:-}" ]] || continue
            seen["$path"]=1
            candidates+=("$path")
        done
    done
    # Conventional version-specific installation prefixes under /opt.
    for path in /opt/python3.[0-9]*/bin/python3.[0-9]* /opt/python/3.[0-9]*/bin/python3.[0-9]*; do
        [[ -x "${path}" && ! -d "${path}" ]] || continue
        [[ -z "${seen["$path"]:-}" ]] || continue
        seen["$path"]=1
        candidates+=("$path")
    done
    for candidate in "${candidates[@]}"; do
        if python_is_supported "${candidate}"; then
            PYTHON="${candidate}"
            return 0
        fi
    done
    echo 'Error: Python 3.12 or newer was not found.' >&2
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
    if ! "${PYTHON}" -m venv --without-pip "${PRECHECK_VENV}" >/dev/null 2>&1; then
        echo 'Error: Python could not create a temporary virtual environment.' >&2
        echo 'Check venv support, directory permissions, and free disk space.' >&2
        echo 'Install the matching venv package from your OS repositories, if needed and available.' >&2
        return 1
    fi
    if ! "${PRECHECK_VENV}/bin/python" -c 'import sys' >/dev/null 2>&1; then
        echo 'Error: Python is not usable in the new virtual environment.' >&2
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

select_python || exit 1
trap cleanup_preflight EXIT
preflight_python || exit 1

printf 'Rebuilding %s using %s\n' "${VENV}" "${PYTHON}"
rm -rf -- "${VENV}"
"${PYTHON}" -m venv --without-pip "${VENV}"
"${VENV}/bin/python" -m compileall -q "${ROOT}/netbox_ops" "${ROOT}/netbox_ops.py"
"${VENV}/bin/python" "${ROOT}/netbox_ops.py" --version
if [[ "${ROOT}" == '/opt/netbox-ops' ]]; then
    # The initial installer also invokes this shared upgrade path.
    "${VENV}/bin/python" -I -B "${ROOT}/netbox_ops/logrotate_policy.py" || true
fi
printf 'Virtual environment rebuilt successfully.\n'
