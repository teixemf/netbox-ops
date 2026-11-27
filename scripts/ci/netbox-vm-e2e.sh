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

# Operate on a real NetBox model, not a synthetic PostgreSQL-only table.
# The outer script requires an explicitly marked disposable QEMU/KVM guest.
netbox_tag() {
  local action=$1
  (cd /opt/netbox/netbox &&
    runuser -u netbox -- env DJANGO_SETTINGS_MODULE=netbox.settings \
      /opt/netbox/venv/bin/python - "$action" <<'PY'
import sys
import django

django.setup()
from extras.models import Tag

slug = 'netbox-ops-ci-db-restore'
original = 'NetBox Ops CI original'
modified = 'NetBox Ops CI modified'
action = sys.argv[1]

if action == 'seed':
    assert not Tag.objects.filter(slug=slug).exists(), 'Test tag already exists'
    Tag.objects.create(name=original, slug=slug, color='9e9e9e')
elif action == 'mutate':
    assert Tag.objects.filter(slug=slug).update(name=modified) == 1, 'Tag mutation failed'
    assert Tag.objects.get(slug=slug).name == modified, 'Tag was not modified'
elif action == 'verify':
    assert Tag.objects.get(slug=slug).name == original, 'Original tag was not restored'
    assert not Tag.objects.filter(name=modified).exists(), 'Modified tag survived restore'
elif action == 'verify_modified':
    assert Tag.objects.get(slug=slug).name == modified, 'Unselected database data changed'
else:
    raise SystemExit(f'Invalid test action: {action}')
PY
  )
}

echo 'Creating a real NetBox tag as identifiable backup test data'
netbox_tag seed

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
pg_restore --list "$backup_file" | grep 'django_migrations' >/dev/null ||
  fail 'NetBox migration table is missing from the backup'
systemctl is-active --quiet netbox.service netbox-rq.service ||
  fail 'NetBox services stopped during the backup'
wait_for_http || fail 'NetBox HTTP is unavailable after backup'
echo 'PASS: real NetBox database backup, verification and contents'

echo 'Modifying the NetBox tag, then restoring the real database backup'
netbox_tag mutate
"${cli[@]}" restore db --file "$backup_file" --yes --acknowledge-data-loss
netbox_tag verify

systemctl is-active --quiet netbox.service netbox-rq.service nginx.service \
  redis-server.service postgresql@16-main.service ||
  fail 'A NetBox service did not recover after database restore'
wait_for_http || fail 'NetBox HTTP is unavailable after database restore'
"${cli[@]}" services status
echo 'PASS: database restore recovered the original NetBox tag and healthy services'

echo 'Checking the pre-restore safety backup and database staging cleanup'
mapfile -t backups_after < <("${cli[@]}" backup list --kind db)
[[ "${#backups_after[@]}" -eq 2 ]] || fail 'Expected original and pre-restore database backups'
safety_file=''
for candidate in "${backups_after[@]}"; do
  if [[ "$candidate" == *-pre-restore-* ]]; then safety_file=$candidate; fi
