#!/usr/bin/env bash
# Only the CI host may launch this fixture in a disposable overlay.
set -Eeuo pipefail
# Report the failed fixture line without echoing commands or credentials.
trap 'echo "FAIL: upgrade E2E fixture line $LINENO" >&2' ERR
scenario=${1:?Missing scenario}
source_version=${2:-4.4.10}
fixture=${3:-fresh}
case "$source_version:$fixture" in
  4.4.10:fresh|4.4.9:fresh) ;;
  4.4.9:pre-migrated) [[ "$scenario" == cross-minor ]] ;;
  *) exit 2 ;;
esac
[[ $(id -u) == 0 && -f /run/netbox-ops-ci-e2e ]]
[[ $(cat /run/netbox-ops-ci-e2e) == netbox-ci-e2e ]]
case "$(systemd-detect-virt --vm)" in qemu|kvm) ;; *) exit 1 ;; esac
case "$scenario" in netbox|plugins|combined|cross-minor|failure) ;; *) exit 2 ;; esac
root=/opt/netbox
cli=(/usr/local/sbin/netbox-ops --lang en)
status_url=http://127.0.0.1/api/status/
backup_dir=/var/lib/netbox-ops/backups
marker=/var/lib/netbox-ops/upgrade.pending
[[ -d "$root" && ! -e "$marker" ]]
if [[ "$fixture" == fresh ]]; then
  [[ ! -e "$root/.git" ]]
else
  [[ -d "$root/.git" ]]
  [[ $(git -C "$root" describe --tags --exact-match) == v4.4.9 ]]
fi
[[ -z $(find "$backup_dir" -mindepth 1 -maxdepth 1 -print -quit) ]]
umask 022
# Check isolation configured at boot, before creating the source fixture.
for unit in apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service; do
  state=$(systemctl show -p UnitFileState --value "$unit")
  case "$state" in masked|masked-runtime) ;; *) echo "FAIL: background APT unit is not masked: $unit" >&2; exit 1 ;; esac
  case "$(systemctl show -p ActiveState --value "$unit")" in inactive|failed) ;; *) echo "FAIL: background APT unit active: $unit" >&2; exit 1 ;; esac
done
if [[ "$fixture" == fresh ]]; then
  apt-get update -qq
  NEEDRESTART_MODE=l apt-get install -y --no-install-recommends git
fi
old=4.7.2
target=4.7.3
python=/usr/bin/python3.12
plugin=netbox-inventory
old_plugin=2.7.0
new_plugin=2.7.1
if [[ "$scenario" == cross-minor ]]; then
  old=$source_version
  plugin=netbox-reorder-rack
  old_plugin=1.1.4
  new_plugin=1.1.5
  if [[ "$fixture" == fresh ]]; then
    NEEDRESTART_MODE=l apt-get install -y --no-install-recommends libldap-dev libsasl2-dev libssl-dev python3.12-dev
  fi
