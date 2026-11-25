# Contributing to netbox-ops

netbox-ops is a small, early-stage project. Keep contributions simple, focused, and safe.

## Branches

- `main`: stable releases.
- `dev`: ongoing development and integration.
- Create a short-lived branch from `dev` for normal changes, then open a Pull Request (PR) to `dev`.
- Use `dev` -> `main` PRs for releases. Urgent fixes to released versions may branch from `main` and must also be brought back to `dev`.
- Avoid direct pushes to `main` and `dev`. Prefer **Squash Merge** for development PRs and **Merge Commit** for release PRs.

Branch names can be simple: `feat/backup-status`, `fix/service-detection`, or `docs/contributing`. Include an Issue number when there is one.

## Issues and Pull Requests

Create an Issue when a bug, feature, or larger task needs discussion or tracking. **An Issue is not required for every PR**, especially for small fixes, tests, and documentation.

Keep PRs small. Describe what changed, why, and how it was tested. Link related Issues when applicable. A self-review is sufficient when working alone; ask for another review when available or when a change is particularly risky.

Use clear commit messages, preferably in Conventional Commits style (for example, `fix(backups): validate archive path`). This is a convention, not a merge blocker.

## Testing and safety

Run the relevant tests before merging. The standard test command is:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Run Ruff to check basic Python code quality:

```bash
python -m pip install ruff==0.17.6
ruff check .
```

Run the complete suite in an isolated development environment when elevated permissions are required. CI should pass before merging. See the [CI coverage policy](docs/CI_COVERAGE_POLICY.md) for coverage reporting and review.

Changes involving backups, restores, upgrades, installation, permissions, or services deserve extra care. Test destructive operations only in disposable, non-production environments. Clearly document any important testing limitation; do not claim untested restore or upgrade paths are production-ready.

## Documentation and translations

Write repository documentation, PR descriptions, and release notes in **English**. Keep supported PT-PT and EN application messages aligned when changing user-facing text.

See [Release Policy](docs/RELEASING.md) for the lightweight release process.
