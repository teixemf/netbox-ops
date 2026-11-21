# PostgreSQL integration tests

Run only in **disposable containers**. The bootstrap installs the distribution's PostgreSQL packages, creates a temporary SQL_ASCII cluster, and checks a real UTF8 dump/restore cycle. PostgreSQL runs over a private Unix socket without TCP access. The temporary cluster is stopped and removed at the end, including when a check fails.

From the repository root, with Docker installed:

```bash
docker run --rm -v "$PWD:/workspace:ro" -w /workspace ubuntu:22.04 \
  bash tests/integration/run_postgres.sh

docker run --rm -v "$PWD:/workspace:ro" -w /workspace rockylinux:9 \
  bash tests/integration/run_postgres.sh
```

The same script will later accept the integration unittest command as arguments. The smoke check deliberately uses PostgreSQL's SQL_ASCII `template0` and a UTF8 application database, matching the regression scenario in issue #24. No NetBox installation, systemd, external services, or credentials are required.
