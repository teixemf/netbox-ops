#!/usr/bin/env bash
# Run service and database backup smoke tests inside a disposable NetBox CI VM.
set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Never run these root-level operations on an ordinary host.
[[ "$(id -u)" == 0 ]] || fail 'Root is required'
[[ -f /run/netbox-ops-ci-e2e ]] || fail 'Disposable CI VM marker is missing'
[[ "$(cat /run/netbox-ops-ci-e2e)" == "netbox-ci-e2e" ]] || fail 'Invalid CI VM marker'
[[ "$(systemd-detect-virt --vm)" == "qemu" || "$(systemd-detect-virt --vm)" == "kvm" ]] ||
  fail 'Expected a QEMU/KVM virtual machine'
[[ -x /usr/local/sbin/netbox-ops ]] || fail 'netbox-ops is not installed'

cli=(/usr/local/sbin/netbox-ops --lang en)
http_url='http://127.0.0.1/login/'
backup_dir='/var/lib/netbox-ops/backups'

wait_for_http() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --output /dev/null "$http_url" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

echo 'Checking initial NetBox services and HTTP'
"${cli[@]}" services status
systemctl is-active --quiet netbox.service netbox-rq.service nginx.service \
  redis-server.service postgresql@16-main.service
wait_for_http || fail 'NetBox HTTP is unavailable before restart'
echo 'PASS: initial service and HTTP checks'

echo 'Restarting the real NetBox RQ service through netbox-ops'
"${cli[@]}" services restart --service netbox-rq --yes
for attempt in $(seq 1 30); do
  if systemctl is-active --quiet netbox-rq.service; then break; fi
  sleep 2
done
systemctl is-active --quiet netbox-rq.service || fail 'NetBox RQ did not recover'
wait_for_http || fail 'NetBox HTTP is unavailable after restart'
"${cli[@]}" services status --service netbox-rq
echo 'PASS: service restart and recovery'

echo 'Creating a real NetBox PostgreSQL backup through netbox-ops'
[[ -d "$backup_dir" ]] || fail 'Backup directory is missing'
[[ -z "$("${cli[@]}" backup list --kind db)" ]] ||
  fail 'Expected a fresh, empty disposable backup directory'
"${cli[@]}" backup db
mapfile -t backups < <("${cli[@]}" backup list --kind db)
[[ "${#backups[@]}" -eq 1 ]] || fail 'Expected exactly one database backup'
backup_file="${backups[0]}"
[[ "$backup_file" == "$backup_dir/"* ]] || fail 'Backup escaped the expected directory'
[[ -f "$backup_file" && ! -L "$backup_file" ]] || fail 'Backup is not a regular file'
[[ "$(stat -c %a "$backup_file")" == 600 ]] || fail 'Unexpected backup permissions'
[[ "$(stat -c %a "$backup_file.json")" == 600 ]] || fail 'Unexpected metadata permissions'
[[ "$(stat -c %a "$backup_dir")" == 700 ]] || fail 'Unexpected backup directory permissions'

"${cli[@]}" backup verify --kind db --file "$backup_file"
python3 - "$backup_file" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

dump = Path(sys.argv[1])
metadata = json.loads(Path(str(dump) + '.json').read_text())
assert metadata['kind'] == 'db', 'Unexpected backup kind'
assert metadata['database'] == 'netbox', 'Unexpected database'
assert metadata['sha256'] == hashlib.sha256(dump.read_bytes()).hexdigest(), 'Checksum mismatch'
with dump.open('rb') as stream:
    assert stream.read(5) == b'PGDMP', 'Not a PostgreSQL custom-format dump'
PY
pg_restore --list "$backup_file" | grep -q 'django_migrations' ||
  fail 'NetBox migration table is missing from the backup'
systemctl is-active --quiet netbox.service netbox-rq.service ||
  fail 'NetBox services stopped during the backup'
wait_for_http || fail 'NetBox HTTP is unavailable after backup'
echo 'PASS: real NetBox database backup, verification and contents'
