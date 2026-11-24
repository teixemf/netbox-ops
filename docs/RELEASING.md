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
4. Open **Actions → Build Release → Run workflow**, select `main`, and enter the planned stable tag (for example, `v0.6.0`). Leave **Publish after validation** unchecked to build and inspect a candidate without publishing.
5. When ready to publish, run the same workflow on `main` with **Publish after validation** checked. The required CI tests and candidate validation must pass on the selected commit first. The publication job uses the artifacts from that run, creates the annotated tag and a draft GitHub Release, uploads the archive, SHA-256 and compatibility manifest, verifies their digests, and publishes the draft. The generated release notes become the release description.
6. Check the workflow result and the published release. Published release-pair testing is tracked in #132.

The workflow does not publish automatically on a merge. Publication runs are serialized across versions. If publication is interrupted, rerun it with the same tag and unchanged `main` commit: a matching annotated tag and draft can be reused, and only missing assets are uploaded. An already published matching release is verified without modification.

If a tag points to another commit, an existing asset differs, a release is incomplete but already public, or the release notes differ, the workflow stops without replacing anything. Review the conflict manually. GitHub may leave an incomplete upload entry after a server error; inspect it before retrying. If `main` has advanced, do not move the tag or rebuild an existing release from different code.

The job uses the repository's GitHub Actions token with contents-write permission only for publication. Repository rules must permit that token to create release tags. If the rules block it, publication fails rather than bypassing them.

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
