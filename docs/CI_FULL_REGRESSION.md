# Full regression

The **Full regression** workflow reuses the standard tests (Python matrix, lint, packaging and PostgreSQL) and the three VM workflows. It does not publish a release, create a tag or replace any required PR or release checks. Self-upgrade uses unpublished candidates unless an official release pair is selected separately in its own workflow.

## Run manually

After the workflow reaches the default branch, open Actions > Full regression > Run workflow. Select the desired branch (dev for development, main for stable). The called workflows use the selected revision. The existing scope selector returns all suites for manual and scheduled events, not reduced PR coverage. Inspect all jobs and record the tested commit.

## Schedule

The initial schedule is Sunday at 03:37 UTC, weekly, on the default branch (main), not dev. GitHub schedules are best effort and may be delayed or disabled for inactive public repositories. A passing scheduled run on main does not validate dev or a later release candidate. Use manual dispatch for dev and retain the exact-revision release gates in RELEASING.md.

The five upgrade VM scenarios alone used about 65 minutes of aggregate VM time in PR #160 (https://github.com/teixemf/netbox-ops/actions/runs/45634453006), excluding setup and other jobs. Start weekly rather than nightly; review runner usage, failure history and overlap after representative runs. The full regression workflow is not triggered by every PR. Its concurrency group is separate from regular VM checks so a scheduled run cannot cancel an existing release validation.

## Validation

Verify the called workflows and the manual entry point on the chosen branch before relying on this as a full regression run. The schedule and manual entry point become available only once the workflow is present on the default branch. Existing uninstrumented E2E checks remain authoritative.