done
[[ -n "$safety_file" ]] || fail 'Restore did not create a pre-restore safety backup'
"${cli[@]}" backup verify --kind db --file "$safety_file"
python3 - "$safety_file.json" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
assert manifest['kind'] == 'db', 'Unexpected safety backup kind'
assert manifest['purpose'] == 'pre_restore', 'Backup is not tagged as pre-restore'
PY
remaining=$(runuser -u postgres -- psql -X -A -t -d postgres \
  -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'")
[[ "$remaining" == 0 ]] || fail 'A staged restore database was left behind'
echo 'PASS: pre-restore safety backup and staging cleanup'


echo 'Creating a real NetBox media fixture for backup and restore'
media_dir='/opt/netbox/netbox/media'
fixture_dir="$media_dir/netbox-ops-ci"
fixture="$fixture_dir/media-restore.txt"
[[ -d "$media_dir" && ! -L "$media_dir" ]] || fail 'NetBox media directory is missing or unsafe'
[[ ! -e "$fixture_dir" && ! -L "$fixture_dir" ]] || fail 'Media test fixture already exists'
install -d -o netbox -g netbox -m 750 "$fixture_dir"
printf 'netbox-ops original media fixture\n' > "$fixture"
chown netbox:netbox "$fixture"
chmod 640 "$fixture"
original_sha=$(sha256sum "$fixture" | cut -d' ' -f1)

echo 'Backing up real NetBox media through netbox-ops'
[[ -z "$("${cli[@]}" backup list --kind media)" ]] ||
  fail 'Expected no existing media backups in disposable VM'
"${cli[@]}" backup media
mapfile -t media_backups < <("${cli[@]}" backup list --kind media)
[[ "${#media_backups[@]}" -eq 1 ]] || fail 'Expected one media backup'
media_backup="${media_backups[0]}"
[[ "$media_backup" == "$backup_dir/"* && -f "$media_backup" && ! -L "$media_backup" ]] ||
  fail 'Media backup is missing or unsafe'
"${cli[@]}" backup verify --kind media --file "$media_backup"

echo 'Changing media data and restoring the original archive'
printf 'netbox-ops modified media fixture\n' > "$fixture"
[[ "$(sha256sum "$fixture" | cut -d' ' -f1)" != "$original_sha" ]] ||
  fail 'Media fixture mutation did not change the contents'
"${cli[@]}" restore media --file "$media_backup" --yes --acknowledge-data-loss
[[ -f "$fixture" && ! -L "$fixture" ]] || fail 'Restored media fixture is missing'
[[ "$(sha256sum "$fixture" | cut -d' ' -f1)" == "$original_sha" ]] ||
  fail 'Restored media contents differ from the original'
[[ "$(stat -c '%U:%G' "$fixture")" == 'netbox:netbox' ]] ||
  fail 'Restored media ownership is incorrect'
[[ "$(stat -c '%a' "$fixture")" == '640' ]] ||
  fail 'Restored media permissions are incorrect'
systemctl is-active --quiet netbox.service netbox-rq.service nginx.service ||
  fail 'NetBox services are not healthy after media restore'
wait_for_http || fail 'NetBox HTTP is unavailable after media restore'
rm -f -- "$fixture"
rmdir -- "$fixture_dir"
echo 'PASS: real NetBox media backup and restore with content, ownership and mode checks'

echo 'Preparing database and media fixtures for combined restore checks'
[[ ! -e "$fixture_dir" && ! -L "$fixture_dir" ]] || fail 'Combined fixture directory already exists'
install -d -o netbox -g netbox -m 750 "$fixture_dir"
printf 'netbox-ops original combined media fixture\n' > "$fixture"
chown netbox:netbox "$fixture"
chmod 640 "$fixture"
combined_original_sha=$(sha256sum "$fixture" | cut -d' ' -f1)
netbox_tag verify

echo 'Backing up the real NetBox database and media together'
[[ -z "$("${cli[@]}" backup list --kind combined)" ]] ||
  fail 'Expected no combined backups in disposable VM'
"${cli[@]}" backup combined
mapfile -t combined_backups < <("${cli[@]}" backup list --kind combined)
[[ "${#combined_backups[@]}" -eq 1 ]] || fail 'Expected one combined backup'
combined_backup="${combined_backups[0]}"
[[ "$combined_backup" == "$backup_dir/"* && -f "$combined_backup" && ! -L "$combined_backup" ]] ||
  fail 'Combined backup is missing or unsafe'
"${cli[@]}" backup verify --kind combined --file "$combined_backup"

combined_mutate_media() {
  printf 'netbox-ops modified combined media fixture\n' > "$fixture"
  [[ "$(sha256sum "$fixture" | cut -d' ' -f1)" != "$combined_original_sha" ]] ||
    fail 'Combined media mutation did not change the contents'
}
combined_verify_media_original() {
  [[ -f "$fixture" && ! -L "$fixture" ]] || fail 'Combined media fixture is missing'
  [[ "$(sha256sum "$fixture" | cut -d' ' -f1)" == "$combined_original_sha" ]] ||
    fail 'Original combined media content was not restored'
  [[ "$(stat -c '%U:%G' "$fixture")" == netbox:netbox ]] ||
    fail 'Combined media ownership changed'
  [[ "$(stat -c '%a' "$fixture")" == 640 ]] ||
    fail 'Combined media permissions changed'
}
combined_verify_healthy() {
  systemctl is-active --quiet netbox.service netbox-rq.service nginx.service \
    redis-server.service postgresql@16-main.service ||
    fail 'NetBox services are unhealthy after combined restore'
  wait_for_http || fail 'NetBox HTTP is unavailable after combined restore'
}

echo 'Testing full combined restore of both modified data sets'
netbox_tag mutate
combined_mutate_media
"${cli[@]}" restore combined --file "$combined_backup" --yes --acknowledge-data-loss
netbox_tag verify
combined_verify_media_original
combined_verify_healthy
echo 'PASS: combined restore recovered database and media'

echo 'Testing database-only restore preserves modified media'
netbox_tag mutate
combined_mutate_media
modified_media_sha=$(sha256sum "$fixture" | cut -d' ' -f1)
"${cli[@]}" restore combined --file "$combined_backup" --only db --yes --acknowledge-data-loss
netbox_tag verify
[[ "$(sha256sum "$fixture" | cut -d' ' -f1)" == "$modified_media_sha" ]] ||
  fail 'Database-only restore changed unselected media'
combined_verify_healthy
echo 'PASS: database-only restore preserved unselected media'

echo 'Testing media-only restore preserves modified database data'
netbox_tag mutate
"${cli[@]}" restore combined --file "$combined_backup" --only media --yes --acknowledge-data-loss
netbox_tag verify_modified
combined_verify_media_original
combined_verify_healthy
rm -f -- "$fixture"
rmdir -- "$fixture_dir"
echo 'PASS: media-only restore preserved unselected database data'


echo 'Testing rollback after database and media replacement begins'
# The callback is injected by the disposable VM test harness only. Production
# CLI code and normal restore paths do not expose a failure-injection switch.
python3 - "$combined_backup" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.backups import BackupManager
from netbox_ops.config import load_config

source = Path(sys.argv[1])
reached_replace = False

events = []

def fail_after_replace(event):
    global reached_replace
    events.append(event)
    if event.task.phase == 'replace' and event.state == 'done':
        reached_replace = True
        raise RuntimeError('CI injected failure after live replacement')

try:
    BackupManager(load_config()).restore(
        'combined', source, task_progress=fail_after_replace
    )
except RuntimeError as exc:
    assert str(exc) == 'CI injected failure after live replacement', str(exc)
else:
    raise AssertionError('Restore unexpectedly succeeded despite injected failure')
assert reached_replace, 'Restore did not reach the post-replacement checkpoint'
states = {event.task.phase: event.state for event in events}
assert states['rollback_files'] == 'done', states
assert states['rollback_db'] == 'done', states
assert states['rollback_services'] == 'done', states
assert states['restart_services'] == 'not_run', states
print('PASS: injected post-replacement failure reached rollback path')
PY
netbox_tag verify_modified
[[ ! -e "$fixture_dir" ]] || fail 'Rollback did not restore pre-restore media state'
combined_verify_healthy
remaining=$(runuser -u postgres -- psql -X -A -t -d postgres \
  -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'")
[[ "$remaining" == 0 ]] || fail 'Rollback left a staging database'
find "$media_dir" -maxdepth 1 -name '.netbox-ops-restore-*' | grep -q . &&
  fail 'Rollback left media staging directories'
echo 'PASS: post-replacement failure restored database, media and services'

echo 'Testing a rejected corrupt combined archive without changing live data'
# The disposable VM is the only place where the test archive is deliberately damaged.
# The database tag is currently modified, while the media fixture was cleaned up.
netbox_tag verify_modified
[[ ! -e "$fixture_dir" ]] || fail 'Media fixture unexpectedly survived cleanup'
python3 - "$combined_backup" <<'PY'
import sys
from pathlib import Path

archive = Path(sys.argv[1])
# Damage the gzip header, not the trailing padding: the latter can be ignored
# by gzip readers and produce a false negative for this regression.
with archive.open('r+b') as stream:
    assert stream.read(2) == b'\x1f\x8b', 'Unexpected archive format'
    stream.seek(0)
    stream.write(b'XX')
PY
failure_log=$(mktemp)
if "${cli[@]}" restore combined --file "$combined_backup" --yes --acknowledge-data-loss >"$failure_log" 2>&1; then
  cat "$failure_log" >&2
  rm -f -- "$failure_log"
  fail 'Restore unexpectedly accepted a corrupt combined archive'
fi
echo 'Expected restore rejection diagnostics:'
cat "$failure_log"
rm -f -- "$failure_log"
netbox_tag verify_modified
[[ ! -e "$fixture_dir" ]] || fail 'Rejected restore changed media data'
combined_verify_healthy
remaining=$(runuser -u postgres -- psql -X -A -t -d postgres \
  -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'nbo_stage_%'")
[[ "$remaining" == 0 ]] || fail 'Rejected restore left a staging database'
echo 'PASS: corrupt archive rejected without changing live data or service health'