fi
[[ "$scenario" != plugins ]] || target=$old
# Preserve the fixture configuration, never the newer schema. A fresh database
# is created for the older release rather than attempting a downgrade.
scratch=$(mktemp -d)
chmod 700 "$scratch"
trap 'rm -rf -- "$scratch"' EXIT
overlap_pin=''
if [[ "$fixture" == fresh ]]; then
  systemctl stop netbox-rq netbox
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
    printf '%s\n' 'tomli-w==1.2.0' >> "$root/local_requirements.txt"
  elif [[ "$scenario" == cross-minor ]]; then
    printf '\nPLUGINS = ["netbox_reorder_rack"]\n' >> "$root/netbox/netbox/configuration.py"
    printf '%s==%s\n' "$plugin" "$old_plugin" > "$root/local_requirements.txt"
    printf '%s\n' 'tomli-w==1.2.0' 'django-auth-ldap==5.4.0' >> "$root/local_requirements.txt"
  else
    printf '\nPLUGINS = []\n' >> "$root/netbox/netbox/configuration.py"
  fi
  # Keep an ordinary local declaration that matches a core pin at the target.
  case "$scenario" in
    plugins) overlap_pin='Django==6.1.1' ;;
    netbox|combined) overlap_pin='pymdown-extensions==12.1' ;;
    *) overlap_pin='' ;;
  esac
  if [[ -n "$overlap_pin" ]]; then
    printf '# preserve local intent\n%s\n' "$overlap_pin" >> "$root/local_requirements.txt"
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
  # These source tags leave pymdown-extensions unpinned; 13.x breaks
  # their documentation builders. Use the exact pin from upstream 4.7.3
  # only while creating the source fixture. The tested upgrade receives
  # no fixture constraint, and executes the unmodified official script.
  if [[ "$old" == 4.7.2 || "$old" == 4.4.10 || "$old" == 4.4.9 ]]; then
    printf '%s\n' 'pymdown-extensions==12.1' > "$scratch/source-constraints.txt"
    if [[ "$scenario" == cross-minor ]]; then
      printf '%s\n' 'python-ldap==3.4.8' >> "$scratch/source-constraints.txt"
    fi
    (cd "$root" && PIP_CONSTRAINT="$scratch/source-constraints.txt" PYTHON="$python" bash upgrade.sh)
  else
    (cd "$root" && PYTHON="$python" bash upgrade.sh)
  fi
  systemctl start netbox netbox-rq
  echo "Fixture installed: NetBox $old"
else
  # The baseline was built without runtime credentials; provision has already
  # installed this checkout, created fresh credentials and checked migrations.
  [[ $(git -C "$root" describe --tags --exact-match) == "v$old" ]]
  diff -u <(printf '%s\n' "$plugin==$old_plugin" 'tomli-w==1.2.0' 'django-auth-ldap==5.4.0') "$root/local_requirements.txt"
  "$root/venv/bin/python" -c 'import ldap, importlib.metadata as m; assert m.version("netbox-reorder-rack")=="1.1.4"; assert m.version("tomli-w")=="1.2.0"; assert m.version("django-auth-ldap")=="5.4.0"; assert m.version("python-ldap")=="3.4.8"'
  for unit in netbox netbox-rq; do systemctl is-active --quiet "$unit"; done
  echo "Fixture pre-migrated: NetBox $old"
fi
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
printf 'Source fixture: %s; NetBox %s; revision %s\n' "$fixture" "$old" "$(git -C "$root" rev-parse HEAD)"
if [[ "$scenario" == cross-minor ]]; then
  printf 'Source dependency list SHA256: %s\n' "$("$root/venv/bin/python" -m pip list --format=freeze | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"
fi
if [[ "$scenario" == cross-minor ]]; then
  # This disposable database has no ltree yet. Exercise both the denied
  # migration role and the verified administrative path before the real
  # native migration scenario. Never change roles in production code.
  [[ $(runuser -u postgres -- psql -X -At -d netbox -c "SELECT count(*) FROM pg_extension WHERE extname='ltree'") == 0 ]]
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d postgres -c 'ALTER DATABASE netbox OWNER TO postgres'
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d netbox -c 'REVOKE CREATE ON DATABASE netbox FROM netbox; REVOKE CREATE ON SCHEMA public FROM PUBLIC, netbox; GRANT USAGE ON SCHEMA public TO netbox'
  install -d -m 0700 "$scratch/ltree-backups"
  python3 - "$scratch/ltree-backups" <<'PY'
import sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.config import load_config
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_ltree import build_ltree_review, apply_ltree_review

cfg = replace(load_config(), backup_directory=Path(sys.argv[1]))
review = build_ltree_review(cfg)
assert review.evidence.status == 'permission', review.evidence
assert review.fingerprint and review.evidence.admin_port, review.evidence
try:
    apply_ltree_review(cfg, review, 'wrong fingerprint')
except UpgradeExecutionError:
    pass
else:
    raise AssertionError('Unapproved ltree action accepted')
assert not list(cfg.backup_directory.iterdir())
assert build_ltree_review(cfg) == review
backup = apply_ltree_review(cfg, review, review.fingerprint)
assert backup.is_file()
observed = build_ltree_review(cfg)
assert observed.evidence.status == 'installed' and observed.fingerprint is None, observed
try:
    apply_ltree_review(cfg, review, review.fingerprint)
