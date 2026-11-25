#!/usr/bin/env bash
# Coverage-only instrumentation for the disposable baseline E2E guest.
# Never run on a production system or on a self-upgrade candidate.
set -euo pipefail
[[ $EUID == 0 && -f /run/netbox-ops-ci-e2e ]]
[[ "$(cat /run/netbox-ops-ci-e2e)" == netbox-ci-e2e ]]
[[ "$(hostname)" == netbox-ci-1 ]]

app=/opt/netbox-ops
python="$app/venv/bin/python"
work=/run/netbox-ops-coverage
export_dir=/run/netbox-ops-coverage-export

case "${1:-}" in
  setup)
    # Fail rather than reusing a directory or overwriting existing instrumentation.
    umask 077
    mkdir "$work"
    /usr/bin/python3 -I -m venv "$work/tool"
    "$work/tool/bin/python" -I -m pip install --disable-pip-version-check coverage==7.10.7
    tool_site=$("$work/tool/bin/python" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    app_site=$("$python" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    [[ "$tool_site" == "$work/tool/"* && "$app_site" == "$app/venv/"* ]]
    hook="$app_site/zz_netbox_ops_ci_coverage.pth"
    [[ ! -e "$hook" && ! -L "$hook" ]]
    cat > "$work/coveragerc" <<EOF
[run]
branch = true
parallel = true
source =
    $app/netbox_ops
data_file = $work/.coverage
EOF
    # The installed launcher remains unmodified. Only its private guest venv
    # loads the hook; do not propagate coverage settings to NetBox subprocesses.
    printf '%s\n%s\n' "$tool_site" \
      "import os; import coverage; os.environ['COVERAGE_PROCESS_START'] = '$work/coveragerc'; coverage.process_startup(); os.environ.pop('COVERAGE_PROCESS_START', None)" \
      > "$hook"
    /usr/local/sbin/netbox-ops --version
    compgen -G "$work/.coverage.*" > /dev/null
    ;;
  collect)
    [[ -d "$work" && ! -L "$work" ]]
    cd "$app"
    export COVERAGE_RCFILE="$work/coveragerc"
    "$work/tool/bin/python" -I -m coverage combine --keep
    "$work/tool/bin/python" -I -m coverage report -m | tee "$work/coverage.txt"
    "$work/tool/bin/python" -I -m coverage json -o "$work/coverage.json"
    "$work/tool/bin/python" -I -m coverage xml -o "$work/coverage.xml"
    "$python" --version > "$work/python-version.txt"
    # Fail if only a startup probe was measured. The baseline E2E suite
    # exercises real backups/restores through the installed CLI.
    "$work/tool/bin/python" -I - "$work/coverage.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as report:
    files = json.load(report)["files"]
for module in ("backups.py", "services.py"):
    matches = [
        data for path, data in files.items()
        if path.endswith("/" + module) or path == module
    ]
    if len(matches) != 1 or len(matches[0]["executed_lines"]) < 100:
        raise SystemExit(f"Insufficient E2E instrumentation evidence for {module}")
PY
    # Transfer read-only evidence, not the raw coverage database or VM secrets.
    mkdir -m 0750 "$export_dir"
    chgrp ubuntu "$export_dir"
    for file in coverage.txt coverage.json coverage.xml python-version.txt; do
      install -o root -g ubuntu -m 0640 "$work/$file" "$export_dir/$file"
    done
    ;;
  *)
    echo 'Usage: netbox-vm-coverage.sh setup|collect' >&2
    exit 2
    ;;
esac
