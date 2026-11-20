#!/usr/bin/env bash
# Initial local installation following a privileged Git clone to /opt/netbox-ops.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
    echo 'Execute como root: sudo /opt/netbox-ops/install.sh' >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${ROOT}" != '/opt/netbox-ops' ]]; then
    echo 'Instale o projecto em /opt/netbox-ops antes de executar install.sh.' >&2
    exit 1
fi

# Do not change permissions on a non-root-controlled source tree.
if [[ "$(stat -c %u -- "${ROOT}")" -ne 0 ]]; then
    echo 'O directório /opt/netbox-ops deve pertencer a root.' >&2
    exit 1
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
        echo "Erro: ${LINK} já aponta para outro destino." >&2
        exit 1
    fi
elif [[ -e "${LINK}" ]]; then
    echo "Erro: ${LINK} já existe e não será substituído." >&2
    exit 1
else
    ln -s -- "${TARGET}" "${LINK}"
fi

printf '\nInstalação concluída. Execute: sudo netbox-ops\n'
