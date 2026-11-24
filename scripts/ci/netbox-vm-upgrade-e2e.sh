#!/usr/bin/env bash
# Only the CI host may launch this fixture in a disposable overlay.
set -Eeuo pipefail
# Report the failed fixture line without echoing commands or credentials.
trap 'echo "FAIL: upgrade E2E fixture line $LINENO" >&2' ERR
scenario=${1:?Missing scenario}
[[ $(id -u) == 0 && -f /run/netbox-ops-ci-e2e ]]
[[ $(cat /run/netbox-ops-ci-e2e) == netbox-ci-e2e ]]
case "$(systemd-detect-virt --vm)" in qemu|kvm) ;; *) exit 1 ;; esac
case "$scenario" in netbox|plugins|combined|cross-minor|failure) ;; *) exit 2 ;; esac
root=/opt/netbox
cli=(/usr/local/sbin/netbox-ops --lang en)
status_url=http://127.0.0.1/api/status/
backup_dir=/var/lib/netbox-ops/backups
marker=/var/lib/netbox-ops/upgrade.pending
[[ -d "$root" && ! -e "$root/.git" && ! -e "$marker" ]]
[[ -z $(find "$backup_dir" -mindepth 1 -maxdepth 1 -print -quit) ]]
umask 022
# A cloud-image timer may otherwise restart PostgreSQL during fixture migration.
# Stop future timer activations and let any running APT transaction finish.
systemctl stop apt-daily.timer apt-daily-upgrade.timer
for unit in apt-daily.service apt-daily-upgrade.service; do
  for attempt in $(seq 1 180); do
    state=$(systemctl show -p ActiveState --value "$unit")
    case "$state" in inactive|failed) break ;; esac
    sleep 2
  done
  case "$(systemctl show -p ActiveState --value "$unit")" in inactive|failed) ;; *) exit 1 ;; esac
done
systemctl mask --runtime apt-daily.service apt-daily-upgrade.service apt-daily.timer apt-daily-upgrade.timer
apt-get update -qq
NEEDRESTART_MODE=l apt-get install -y --no-install-recommends git
old=4.7.2
target=4.7.3
python=/usr/bin/python3.12
plugin=netbox-inventory
old_plugin=2.7.0
new_plugin=2.7.1
if [[ "$scenario" == cross-minor ]]; then
  old=4.4.10
fi
[[ "$scenario" != plugins ]] || target=$old
# Preserve the fixture configuration, never the newer schema. A fresh database
# is created for the older release rather than attempting a downgrade.
systemctl stop netbox-rq netbox
scratch=$(mktemp -d)
chmod 700 "$scratch"
trap 'rm -rf -- "$scratch"' EXIT
cp "$root/netbox/netbox/configuration.py" "$scratch/configuration.py"
mv "$root" "$scratch/archive-install"
git clone --quiet --depth 1 --branch "v$old" https://github.com/netbox-community/netbox.git "$root"
install -o root -g netbox -m 0640 "$scratch/configuration.py" "$root/netbox/netbox/configuration.py"
# Both tested releases accept DATABASES and reject a simultaneous DATABASE.
# The baseline supplies DATABASES. Permit unauthenticated status checks only
# in this disposable guest (loopback nginx, no production configuration).
printf '\nLOGIN_REQUIRED = False\n' >> "$root/netbox/netbox/configuration.py"
if [[ "$scenario" == plugins || "$scenario" == combined ]]; then
  printf '\nPLUGINS = ["netbox_inventory"]\n' >> "$root/netbox/netbox/configuration.py"
  printf '%s==%s\n' "$plugin" "$old_plugin" > "$root/local_requirements.txt"
else
  printf '\nPLUGINS = []\n' >> "$root/netbox/netbox/configuration.py"
fi
install -m 0644 "$root/contrib/gunicorn.py" "$root/gunicorn.py"
sed -i 's/^workers = 5$/workers = 2/' "$root/gunicorn.py"
install -m 0644 "$root/contrib/netbox.service" /etc/systemd/system/netbox.service
install -m 0644 "$root/contrib/netbox-rq.service" /etc/systemd/system/netbox-rq.service
systemctl daemon-reload
for dir in media scripts reports; do
  install -d -o netbox -g netbox "$root/netbox/$dir"
  chown -R netbox:netbox "$root/netbox/$dir"