except UpgradeExecutionError:
    pass
else:
    raise AssertionError('Stale ltree approval accepted')
print('PASS: real denied migration role, verified local administrative ltree installation and stale approval')
PY
  wait_health "$old"
  # Restore the disposable fixture's initial owner/extension state so the
  # unchanged upstream upgrade script must still exercise native creation.
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d netbox -c 'DROP EXTENSION ltree'
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d postgres -c 'ALTER DATABASE netbox OWNER TO netbox'
  [[ $(runuser -u postgres -- psql -X -At -d netbox -c "SELECT count(*) FROM pg_extension WHERE extname='ltree'") == 0 ]]
  wait_health "$old"
fi
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

if [[ "$scenario" == plugins || "$scenario" == combined ]]; then
  if "${cli[@]}" upgrade plugins --plugin tomli-w > "$scratch/rejected-non-plugin" 2>&1; then
    echo 'FAIL: additional local package was accepted as a plugin' >&2
    exit 1
  fi
  grep -Fq 'Select only reliably mapped installed configured plugins' "$scratch/rejected-non-plugin"
  "${cli[@]}" upgrade inspect > "$scratch/plugin-inventory"
  grep -Fq "$plugin: $old_plugin" "$scratch/plugin-inventory"
  if grep -Fq 'tomli-w:' "$scratch/plugin-inventory"; then
    echo 'FAIL: additional local package was offered for plugin upgrade' >&2
    exit 1
  fi
  echo 'PASS: additional local package excluded from plugin selection'
fi


# Reproduce the reported incompatible plugin upgrade and preserve a bare local dependency
# against real published releases, before any maintenance begins.
if [[ "$scenario" == cross-minor ]]; then
  # The source fixture installed the exact version. Simulate the operator's
  # existing bare declaration without changing any installed package.
  sed -i -e 's/^tomli-w==1.2.0$/tomli-w  # preserve local intent/' \
         -e 's/^django-auth-ldap==5.4.0$/django-auth-ldap  # preserve LDAP declaration/' "$root/local_requirements.txt"
  for unit in netbox netbox-rq; do
    systemctl show -p ActiveEnterTimestampMonotonic --value "$unit" > "$scratch/$unit"
  done
  python3 - "$target" "$plugin" "$new_plugin" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.config import load_config
from netbox_ops.plugin_inventory import inventory_plugins
from netbox_ops.upgrade_ui import discover_context, make_plan
target, plugin, version = sys.argv[1:]
cfg = load_config()
context = discover_context(cfg)
requirements = context.installation.root / 'local_requirements.txt'
original = requirements.read_bytes()
bad = make_plan(context, 'plugins', None, {plugin: version})
assert not bad.can_execute
check = next(c for stage in bad.stages for c in stage.plugin_checks if c.package == plugin)
assert check.status == 'blocked', check
assert any(e.status == 'blocks' and '4.7.0' in e.detail and 'PluginConfig' in e.detail
           for e in check.evidence), check.evidence
assert all(r.resolution.status == 'not-run' for r in bad.dependency_reviews)
assert requirements.read_bytes() == original
print('PASS: real reorder-rack release rejected on NetBox 4.4.x')

inventory = inventory_plugins(context.installation, local_pin_names=('tomli-w', 'django-auth-ldap', 'python-ldap'))
assert not any(p.package in ('tomli-w', 'django-auth-ldap', 'python-ldap') for p in inventory.proposals)
plan = make_plan(context, 'stack', target, {plugin: version})
assert plan.can_execute, plan.blockers
assert all(r.resolution.status == 'passed' and r.resolution.checked_core for r in plan.dependency_reviews)
assert all(dict(r.resolution.packages).get('tomli-w') == '1.2.0' for r in plan.dependency_reviews)
assert all(dict(r.resolution.packages).get('django-auth-ldap') == '5.4.0' for r in plan.dependency_reviews)
assert all(dict(r.resolution.packages).get('python-ldap') == '3.4.8' for r in plan.dependency_reviews)
assert not any(d.name == 'tomli-w' for r in plan.dependency_reviews for d in r.local_diagnostics)
assert any(r.ltree in ('installed', 'available') for r in plan.dependency_reviews)
assert requirements.read_bytes() == original
assert not list(Path('/var/lib/netbox-ops/pin-recovery').glob('pins-*/local_requirements.txt'))
print('PASS: bare non-plugin requirement resolved at installed version without permanent pin')

