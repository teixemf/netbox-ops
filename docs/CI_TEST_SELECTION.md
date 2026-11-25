# CI test selection

The CI selector maps changed paths to the existing unit, PostgreSQL, packaging and three VM suites. It runs the entire selected unittest matrix, not individual tests. It logs each changed path and the mapped suites, then the selected and skipped suites. Unknown paths, shared critical modules, main, manual/scheduled events and invalid comparison data use the full scope.

Known upgrade and self-upgrade modules are listed explicitly. Review dependencies when adding a module or changing its responsibility; a familiar filename prefix alone is not evidence of safe reduced testing. The translation runtime and English catalog affect the CLI used by all VM suites. Portuguese catalog changes retain unit coverage. Keep the release gates independent of this selector.

## Efficiency evidence

PR #161's uninstrumented Python 3.12 suite ran 805 tests in 19.009s ([run](https://github.com/teixemf/netbox-ops/actions/runs/45640747157)); PR #160's instrumented suite ran 826 tests in 22.326s ([run](https://github.com/teixemf/netbox-ops/actions/runs/45653452912)). These are different test sets and runners, not a controlled overhead benchmark. The five upgrade VM boots in #160 took a combined 65m18s, excluding setup. Fine-grained unit selection has little demonstrated value compared with avoiding irrelevant VM jobs. Do not reduce the test matrix on this evidence.

After integration, compare representative docs-only, translation-only, upgrade, backup and shared-module PRs with equivalent historical runs. Record actual job execution and runner duration, not just successful check counts; skipped jobs can also report success. The full weekly/manual workflow provides additional regression evidence, not a reason to weaken release validation.
