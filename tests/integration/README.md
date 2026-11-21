# PostgreSQL integration tests

Run only in **disposable containers**. The bootstrap installs the distribution's PostgreSQL packages, creates a temporary SQL_ASCII cluster, and checks a real UTF8 dump/restore cycle. PostgreSQL runs over a private Unix socket without TCP access. The temporary cluster is stopped and removed at the end, including when a check fails.

From the repository root, with Docker installed:

```bash
docker run --rm -v "$PWD:/workspace:ro" -w /workspace ubuntu:22.04 \
  bash tests/integration/run_postgres.sh

docker run --rm -v "$PWD:/workspace:ro" -w /workspace rockylinux:9 \
  bash tests/integration/run_postgres.sh
```

To execute the real backup integration tests, append this command to either Docker invocation:

```bash
python3 -m unittest discover -s tests/integration -p 'integration_*.py' -v
```

The GitHub Actions smoke workflow runs it in both containers. The suite creates disposable
databases and media directories and uses real `pg_dump`/`pg_restore` for backups and data
verification. Only systemd, audit output and the NetBox version are mocked. No NetBox
installation or credentials are required.

Integration files use the `integration_*.py` pattern so the existing `test_*.py` unit
test job remains independent. The smoke check deliberately uses PostgreSQL's SQL_ASCII `template0` and a UTF8 application database, matching the regression scenario in issue #24. No NetBox installation, systemd, external services, or credentials are required.