PY
  [[ $(git -C "$root" rev-parse HEAD) == "$commit" && ! -e "$marker" ]]
  [[ -z $(find "$backup_dir" -mindepth 1 -maxdepth 1 -print -quit) ]]
  for unit in netbox netbox-rq; do
    [[ $(systemctl show -p ActiveEnterTimestampMonotonic --value "$unit") == "$(cat "$scratch/$unit")" ]]
  done
  wait_health "$old"
fi

case "$scenario" in
  plugins) args=(upgrade plugins --plugin "$plugin==$new_plugin") ;;
  combined|cross-minor) args=(upgrade stack --netbox-version "$target" --plugin "$plugin==$new_plugin") ;;
  *) args=(upgrade netbox --version "$target") ;;
esac
"${cli[@]}" "${args[@]}" --details | tee "$scratch/plan"
if [[ -n "$overlap_pin" ]]; then
  grep -F 'Redundant identical pin; retained, not edited.' "$scratch/plan" | grep -Fq "$overlap_pin"
  grep -Fxq "$overlap_pin" "$root/local_requirements.txt"
  grep -Fxq '# preserve local intent' "$root/local_requirements.txt"
  echo 'PASS: redundant core/local pin reviewed without modification'
fi
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

# Exercise real TUI selection, review, cancellation and execution. Only the
# screen is synthetic; inventory, planning, execution and recovery are real.
if [[ "$scenario" == netbox || "$scenario" == plugins || "$scenario" == combined ]]; then
  python3 - "$scenario" "$target" "$plugin" "$new_plugin" "$status_url" <<'PY'
import curses
import sys
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.config import load_config
from netbox_ops.i18n import Translator
from netbox_ops.tui import CursesMenu
from netbox_ops.upgrade_dashboard import UpgradeDashboard
from netbox_ops.upgrade_plan import UpgradePlanError
from tests.helpers.fake_screen import FakeScreen

scenario, target, plugin, plugin_target, status_url = sys.argv[1:]
cfg = load_config()
class TimedScreen(FakeScreen):
    def getch(self):
        # Match curses timeout behavior while backend work is in progress.
        # Do not deliver a queued approval or result acknowledgement to a job.
        if getattr(self, 'wait_ms', -1) >= 0:
            time.sleep(self.wait_ms / 1000)
            return -1
        return super().getch()

screen = TimedScreen(dimensions=(34, 140))
menu = CursesMenu(screen, Translator('en'))
dashboard = UpgradeDashboard(menu)
# Exercise actual initial dashboard discovery, not a preloaded model.
with ThreadPoolExecutor(max_workers=1) as pool:
    dashboard._discover(pool, 'suggest')
    assert dashboard._draw()
    initial = screen.frames[-1]
    assert any(text == menu.t('ut_discovery_heading') for _, _, text, _ in initial)
    assert not dashboard._enabled('execute') and not dashboard._enabled('validate')
    assert dashboard._rows()[0].current == '?' and dashboard._rows()[0].target == '?'
    deadline = time.monotonic() + 180
    while dashboard.future is not None:
        dashboard._poll(pool)
        dashboard._draw()
        assert not dashboard._enabled('execute')
        assert time.monotonic() < deadline, 'Initial discovery exceeded its test budget'
        if dashboard.future is not None:
            time.sleep(0.15)
assert dashboard.model is not None and dashboard.snapshot is not None
assert dashboard._inventory_valid and not dashboard.error
model = dashboard.model
context = model.context
assert context.tags and model.installed.netbox == context.installation.version
assert any(text == context.installation.version for frame in screen.frames for _, _, text, _ in frame)
assert dashboard.plan is None
print(f'PASS: {scenario} real dashboard initial discovery and execution gating')
model.reset()
# The fixture checks exact targets independently of preliminary suggestions.
dashboard.snapshot = None
assert not dashboard._enabled('validate') and not dashboard._enabled('execute')
try:
    model.select('tomli-w', '1.2.0')
