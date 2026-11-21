#!/usr/bin/env bash
# Disposable PostgreSQL environment for netbox-ops integration tests.
# Run only inside a fresh Ubuntu 22.04 or Rocky Linux 9 container.
set -Eeuo pipefail

if (( EUID != 0 )); then
  echo 'ERROR: Run as root inside a disposable container.' >&2
  exit 1
fi
if [[ ! -f /.dockerenv && ! -f /run/.containerenv ]]; then
  echo 'ERROR: Refusing to install packages outside a container.' >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04)
    echo 'Installing distribution PostgreSQL packages on Ubuntu 22.04...'
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends postgresql postgresql-client util-linux python3
    ;;
  rocky:9|rocky:9.*)
    echo 'Installing distribution PostgreSQL packages on Rocky Linux 9...'
    dnf install -y --setopt=install_weak_deps=False postgresql postgresql-server util-linux python3
    ;;
  *)
    echo "ERROR: Unsupported container: ${ID:-unknown} ${VERSION_ID:-unknown}" >&2
    exit 1
    ;;
esac

for executable in runuser psql pg_dump pg_restore createdb dropdb; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    echo "ERROR: PostgreSQL tool missing: $executable" >&2
    exit 1
  fi
done

# Ubuntu keeps server binaries under /usr/lib/postgresql/<major>/bin;
# Rocky's distribution packages normally install them under /usr/bin.
pg_bin_dir=''
for candidate in /usr/lib/postgresql/*/bin/initdb /usr/bin/initdb /usr/pgsql-*/bin/initdb; do
  if [[ -x "$candidate" && -x "$(dirname "$candidate")/pg_ctl" ]]; then
    pg_bin_dir=$(dirname "$candidate")
    break
  fi
done
if [[ -z "$pg_bin_dir" ]]; then
  echo 'ERROR: Cannot find matching initdb and pg_ctl binaries.' >&2
  exit 1
fi

pg_as_postgres() { runuser -u postgres -- "$@"; }
workdir=$(mktemp -d /tmp/nbo-pg-integration.XXXXXXXX)
pg_data="$workdir/data"
pg_ctl="$pg_bin_dir/pg_ctl"

cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )) && [[ -f "$workdir/server.log" ]]; then
    echo 'PostgreSQL server log (last 40 lines):' >&2
    tail -n 40 "$workdir/server.log" >&2
  fi
  if [[ -d "$pg_data" ]]; then
    pg_as_postgres "$pg_ctl" -D "$pg_data" -m fast -w stop >/dev/null 2>&1 || true
  fi
  if [[ "$workdir" == /tmp/nbo-pg-integration.* ]]; then
    rm -rf -- "$workdir"
  fi
  exit "$result"
}
trap cleanup EXIT

chown postgres:postgres "$workdir"
mkdir "$workdir/socket"
chown postgres:postgres "$workdir/socket"

# Reproduce #24 deliberately: template0 is SQL_ASCII, while the test DB is UTF8.
# No TCP listener, no password, and no access outside this temporary container.
pg_as_postgres "$pg_bin_dir/initdb" -D "$pg_data" \
  --encoding=SQL_ASCII --locale=C --auth-local=peer --auth-host=reject
cat >> "$pg_data/postgresql.conf" <<CONFIG
listen_addresses = ''
unix_socket_directories = '$workdir/socket'
port = 55432
CONFIG

export PGHOST="$workdir/socket"
export PGPORT=55432
pg_as_postgres "$pg_ctl" -D "$pg_data" -l "$workdir/server.log" -w start

echo "PostgreSQL: $(pg_as_postgres psql --version)"
echo "Server binary: $pg_bin_dir"

pg_as_postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c 'CREATE ROLE ci_pg_owner LOGIN' >/dev/null
pg_as_postgres createdb --owner=ci_pg_owner --template=template0 \
  --encoding=UTF8 --lc-collate=C --lc-ctype=C ci_pg_smoke
pg_as_postgres psql -X -v ON_ERROR_STOP=1 -d ci_pg_smoke -c \
  "CREATE TABLE public.django_migrations (id bigint PRIMARY KEY, app text, name text);
   CREATE TABLE public.ci_marker (id integer PRIMARY KEY, marker text NOT NULL);
   INSERT INTO public.ci_marker VALUES (1, 'A');" >/dev/null

pg_as_postgres pg_dump --format=custom --no-owner --no-acl \
  --dbname=ci_pg_smoke --file="$workdir/smoke.dump"
if [[ "$(head -c 5 "$workdir/smoke.dump")" != PGDMP ]]; then
  echo 'ERROR: pg_dump did not produce a custom-format dump.' >&2
  exit 1
fi
pg_as_postgres pg_restore --list "$workdir/smoke.dump" >/dev/null

pg_as_postgres createdb --owner=ci_pg_owner --template=template0 \
  --encoding=UTF8 --lc-collate=C --lc-ctype=C ci_pg_stage
pg_as_postgres pg_restore --no-owner --no-acl --role=ci_pg_owner \
  --exit-on-error --single-transaction --dbname=ci_pg_stage "$workdir/smoke.dump"

encodings=$(pg_as_postgres psql -X -A -t -v ON_ERROR_STOP=1 -d postgres -c \
  "SELECT string_agg(datname || ':' || pg_encoding_to_char(encoding), ',' ORDER BY datname)
   FROM pg_database WHERE datname IN ('template0', 'ci_pg_smoke', 'ci_pg_stage');")
if [[ "$encodings" != 'ci_pg_smoke:UTF8,ci_pg_stage:UTF8,template0:SQL_ASCII' ]]; then
  echo "ERROR: Unexpected database encodings: $encodings" >&2
  exit 1
fi
marker=$(pg_as_postgres psql -X -A -t -v ON_ERROR_STOP=1 -d ci_pg_stage -c \
  'SELECT marker FROM public.ci_marker WHERE id = 1')
if [[ "$marker" != 'A' ]]; then
  echo "ERROR: Restored marker does not match: $marker" >&2
  exit 1
fi
pg_as_postgres psql -X -A -t -v ON_ERROR_STOP=1 -d ci_pg_stage -c \
  "SELECT to_regclass('public.django_migrations') IS NOT NULL" | grep -qx t

pg_as_postgres dropdb --if-exists --force ci_pg_stage
pg_as_postgres dropdb --if-exists --force ci_pg_smoke
pg_as_postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c 'DROP ROLE ci_pg_owner' >/dev/null
echo 'PASS: real pg_dump/pg_restore, staging, UTF8/SQL_ASCII, and cleanup.'

# Later stages may pass a Python unittest command, using this same local cluster.
if (( $# > 0 )); then
  export NBO_CI_PG_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1
  "$@"
fi
