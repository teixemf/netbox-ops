#!/usr/bin/env bash
# Rebuild the netbox-ops venv in place, in the same spirit as NetBox upgrade.sh.
# Does NOT update NetBox or restart any services.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo 'Execute como root: sudo /opt/netbox-ops/upgrade.sh' >&2
    exit 1
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="${ROOT}/venv"
PYTHON="${PYTHON_BIN:-/usr/bin/python3.12}"

if [[ ! -x "${PYTHON}" ]]; then
    echo "Python 3.12 não encontrado em ${PYTHON}; ajuste PYTHON_BIN." >&2
    exit 1
fi
if [[ "$("${PYTHON}" -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')" != '3.12' ]]; then
    echo 'O interpretador configurado não é Python 3.12.' >&2
    exit 1
fi
if [[ "${VENV}" == '/' || -L "${VENV}" ]]; then
    echo 'Caminho de venv inválido.' >&2
    exit 1
fi

for f in requirements.txt local_requirements.txt; do
    [[ -f "${ROOT}/${f}" ]] || { echo "Ficheiro em falta: ${f}" >&2; exit 1; }
done

printf 'A reconstruir %s com %s\n' "${VENV}" "${PYTHON}"
rm -rf -- "${VENV}"
"${PYTHON}" -m venv "${VENV}"
"${VENV}/bin/python" -m pip --disable-pip-version-check --no-input install \
    -r "${ROOT}/requirements.txt" \
    -r "${ROOT}/local_requirements.txt"
"${VENV}/bin/python" -m compileall -q "${ROOT}/netbox_ops" "${ROOT}/netbox_ops.py"
"${VENV}/bin/python" "${ROOT}/netbox_ops.py" --version
printf 'Venv reconstruído com sucesso.\n'