except UpgradePlanError:
    pass
else:
    raise AssertionError('Non-plugin accepted as target')

def choose(component, version):
    # Enumerate the full eligible list without assessing every version.
    result = model.evaluate(model.request('listing', component))
    assert model.accept(result)
    installed = model.installed.netbox if component is None else dict(model.installed.plugins)[component]
    assert result.rows[0].target == installed
    position = next(i for i, row in enumerate(result.rows) if row.target == version)
    screen.keys = iter([curses.KEY_DOWN] * position + [10])
    assert dashboard._picker(component) == version
    model.select(component, version)

if scenario != 'plugins':
    choose(None, target)
if scenario != 'netbox':
    choose(plugin, plugin_target)
operation, netbox, pins = model.selection.plan_arguments(model.installed)
assert operation == {'netbox': 'netbox', 'plugins': 'plugins', 'combined': 'stack'}[scenario]
assert netbox == (None if scenario == 'plugins' else target)
assert pins == ({} if scenario == 'netbox' else {plugin: plugin_target})
result = model.evaluate(model.request('assess'))
assert model.accept(result)
dashboard.snapshot = result
dashboard._validate()
assert dashboard._enabled('execute'), 'Fixture plan is not executable'
plan = dashboard.plan
assert plan.target_netbox == target and dict(plan.selected_plugins) == pins

# Render both supported languages and representative terminal sizes.
for language in ('en', 'pt'):
    for size in ((16, 50), (34, 140)):
        menu.t = dashboard.t = Translator(language)
        screen.dimensions = size
        assert dashboard._draw()
        frame = screen.frames[-1]
        headers = ' '.join(text for y, _, text, _ in frame if y == 4)
        assert menu.t('ut_group_core') in headers, headers
        assert menu.t('ut_col_component') not in headers, headers
        for name in ('current', 'target', 'status'):
            assert menu.t('ut_col_' + name) in headers, headers
        print(f'PASS: {scenario} dashboard {language} {size[1]}x{size[0]}: {headers}')

# Actual review, URL input and literal-token prompt; Esc must cancel
# before the execution boundary. Guard against accidental mutation.
screen.dimensions = (34, 140)
screen.keys = iter([10, *map(ord, status_url), 10, 10, 27])
with patch('netbox_ops.upgrade_ui.execute_plan',
           side_effect=AssertionError('Cancelled review attempted execution')) as execute:
    assert not menu._execute_reviewed_upgrade('Upgrade', cfg, operation, plan)
    execute.assert_not_called()
print(f'PASS: {scenario} real TUI discovery, exact picker, validation and cancellation')
# Check cancellation before deliberately approving the real upgrade.
assert subprocess.check_output(['git', '-C', '/opt/netbox', 'rev-parse', 'HEAD'],
                               text=True).strip() == context.installation.commit
assert not Path('/var/lib/netbox-ops/upgrade.pending').exists()
assert not any(cfg.backup_directory.iterdir())
assert dashboard._enabled('execute')
# Use the exact reviewed plan, the real confirmation prompts and the actual
# worker/progress path. Do not patch execute_plan or the backend here.
menu.t = dashboard.t = Translator('en')
approval = [10, *map(ord, status_url), 10, 10, *map(ord, 'UPGRADE'), 10]
if plan.requires_strong_confirmation:
    approval.extend([*map(ord, 'UNCERTAIN'), 10])
