# CI coverage policy

## Scope

Keep the existing conservative CI suite selection (scripts/ci/test_scope.py). Documentation-only changes do not start heavy suites. Unknown or shared changes run all relevant suites. Never skip existing release validation based on coverage. The full unittest suite took about 23 seconds under instrumentation; finer unit-test selection is not justified without evidence of savings.

## Pull requests

Collect statement and branch coverage in the existing Python 3.12 matrix job when unit tests are selected. Do not run a second unittest suite or add a new required check. Other Python versions retain the normal tests. Report changed executable lines against the actual PR merge checkout. Reject missing coverage data or an inconsistent base; distinguish N/A from 100%. Renames count conservatively as new files. The reporter measures executable lines identified by coverage.py, not changed branch outcomes, shell scripts, or uninstrumented processes; edits only to continuation lines may need manual review.

Coverage percentages start in reporting mode. Review uncovered changed statements and failure scenarios in every relevant PR. Do not treat a percentage as proof of correctness. New critical operations should have explicit success, rejection, interruption and recovery tests as applicable. Do not remove assertions or coverage exclusions to improve a number.

## Baseline and thresholds

See [initial measurements](COVERAGE_BASELINE.md). The observed Python 3.12 unit reference is 83.53% statements, 73.44% branches, 80.60% combined (research branch). Re-measure on the integrated revision before setting numeric requirements. Compare complete runs using the same interpreter, tool and source scope. Track statements and branches separately. Investigate nondeterministic coverage differences; do not enforce a rounded global percentage as a substitute for reviewed changes. Propose a no-regression floor and changed-line minimum after collecting representative PR data and reviewing critical paths with the maintainer. Until then, no automatic percentage gate.

## Integration and VM tests

Keep the existing PostgreSQL and VM checks and release gates unmodified. Coverage experiments on these suites use separate reports; do not sum percentages or compare unlike interpreters. Do not add an extra VM execution for every PR. Instrumented VM tests are diagnostic, not a substitute for uninstrumented operational tests. Self-upgrade coverage requires a separate design because the installation is replaced and integrity is checked. Periodic full regression and a manual override should use existing workflows where possible; schedule and validate them separately before enabling recurring resource use.

