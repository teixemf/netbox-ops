# Release Policy

This document describes how netbox-ops changes move from the integration branch (`dev`) to the published branch (`main`).

## Branch and release rules

- `dev` accepts reviewed development PRs, normally using **Squash Merge**.
- `main` represents published stable code. Only a reviewed **release PR from `dev`** or an exceptional **hotfix PR** may change it.
- Release PRs from `dev` into `main` use **Merge Commit**, never Squash Merge. This preserves the integration boundary.
- Both branches require PRs, passing required checks, and no direct pushes or force-pushes.
- A successful merge into `dev` does **not** automatically publish a release.

## Version numbers

Use [Semantic Versioning](https://semver.org/) and tag stable releases `vMAJOR.MINOR.PATCH`, matching the runtime version displayed by `netbox-ops --version`.

- **PATCH**: backward-compatible bug fixes or internal corrections, e.g. `0.4.0 -> 0.4.1`.
- **MINOR**: new backward-compatible functionality, e.g. `0.4.1 -> 0.5.0`.
- **MAJOR**: incompatible changes when the project has a stable `1.x` API/contract.
- During `0.x` development, breaking changes may require a **MINOR** bump; document them explicitly and never conceal them in a PATCH release.
- Pre-releases, when needed, use a SemVer suffix such as `v0.5.0-rc.1` and must be marked as GitHub pre-releases.

Security, migration, or rollback impact must be described regardless of version size. Avoid publishing a tag for an unvalidated commit.

## Release prerequisites

A maintainer must verify:

1. The intended Issues are accepted in `dev` and assigned to the target release milestone; known blockers are resolved or documented.
2. The latest `dev` CI is green, and the release PR also passes required checks.
3. Python 3.12 automated tests pass in a disposable environment:
   ```bash
   python -m unittest discover -s tests -p 'test_*.py' -v
   ```
4. Changes affecting backups, restores, destructive actions, installation, upgrades, systemd, or platform support have been validated on representative **non-production** RHEL and/or Debian VMs, as applicable.
5. Backup/restore compatibility, confirmation safeguards, failure handling, permissions, and rollback behavior have been reviewed when affected.
6. No secrets or customer data appear in source code, logs, examples, artifacts, or release notes.
7. All documentation and release notes are in **English**; PT-PT and EN application messages are synchronized if affected.

A **stable release must not be described as production-validated** when required safety-critical validation has not been completed. Use a pre-release or defer publication, and document all test limitations.

## Prepare a release on dev

1. Open a release-preparation Issue (or use an existing tracked release milestone).
2. Create a short-lived branch from `dev` and open a PR back to `dev` for version and release metadata.
3. Update `netbox_ops/__init__.py` (`__version__`) to the chosen release version.
4. Update the current-version references in `README.md` and any other active installation/user documentation. Preserve historical release references where they describe past behavior.
5. Update `CHANGELOG.md` in English with user-facing changes, fixes, breaking changes, migrations, and known limitations. The current changelog format may be kept until a separate migration is agreed.
6. Verify CLI version output using a safe non-destructive command, e.g. `python netbox_ops.py --version`.
7. Review the PR, run CI, and Squash Merge into `dev`.

Keep unrelated work out of `dev` while the release candidate is under final validation, or restart validation after any new merge.

## Publish a stable release

1. Open a PR with **head `dev`, base `main`**, titled `chore(release): vX.Y.Z`.
2. Include the target milestone, changelog summary, validation evidence, compatibility notes, known limitations, and rollback notes.
3. Confirm that every commit in the PR is intended for release and that no unreviewed change is included. Re-run required checks and complete the release review.
4. Use **Merge Commit** to merge `dev` into `main`.
5. Verify that `main` contains the intended version and tests still pass; tag **the exact release commit on `main`** with an annotated tag:
   ```bash
   git fetch origin main --tags
   git switch main
   git pull --ff-only origin main
   git tag -a vX.Y.Z -m "netbox-ops vX.Y.Z"
   git push origin vX.Y.Z
   ```
6. Create a **GitHub Release** from that exact tag, with English release notes covering changes, compatibility, security considerations, required actions, and validation status. Mark pre-release tags as pre-releases.
7. Verify that published source archives/artifacts correspond to the tag and, where applicable, smoke-test installation in a disposable VM. Never test a release against production services.
8. Close the release milestone after confirming its scope and record any deferred Issues in a future milestone.

Do not move or overwrite published release tags. Fix subsequent problems in a new version.

## Emergency hotfixes

Use this path only for a high-priority regression or security issue in the released version:

1. Record an Issue privately for sensitive vulnerabilities or publicly for ordinary bugs.
2. Branch **from `main`** using `hotfix/<issue-number>-<description>`.
3. Make the smallest safe fix, add a regression test, **bump `__version__` to the next patch version**, update current-version documentation and `CHANGELOG.md`, and open a PR **to `main`** with explicit risk/rollback notes.
4. Require CI and review (or documented solo self-review). Do not bypass safety checks merely because the issue is urgent.
5. Merge via PR, publish an incremented patch version/tag/GitHub Release, and document the impact.
6. **Forward-port the fix into `dev` through a separate PR** (merge `main` back to `dev` or cherry-pick the hotfix onto a branch based on `dev`); run CI and avoid duplicate changes.
7. Reconcile runtime version and changelog on `dev` as necessary to prevent a later release from dropping the hotfix.

Do not merge unrelated unfinished changes from `dev` into `main` during a hotfix.

## Rollback and incident handling

- Stop deployment on a failed validation or unexpected production symptom.
- Identify the affected release commit, tag, configuration changes, and data migrations.
- Prefer a new corrective PR and PATCH release. Revert a release commit only after evaluating whether its dependencies, data format, or database changes make reverting unsafe.
- Never equate a Git revert with a safe database restore. Restore only from verified, compatible backups with an approved plan and a tested recovery path.
- Document the incident, regression test, and corrective Issue/PR.

## Repository settings checklist

These items require GitHub repository settings changes rather than a documentation commit:

- Protect `main` and `dev` from direct pushes, force pushes, and deletions.
- Require PRs and passing CI checks for both branches; enable these checks **after the CI workflow from PR #2 is merged** and status names are known.
- Require conversations resolved; configure review requirements appropriate to available maintainers.
- Enable Squash Merge for development PRs and Merge Commit for release PRs.
- Configure release permissions and verify who may publish Git tags/releases.

The repository's current settings must be verified independently; this document specifies the desired policy, not an assertion that enforcement is already active.

