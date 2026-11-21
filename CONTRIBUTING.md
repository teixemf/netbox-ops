# Contributing to netbox-ops

Thanks for helping improve netbox-ops. The project manages privileged NetBox operations; changes must be traceable, reviewable, and safe.

This document defines the contribution workflow. See [Release Policy](docs/RELEASING.md) for release and hotfix procedures.

## Principles

- Make small, focused, reversible changes.
- Preserve safe defaults and require explicit confirmation for destructive operations.
- Use GitHub Issues for planning, Pull Requests (PRs) for changes, and CI for verification.
- Write repository documentation, issue descriptions, PR descriptions, code comments, and release notes in **English**. Application localization files and user-facing translated messages may contain supported languages (currently PT-PT and EN).
- Never develop, test, or troubleshoot destructive operations against production data or production NetBox services.

## Branch model

| Branch | Purpose | Changes |
| --- | --- | --- |
| `main` | Stable, published code | Release PRs from `dev`, or exceptional hotfix PRs from `main` |
| `dev` | Integration of completed work | Reviewed PRs from short-lived branches |
| Short-lived branches | Individual features, fixes, refactors, tests, documentation | Branch off `dev` and submit a PR to `dev` |

Both `main` and `dev` must be protected. Do not push directly, force-push, or delete these branches. Do not merge an incomplete PR just to unblock other work.

Use `<type>/<issue-number>-<short-description>`, for example:

- `feat/42-plugin-management`
- `fix/51-backup-validation`
- `refactor/63-service-manager`
- `test/72-restore-coverage`
- `docs/81-contribution-guide`
- `chore/90-dependency-update`

Allowed types include `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, and `ci`. Emergency hotfixes use `hotfix/<issue-number>-<description>` and branch from `main`.

## Issues and planning

Create an Issue before starting **non-trivial** changes: features, bug fixes, refactors, security-relevant changes, and changes to backup, restore, installation, upgrades, or services. Trivial spelling/formatting-only updates may omit an Issue but still require a PR.

An Issue must explain the motivation, scope, acceptance criteria, and relevant risks. Small tasks should be independently verifiable. One Issue can lead to multiple PRs, but keep unrelated work separate. Use milestones to indicate the intended release.

Do not post secrets, credentials, backup contents, or details of exploitable vulnerabilities in public Issues. Contact the maintainer privately to coordinate security reports; use GitHub's private vulnerability reporting if available.

An Issue is **done** once its acceptance criteria have been verified and the implementation has been integrated into `dev`. Maintainers may close it manually at that point. Release publication is tracked separately through the milestone and release PR.

## Workflow

1. Choose or create an Issue (unless the change is trivial).
2. Agree on scope and acceptance criteria before large changes.
3. Create a short-lived branch from the current `dev`.
4. Implement the smallest coherent change; add or update tests and translations when needed.
5. Run tests in a disposable development environment; never against production.
6. Open a **draft PR to `dev`** early for substantial changes. Link the Issue with `Refs #123`; do not rely on GitHub's automatic closing keywords for PRs targeting `dev`.
7. Complete the PR template, self-review the diff, and address any review feedback.
8. Mark the PR ready for review when CI passes, acceptance criteria are satisfied, and risks are documented.
9. Use **Squash Merge** into `dev`; remove the short-lived branch after merging.
10. Close the Issue once the work is accepted in `dev`, and keep the intended release milestone accurate.

Do not rebase or force-push shared branches without coordinating with their collaborators.

## Commits and PRs

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit and PR titles:

```text
feat(backups): add archive validation
fix(services): handle missing systemd units
refactor(tui): separate rendering and actions
docs: document release checklist
test(restores): cover version mismatch safeguards
chore: update development tooling
```

A PR should have one primary purpose, a clear summary, relevant Issue references, verification results, risk/rollback notes, and a completed checklist. Avoid mixing behavior changes with mechanical refactoring or unrelated formatting.

For normal contributions, target `dev` and **Squash Merge**. A release PR targets `main` from `dev` and uses **Merge Commit** so release integration is preserved. Hotfixes follow the separate release procedure.

## Required checks and reviews

Before merging a normal PR:

- The diff has been reviewed by another maintainer when one is available; solo maintenance requires an explicit self-review and completed PR checklist.
- The required CI checks pass for the PR's latest commit.
- New or changed behavior has regression coverage; any testing gap is disclosed in the PR.
- Installation and permissions remain scoped to netbox-ops, with no unintended modifications to NetBox, system packages, or service configuration.
- CLI/TUI wording, safety prompts, and supported PT-PT/EN translations remain consistent.
- User-visible changes and breaking changes are reflected in relevant English documentation and the release notes/changelog.

The project uses **Python 3.12** and the standard-library `unittest` test suite:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Some tests inspect root-owned paths and permissions; run the complete suite only in a **disposable container or VM with appropriate isolation**, never on a production server. The CI workflow in PR #2 runs the tests in a disposable Python 3.12 container. Configure required branch checks after that workflow has been merged and its check name is stable.

### Risk-based validation

| Change | Minimum verification |
| --- | --- |
| Documentation only | PR review; optional documentation checks |
| TUI, CLI, localization | Automated tests for affected paths |
| Configuration, systemd services | Automated tests with simulated environments and failure paths |
| Backup, restore, file permissions | Automated tests plus non-production VM validation of affected operations |
| Installer, upgrade, distribution support | Non-production RHEL and Debian validation of affected paths |
| Destructive or security-sensitive behavior | Failure/rollback tests, explicit safeguards, and non-production end-to-end validation |

If required validation has not been performed, do not claim that the affected functionality is production-validated. Record the limitation prominently and block a stable release when the missing test is safety-critical.

## Repository configuration for maintainers

Enable branch protection or repository rulesets for **both `main` and `dev`**:

- Require a Pull Request before merging; disallow direct pushes, force pushes, and branch deletion.
- Require the automated test check once the CI workflow from PR #2 is present on the target branches.
- Require resolved review conversations and an up-to-date PR branch when appropriate.
- Require at least one external review **only when an independent reviewer is available**; otherwise require a documented self-review. GitHub cannot count author self-approval as an approval.
- Prevent bypasses by default; allow emergency exceptions only when separately authorized and recorded.

Apply these settings in GitHub repository settings. **Documenting these rules does not itself enable GitHub branch protection.**

## Releases and emergency fixes

Only `main` is released. Stable releases follow Semantic Versioning and are tagged `vX.Y.Z`; do not publish feature branches or `dev` as stable releases. See [docs/RELEASING.md](docs/RELEASING.md) for release gates, tagging, GitHub Releases, hotfix forward-porting, and rollback.

