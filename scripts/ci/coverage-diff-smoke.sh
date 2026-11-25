#!/usr/bin/env bash
# Temporary next-release experiment; never enforce a PR coverage threshold here.
set -euo pipefail
report_dir="$(realpath "$1")"
baseline=f9b31d3a43a6ee4143219a04c5a8031cb3173a97

python - "$report_dir/coverage.json" <<'PY' | tee "$report_dir/coverage-totals.txt"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    totals = json.load(f)["totals"]
for name, covered, total in (
    ("Statements", totals["covered_lines"], totals["num_statements"]),
    ("Branches", totals["covered_branches"], totals["num_branches"]),
):
    print(f"{name}: {covered}/{total} ({100 * covered / total:.2f}%)")
print(f"Combined: {totals['percent_covered']:.2f}%")
PY

git merge-base --is-ancestor "$baseline" HEAD
git diff --no-renames --no-ext-diff --unified=0 "$baseline" HEAD -- netbox_ops/ > "$report_dir/application.diff"
if [[ ! -s "$report_dir/application.diff" ]]; then
    echo 'Actual application diff: N/A (no package changes since initial baseline).' | tee "$report_dir/diff-experiment.txt"
else
    diff-cover "$report_dir/coverage.xml" --diff-file="$report_dir/application.diff" \
        --format "json:$report_dir/diff-coverage.json" \
        | tee "$report_dir/diff-experiment.txt"
fi

# Prove that the tool distinguishes covered and missed changed statements using
# a throwaway repository. No application code or GitHub repository is changed.
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
cd "$fixture"
git init -q
git config user.name 'Coverage fixture'
git config user.email 'coverage-fixture@example.invalid'
cat > sample.py <<'PY'
def covered():
    return 1


def missed():
    return 2
PY
printf 'from sample import covered\nassert covered() == 10\n' > exercise.py
git add sample.py exercise.py
git commit -qm baseline
base="$(git rev-parse HEAD)"
sed -i -e 's/return 1$/return 10/' -e 's/return 2$/return 20/' sample.py
git add sample.py
git commit -qm change
git diff --no-renames --unified=0 "$base" HEAD -- sample.py > fixture.diff
export COVERAGE_FILE="$fixture/.coverage"
python -m coverage run --source=. exercise.py
python -m coverage xml -o fixture.xml
diff-cover fixture.xml --diff-file=fixture.diff --fail-under=50 \
    --format "json:$report_dir/diff-fixture.json" | tee -a "$report_dir/diff-experiment.txt"
set +e
diff-cover fixture.xml --diff-file=fixture.diff --fail-under=51 > "$report_dir/diff-fixture-threshold.txt" 2>&1
status=$?
set -e
if [[ "$status" != 1 ]]; then
    echo "Expected threshold failure (exit 1); got $status" >&2
    exit 1
fi
echo 'Fixture: 50% passes; 51% fails as expected (two changed executable lines).' | tee -a "$report_dir/diff-experiment.txt"
