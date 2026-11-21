# Release Policy

netbox-ops uses a lightweight release process: develop on `dev`, publish stable versions from `main`.

## Versioning

Use Semantic Versioning (`MAJOR.MINOR.PATCH`) and tags such as `v0.4.1`.

- **PATCH**: compatible bug fixes.
- **MINOR**: new features; during `0.x`, also use a minor bump for breaking changes and explain them.
- **MAJOR**: breaking changes after reaching `1.0`.

The tag, `netbox_ops/__init__.py` version, and release notes must agree.

## Publish a release

1. Make sure the intended changes are in `dev` and CI passes.
2. Update `netbox_ops/__init__.py`, current-version documentation, and `CHANGELOG.md` in a PR to `dev`.
3. Open a PR from `dev` to `main`. Review the changes and merge using **Merge Commit**.
4. Tag the release commit on `main` and publish a GitHub Release with concise English notes:

   ```bash
   git fetch origin main --tags
   git switch main
   git pull --ff-only origin main
   git tag -a vX.Y.Z -m "netbox-ops vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. Check that the published version and source archive match the tag.

Before releasing changes to restore, upgrade, installation, or other destructive operations, validate the affected paths in a disposable environment. Where RHEL/Debian behavior differs, test the relevant platform. State known limitations openly; defer a stable release if a critical safety path has not been validated.

## Hotfixes

For an urgent bug in a released version:

1. Create a branch from `main`, fix the issue, and update the patch version and changelog.
2. Open a PR to `main`, run relevant tests, and merge.
3. Publish a new patch tag and GitHub Release.
4. Bring the fix back to `dev` via PR so it is not lost.

If a release causes problems, stop deployment and prepare a corrective patch. Do not assume reverting code also reverses database or backup changes.

## GitHub settings

Protect `main` and `dev` by requiring PRs and passing CI. Keep review requirements practical for a solo-maintained project. Branch protection must be configured in GitHub settings; this document does not enable it.