screen.keys = iter([*approval, 10])
assert menu._execute_reviewed_upgrade('Upgrade', cfg, operation, plan)
assert any('Upgrade validated.' in text for _, _, text, _ in screen.frames[-1]), screen.frames[-1]
model.reset()
assert not dashboard._enabled('execute')
print(f'PASS: {scenario} real TUI approval and execution')
PY
fi

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
  # Cross-minor exercises the real combined CLI execution path. The three other
  # success scenarios executed the exact reviewed plan through the TUI.
  if [[ "$scenario" == cross-minor ]]; then
    "${cli[@]}" "${args[@]}" --execute --approve-plan "$fingerprint" \
      --accept-unknowns --health-url "$status_url"
  fi
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
  grep -qx 'tomli-w==1.2.0' "$root/local_requirements.txt"
  "$root/venv/bin/python" -c 'import importlib.metadata as m; assert m.version("tomli-w")=="1.2.0"'
  "$root/venv/bin/python" -c 'import importlib.metadata as m,sys; assert m.version(sys.argv[1])==sys.argv[2]' "$plugin" "$new_plugin"
  curl -fsS "$status_url" | python3 -c 'import sys,json; assert sys.argv[1] in json.load(sys.stdin)["plugins"].values()' "$new_plugin"
fi
if [[ "$scenario" == cross-minor ]]; then
  grep -Fxq "$plugin==$new_plugin" "$root/local_requirements.txt"
  grep -Fxq 'tomli-w  # preserve local intent' "$root/local_requirements.txt"
  grep -Fxq 'django-auth-ldap  # preserve LDAP declaration' "$root/local_requirements.txt"
  "$root/venv/bin/python" -c 'import ldap, importlib.metadata as m; assert m.version("django-auth-ldap")=="5.4.0"; assert m.version("python-ldap")=="3.4.8"'
  echo 'PASS: unpinned LDAP declaration preserved and native LDAP module imports'
  "$root/venv/bin/python" -c 'import importlib.metadata as m,sys; assert m.version(sys.argv[1])==sys.argv[2]; assert m.version("tomli-w")=="1.2.0"' "$plugin" "$new_plugin"
  curl -fsS "$status_url" | python3 -c 'import sys,json; assert sys.argv[1] in json.load(sys.stdin)["plugins"].values()' "$new_plugin"
  python3 - "$root" "$new_plugin" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/netbox-ops')
from netbox_ops.plugin_compatibility import parse_plugin_config, fetch_pypi_json, fetch_release_declaration
root, version = sys.argv[1:]
# Compare the actual installed distribution's source to the exact-tag gate;
# do not import the remote module into the operations process.
source, = Path(root).glob('venv/lib/python*/site-packages/netbox_reorder_rack/__init__.py')
installed = parse_plugin_config(source.read_text(), 'netbox_reorder_rack', version, 'https://pypi.org/project/netbox-reorder-rack/')
metadata = fetch_pypi_json('netbox-reorder-rack', version)
declaration = fetch_release_declaration(metadata, version)
assert declaration is not None
gate = declaration.runtime_gate or declaration
assert installed is not None and installed.enforced and gate.enforced
assert installed.min_netbox == gate.min_netbox == '4.7.0'
assert installed.max_netbox == gate.max_netbox is None
print('PASS: installed published plugin artifact matches exact-tag runtime gate')
PY
fi
if [[ -n "$overlap_pin" ]]; then
  grep -Fxq "$overlap_pin" "$root/local_requirements.txt"
  grep -Fxq '# preserve local intent' "$root/local_requirements.txt"
  "$root/venv/bin/python" -c 'import importlib.metadata as m, sys; n, v = sys.argv[1].split("=="); assert m.version(n) == v' "$overlap_pin"
  echo 'PASS: redundant local pin and installed version preserved'
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
    assert old in ('4.4.9', '4.4.10') and record['stages'] == [target]
    assert ['netbox-reorder-rack', '1.1.4'] in record['plugin_versions']
    assert ['netbox-reorder-rack', '1.1.5'] in record['stage_commits'][-1]['plugin_versions']
if scenario in ('plugins', 'combined'):
    assert ['netbox-inventory', '2.7.0'] in record['plugin_versions']
    assert ['netbox-inventory', '2.7.1'] in record['stage_commits'][-1]['plugin_versions']
archive = directory / record['archive']
assert BackupManager(replace(cfg, backup_directory=directory)).verify('combined', archive)['netbox_version'] == old
print('PASS: verified protected recovery archive, revision, pins and configuration')
PY
echo "PASS: $scenario real upgrade scenario"
