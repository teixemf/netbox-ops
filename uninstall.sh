#!/usr/bin/env bash
# Remove only netbox-ops; the helper deliberately does not need its virtualenv.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
    echo 'Run as root: sudo /opt/netbox-ops/uninstall.sh' >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
HELPER="${ROOT}/netbox_ops/uninstall.py"
for path in "${ROOT}" "${ROOT}/netbox_ops" "${HELPER}"; do
    if [[ -L "${path}" || ! -e "${path}" || "$(stat -c %u -- "${path}")" != 0 ]] ||
       (( 8#$(stat -c %a -- "${path}") & 0022 )); then
        echo 'Unsafe or missing uninstaller files. Use a trusted root-owned release.' >&2
        exit 1
    fi
done
if [[ ! -x /usr/bin/python3 ]]; then
    echo 'The system Python 3 interpreter is required; restore it before uninstalling.' >&2
    exit 1
fi
exec /usr/bin/python3 -I -B "${HELPER}" "$@"
