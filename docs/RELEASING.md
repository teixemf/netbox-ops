# Release Policy

netbox-ops uses a lightweight release process: develop on `dev`, publish stable versions from `main`.

## Versioning

Use Semantic Versioning (`MAJOR.MINOR.PATCH`) and tags such as `v0.5.0`.

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

5. Build the Linux installation archive from the release tag (requires Git, tar and gzip):

   ```bash
   bash scripts/build-release.sh vX.Y.Z
   tar -tzf dist/netbox-ops-vX.Y.Z.tar.gz
   ```

   The package contains `netbox-ops/` with the application, installer and configuration example. It does not include development tests, CI files or local runtime data. The build script checks that the tag matches the committed package version.
6. In GitHub **Releases**, create a release using the existing tag, add concise English notes from the changelog, and **upload `dist/netbox-ops-vX.Y.Z.tar.gz` as a release asset**. GitHub's automatic source archives are not a replacement for the installation asset.
7. Download the published asset and verify that extraction creates `netbox-ops/install.sh` with executable permissions, and that the packaged version matches the tag.

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
