# PostgreSQL integration tests

Run only inside disposable Ubuntu 22.04 and Rocky Linux 9 containers. The setup installs distribution PostgreSQL packages and runs an isolated cluster over a private Unix socket, without TCP access, real credentials or external services.

The bootstrap checks a real dump and restore using `SQL_ASCII` template0 and UTF8 databases, reproducing issue #24. The tests then exercise real PostgreSQL backup, restore, staging and cleanup with temporary media.

Only systemd, the Django migration check, audit output and the NetBox version are simulated. Full NetBox service, HTTP, authentication and plugin checks belong in E2E tests.

## Run the full integration suite locally

From the repository root on a disposable development machine with Docker:

```bash
docker run --rm -v "$PWD:/workspace:ro" -w /workspace ubuntu:22.04 \
  bash tests/integration/run_postgres.sh \
    python3 -m unittest discover -s tests/integration -p 'integration_*.py' -v

docker run --rm -v "$PWD:/workspace:ro" -w /workspace rockylinux:9 \
  bash tests/integration/run_postgres.sh \
    python3 -m unittest discover -s tests/integration -p 'integration_*.py' -v
```

The `integration` job in `.github/workflows/tests.yml` runs these commands on standard GitHub-hosted runners for pushes and pull requests to `dev` and `main`. Integration tests use `integration_*.py`, while the offline unit suite uses `test_*.py`.

The temporary PostgreSQL cluster is stopped and removed at the end. Never run the bootstrap on a production host.
