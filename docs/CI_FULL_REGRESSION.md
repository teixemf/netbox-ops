# Full regression

The **Full regression** workflow reuses the standard tests (Python matrix, lint, packaging and PostgreSQL) and the three VM workflows. It does not publish a release, create a tag or replace any required PR or release checks. Self-upgrade uses unpublished candidates unless an official release pair is selected separately in its own workflow.

## Run manually

After the workflow reaches the default branch, open Actions > Full regression > Run workflow. Select the desired branch (dev for development, main for stable). The called workflows use the selected revision. The existing scope selector returns all suites for manual and scheduled events, not reduced PR coverage. Inspect all jobs and record the tested commit.

## Schedule

The initial schedule is Sunday at 03:37 UTC, weekly, on the default branch (main), not dev. GitHub schedules are best effort and may be delayed or disabled for inactive public repositories. A passing scheduled run on main does not validate dev or a later release candidate. Use manual dispatch for dev and retain the exact-revision release gates in RELEASING.md.

The five upgrade VM scenarios alone used about 65 minutes of aggregate VM time in PR #160 (https://github.com/teixemf/netbox-ops/actions/runs/45634453006), excluding setup and other jobs. Start weekly rather than nightly; review runner usage, failure history and overlap after representative runs. The full regression workflow is not triggered by every PR. Its concurrency group is separate from regular VM checks so a scheduled run cannot cancel an existing release validation.

## Validation

Verify the called workflows and the manual entry point on the chosen branch before relying on this as a full regression run. The schedule and manual entry point become available only once the workflow is present on the default branch. Existing uninstrumented E2E checks remain authoritative.

## Cross-minor source baseline

The source fixture is NetBox 4.4.9 with the pinned cross-minor plugin and LDAP dependencies, a migrated PostgreSQL schema and a disabled, password-free database role. Each disposable overlay receives independent credentials and the current netbox-ops checkout. The remaining official 4.4.9 source setup operations (trace paths, documentation, static files, stale content types, lazy reindex and expired sessions) run in the overlay using its credentials. The real 4.7.3 upgrade, approval, ltree and recovery checks are unchanged.

Use **Validate NetBox 4.4.9 source baseline** to compare fresh and pre-migrated source fixtures. It builds a temporary artifact, verifies its checksum and runs both real upgrades. The existing 4.7.3 release baseline remains unchanged.

After the validation PR is integrated into dev, manually select dev and 4.4.9 in **Publish reusable NetBox VM baseline**. Publication requires the explicit `publish_source` option and both verification jobs to pass. This publishes only the versioned CI image, not a netbox-ops release; it never overwrites an existing version. The regular cross-minor job uses the published `netbox-ubuntu2404-netbox449-schema-v1` release, verifies its checksum and upgrades from 4.4.9 to 4.7.3. Other scenarios retain the 4.7.3 baseline. Full regression adds `cross-minor-fresh`, which retains the original fresh 4.4.10 source installation from the 4.7.3 baseline. Both cross-minor jobs execute the same upgrade assertions.
