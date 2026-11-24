#!/usr/bin/env bash
# Run only inside a fresh disposable Docker container, never on a real server.
set -euo pipefail
[[ -f /.dockerenv && "$EUID" == 0 && -d /release ]]
for path in /opt/netbox-ops /opt/netbox /etc/netbox-ops /var/lib/netbox-ops /var/log/netbox-ops /usr/local/sbin/netbox-ops /usr/sbin/netbox-ops /etc/logrotate.d/netbox-ops; do
    [[ ! -e "$path" && ! -L "$path" ]]
done
if [[ ! -x /usr/bin/python3 ]]; then
    ln -s /usr/local/bin/python3 /usr/bin/python3
fi
archive=(/release/netbox-ops-v*.tar.gz)
[[ "${#archive[@]}" == 1 && -f "${archive[0]}" ]]
tar --no-same-owner -xzf "${archive[0]}" -C /opt
mkdir -p /opt/netbox /etc/logrotate.d /usr/local/sbin /run/lock
install -d -m 0700 /etc/netbox-ops /var/log/netbox-ops /var/lib/netbox-ops/backups
printf 'netbox remains\n' > /opt/netbox/marker
printf '{}\n' > /etc/netbox-ops/config.json
printf 'audit\n' > /var/log/netbox-ops/operations.log
chmod 0600 /etc/netbox-ops/config.json /var/log/netbox-ops/operations.log
printf 'backup\n' > /var/lib/netbox-ops/backups/test.dump
cat > /usr/local/sbin/netbox-ops <<'LAUNCHER'
#!/bin/sh
exec /opt/netbox-ops/venv/bin/python /opt/netbox-ops/netbox_ops.py "$@"
LAUNCHER
chmod 0755 /usr/local/sbin/netbox-ops
ln -s /usr/local/sbin/netbox-ops /usr/sbin/netbox-ops
cd /opt/netbox-ops
python3 -c 'from netbox_ops.logrotate_policy import policy_text; from pathlib import Path; Path("/etc/logrotate.d/netbox-ops").write_text(policy_text())'
# Keep a trusted copy for the repeat run after the installed script disappears.
mkdir -m 0700 /tmp/uninstaller /tmp/uninstaller/netbox_ops
install -m 0755 uninstall.sh /tmp/uninstaller/uninstall.sh
install -m 0644 netbox_ops/uninstall.py /tmp/uninstaller/netbox_ops/uninstall.py
cd /
if /opt/netbox-ops/uninstall.sh </dev/null; then
    echo 'Noninteractive uninstall unexpectedly succeeded' >&2
    exit 1
fi
printf 'UNINSTALL\n\n\n' | script -qec /opt/netbox-ops/uninstall.sh /dev/null
[[ ! -e /opt/netbox-ops && ! -e /usr/local/sbin/netbox-ops && ! -L /usr/sbin/netbox-ops ]]
[[ ! -e /etc/logrotate.d/netbox-ops ]]
grep -Fxq '{}' /etc/netbox-ops/config.json
grep -Fxq audit /var/log/netbox-ops/operations.log
grep -Fxq backup /var/lib/netbox-ops/backups/test.dump
grep -Fxq 'netbox remains' /opt/netbox/marker
printf 'UNINSTALL\nREMOVE CONFIG\nREMOVE LOGS\nCONFIRM\n' | script -qec /tmp/uninstaller/uninstall.sh /dev/null
[[ ! -e /etc/netbox-ops/config.json && ! -e /var/log/netbox-ops/operations.log ]]
grep -Fxq backup /var/lib/netbox-ops/backups/test.dump
grep -Fxq 'netbox remains' /opt/netbox/marker
echo 'PASS: interactive uninstall and repeat preserve NetBox and backups'