done
runuser -u postgres -- dropdb --if-exists netbox
runuser -u postgres -- createdb --owner=netbox netbox
# Both source tags leave pymdown-extensions unpinned; 13.x breaks
# their documentation builders. Use the exact pin from upstream 4.7.3
# only while creating the source fixture. The tested upgrade receives
# no fixture constraint, and executes the unmodified official script.
if [[ "$old" == 4.7.2 || "$old" == 4.4.10 ]]; then
  printf '%s\n' 'pymdown-extensions==12.1' > "$scratch/source-constraints.txt"
  (cd "$root" && PIP_CONSTRAINT="$scratch/source-constraints.txt" PYTHON="$python" bash upgrade.sh)
else
  (cd "$root" && PYTHON="$python" bash upgrade.sh)
fi
systemctl start netbox netbox-rq
echo "Fixture installed: NetBox $old"
wait_health() {
  local version=$1
  for i in $(seq 1 60); do
    if curl -fsS "$status_url" 2>/dev/null | python3 -c \
      'import json,sys; s=json.load(sys.stdin); assert s["netbox-version"]==sys.argv[1] and s["rq-workers-running"]>0' "$version" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "FAIL: NetBox status did not reach $version" >&2
  return 1
}
wait_health "$old"
(cd "$root/netbox" && runuser -u netbox -- "$root/venv/bin/python" manage.py shell -c \
  "from extras.models import Tag; Tag.objects.create(name='Upgrade CI',slug='upgrade-ci',color='123456')")
media="$root/netbox/media/upgrade-ci.txt"
printf 'upgrade media fixture\n' > "$media"
chown netbox:netbox "$media"
chmod 640 "$media"
config_sha=$(sha256sum "$root/netbox/netbox/configuration.py" | cut -d' ' -f1)
media_sha=$(sha256sum "$media" | cut -d' ' -f1)
commit=$(git -C "$root" rev-parse HEAD)
for unit in nginx redis-server postgresql@16-main; do
  systemctl show -p ActiveEnterTimestampMonotonic --value "$unit" > "$scratch/$unit"
done

case "$scenario" in
  plugins) args=(upgrade plugins --plugin "$plugin==$new_plugin") ;;
  combined) args=(upgrade stack --netbox-version "$target" --plugin "$plugin==$new_plugin") ;;
  *) args=(upgrade netbox --version "$target") ;;
esac
"${cli[@]}" "${args[@]}" --details | tee "$scratch/plan"
fingerprint=$(sed -n 's/^Plan fingerprint: //p' "$scratch/plan")
[[ "$fingerprint" =~ ^[0-9a-f]{64}$ ]]
# Prove stale approval is rejected without downtime or recovery writes.
if "${cli[@]}" "${args[@]}" --execute --approve-plan "$(printf '%064d' 0)" \
  --accept-unknowns --health-url "$status_url" > "$scratch/rejected-plan" 2>&1; then
  echo 'FAIL: wrong fingerprint accepted' >&2
  exit 1
fi
# Do not mistake an unrelated discovery failure for approval enforcement.
grep -Fq 'Approved fingerprint does not match an executable plan' "$scratch/rejected-plan"
echo 'PASS: stale fingerprint rejected before downtime'
[[ $(git -C "$root" rev-parse HEAD) == "$commit" && ! -e "$marker" ]]
[[ -z $(find "$backup_dir" -mindepth 1 -maxdepth 1 -print -quit) ]]
wait_health "$old"

if [[ "$scenario" == failure ]]; then
  # Inject a failure only in the test process, after the genuine backup and
  # checkout/script/migrations have completed. Do not add a production flag.
  python3 - "$target" "$status_url" <<'PY'
import sys
from unittest.mock import patch
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.config import load_config
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_ui import discover_context, make_plan, execute_plan
from netbox_ops import upgrade_orchestration as backend
cfg = load_config()
plan = make_plan(discover_context(cfg), 'netbox', sys.argv[1], {})
original = backend.wait_for_netbox_health

def fail_after_health(*args, **kwargs):
    original(*args, **kwargs)
    raise UpgradeExecutionError('CI injected final validation failure')

