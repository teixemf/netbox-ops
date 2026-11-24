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
4. Run **Actions → Build Release → Run workflow** on `main` and enter the planned stable tag (for example, `v0.6.0`). The workflow runs the required CI tests on the selected commit, then builds and validates the archive, SHA-256, compatibility manifest and release notes. It uploads those four files as a run artifact. It does not create a tag or publish anything.
5. Review the validated candidate and confirm the tested commit is the intended release commit. Until automated publishing is implemented (#74), tag that exact commit and publish manually:

   ```bash
   git fetch origin main --tags
   git tag -a vX.Y.Z <validated-commit-sha> -m "netbox-ops vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. In GitHub **Releases**, create a release using the existing tag, use the generated `.notes.md` content for the release description, and upload the three validated assets:
   - `netbox-ops-vX.Y.Z.tar.gz`
   - `netbox-ops-vX.Y.Z.tar.gz.sha256`
   - `netbox-ops-vX.Y.Z.compatibility.json`

   Do not substitute GitHub's automatic source archives. Do not rebuild or modify the validated assets during publication.
7. Verify the published assets. Published release-pair testing is tracked in #132.

## Build a candidate locally

Use Python 3.12 or newer, Git, tar and gzip:

```bash
bash scripts/build-release.sh
python3 scripts/release-candidate.py validate dist/netbox-ops-vX.Y.Z.tar.gz vX.Y.Z
```

The build reads the version, changelog and compatibility requirements from the
committed package. It rejects version mismatches, missing release notes, unsafe
archive entries and non-executable installer scripts. The compatibility policy
is committed in `netbox_ops/release_requirements.json`; review it whenever the
supported environment or mandatory upgrade path changes. The manifest is
encoded and validated with the self-upgrade consumer's schema.

For a tagged revision containing the release requirements file, the same
builder accepts the existing tag as its argument. Historical packages without
the compatibility policy cannot be rebuilt with the new validator; their
original published assets remain unchanged.

The pre-release VM tests (#79) use these same build artifacts without creating
tags or publishing releases.

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