with patch.object(backend, 'wait_for_netbox_health', fail_after_health):
    try:
        execute_plan(cfg, 'netbox', plan, fingerprint=plan.fingerprint,
                     acknowledge_unknowns=True, health_url=sys.argv[2])
    except UpgradeExecutionError as exc:
        message = str(exc)
        assert 'functional health check' in message and 'recovery: pre-upgrade-' in message
        assert 'completed stages: 1/1' in message and 'no automatic rollback' in message
        print('PASS: real upgrade stopped on injected validation failure; recovery retained')
    else:
        raise AssertionError('Injected failure was not reported')
PY
  [[ -f "$marker" && $(stat -c '%U:%G:%a' "$marker") == root:root:600 ]]
  for unit in netbox netbox-rq; do
    [[ $(systemctl is-active "$unit" || true) == inactive ]]
  done
else
  "${cli[@]}" "${args[@]}" --execute --approve-plan "$fingerprint" \
    --accept-unknowns --health-url "$status_url"
  [[ ! -e "$marker" ]]
  wait_health "$target"
  for unit in netbox netbox-rq; do
    systemctl is-active --quiet "$unit"
  done
fi
if [[ "$scenario" == plugins ]]; then
  [[ $(git -C "$root" rev-parse HEAD) == "$commit" ]]
else
  [[ $(git -C "$root" rev-parse HEAD) != "$commit" ]]
fi
[[ $(sha256sum "$root/netbox/netbox/configuration.py" | cut -d' ' -f1) == "$config_sha" ]]
[[ $(sha256sum "$media" | cut -d' ' -f1) == "$media_sha" ]]
[[ $(stat -c '%U:%G:%a' "$media") == netbox:netbox:640 ]]
(cd "$root/netbox" && runuser -u netbox -- "$root/venv/bin/python" manage.py shell -c \
  "from extras.models import Tag; assert Tag.objects.get(slug='upgrade-ci').name=='Upgrade CI'")
(cd "$root" && "$root/venv/bin/python" netbox/manage.py migrate --check --noinput)
if [[ "$scenario" == plugins || "$scenario" == combined ]]; then
  grep -qx "$plugin==$new_plugin" "$root/local_requirements.txt"
  "$root/venv/bin/python" -c 'import importlib.metadata as m,sys; assert m.version(sys.argv[1])==sys.argv[2]' "$plugin" "$new_plugin"
  curl -fsS "$status_url" | python3 -c 'import sys,json; assert sys.argv[1] in json.load(sys.stdin)["plugins"].values()' "$new_plugin"
fi
for unit in nginx redis-server postgresql@16-main; do
  systemctl is-active --quiet "$unit"
  [[ $(systemctl show -p ActiveEnterTimestampMonotonic --value "$unit") == "$(cat "$scratch/$unit")" ]]
done
# Validate the archive through the existing verifier and compare protected
# metadata with the actual old revision; never print captured configuration.
python3 - "$old" "$target" "$commit" "$scenario" "$config_sha" <<'PY'
import hashlib
import json
import stat
import sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.backups import BackupManager
from netbox_ops.config import load_config
cfg = load_config()
directories = list(cfg.backup_directory.glob('pre-upgrade-*'))
assert len(directories) == 1
directory = directories[0]
assert directory.stat().st_uid == directory.stat().st_gid == 0
assert stat.S_IMODE(directory.stat().st_mode) == 0o700
for path in directory.iterdir():
    assert not path.is_symlink() and path.is_file()
    assert path.stat().st_uid == path.stat().st_gid == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
record = json.loads((directory / 'recovery.json').read_text())
old, target, commit, scenario, config_sha = sys.argv[1:]
assert record['git_commit'] == commit and record['current_netbox'] == old
assert record['target_netbox'] == target
assert hashlib.sha256((directory / 'configuration.py').read_bytes()).hexdigest() == config_sha
assert record['stages'][-1] == target
if scenario == 'cross-minor':
    assert old == '4.4.10' and record['stages'] == [target]
if scenario in ('plugins', 'combined'):
    assert ['netbox-inventory', '2.7.0'] in record['plugin_versions']
    assert ['netbox-inventory', '2.7.1'] in record['stage_commits'][-1]['plugin_versions']
archive = directory / record['archive']
assert BackupManager(replace(cfg, backup_directory=directory)).verify('combined', archive)['netbox_version'] == old
print('PASS: verified protected recovery archive, revision, pins and configuration')
PY
echo "PASS: $scenario real upgrade scenario"
