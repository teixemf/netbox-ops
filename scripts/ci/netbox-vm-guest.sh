#!/usr/bin/env bash
# Prepare an Ubuntu test guest, or provision a fresh NetBox instance for a disposable boot.
set -euo pipefail

mode=$1
netbox_version=${2:-4.7.3}
case "$netbox_version" in 4.7.3|4.4.9) ;; *) exit 2 ;; esac
export DEBIAN_FRONTEND=noninteractive

case "$mode" in
  prepare)
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      postgresql postgresql-client redis-server nginx \
      python3.12-venv python3-dev build-essential \
      libxml2-dev libxslt1-dev libffi-dev libpq-dev libssl-dev zlib1g-dev \
      curl ca-certificates openssl
    systemctl enable --now postgresql redis-server nginx

    # Pinned NetBox source release. No production credentials enter this baseline.
    if [[ "$netbox_version" == 4.4.9 ]]; then
      NEEDRESTART_MODE=l apt-get install -y --no-install-recommends git libldap-dev libsasl2-dev python3.12-dev
      git clone --quiet --depth 1 --branch v4.4.9 https://github.com/netbox-community/netbox.git /opt/netbox
      test "$(git -C /opt/netbox describe --tags --exact-match)" = v4.4.9
      printf '%s\n' 'netbox-reorder-rack==1.1.4' 'tomli-w==1.2.0' 'django-auth-ldap==5.4.0' > /opt/netbox/local_requirements.txt
    else
      install -d -m 0755 /opt/netbox
      curl -fLsS --retry 3 \
        -o /tmp/netbox-source.tar.gz \
        "https://github.com/netbox-community/netbox/archive/refs/tags/v$netbox_version.tar.gz"
      tar -xzf /tmp/netbox-source.tar.gz -C /opt/netbox --strip-components=1
      rm -f /tmp/netbox-source.tar.gz
    fi

    adduser --system --group --quiet netbox
    for dir in media reports scripts; do
      install -d -m 0755 -o netbox -g netbox "/opt/netbox/netbox/$dir"
      chown -R netbox:netbox "/opt/netbox/netbox/$dir"
    done
    install -m 0644 /opt/netbox/contrib/gunicorn.py /opt/netbox/gunicorn.py
    sed -i 's/^workers = 5$/workers = 2/' /opt/netbox/gunicorn.py
    install -m 0644 /opt/netbox/contrib/netbox.service /etc/systemd/system/netbox.service
    install -m 0644 /opt/netbox/contrib/netbox-rq.service /etc/systemd/system/netbox-rq.service
    systemctl daemon-reload

    # Install Python dependencies, but do not initialize DB or generate secrets yet.
    if [[ "$netbox_version" == 4.4.9 ]]; then
      constraints=$(mktemp)
      printf '%s\n' 'pymdown-extensions==12.1' 'python-ldap==3.4.8' > "$constraints"
      (cd /opt/netbox && PIP_CONSTRAINT="$constraints" PYTHON=/usr/bin/python3.12 bash ./upgrade.sh --readonly)
      rm -f "$constraints"
    else
      (cd /opt/netbox && PYTHON=/usr/bin/python3.12 bash ./upgrade.sh --readonly)
    fi
    /opt/netbox/venv/bin/python -c 'import django; print("Django", django.get_version())'

    # Migrate using local peer authentication and public placeholder values.
    # No password, SSH key or runtime secret is ever written to this image.
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c 'CREATE ROLE netbox LOGIN'
    runuser -u postgres -- createdb --owner=netbox netbox
    cat > /opt/netbox/netbox/netbox/configuration.py <<'CONFIG'
ALLOWED_HOSTS = ['localhost', '127.0.0.1']
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox', 'USER': 'netbox', 'PASSWORD': '',
        'HOST': '', 'PORT': '', 'CONN_MAX_AGE': 300,
    },
}
REDIS = {
    'tasks': {
        'HOST': '127.0.0.1', 'PORT': 6379, 'USERNAME': '',
        'PASSWORD': '', 'DATABASE': 0, 'SSL': False,
    },
    'caching': {
        'HOST': '127.0.0.1', 'PORT': 6379, 'USERNAME': '',
        'PASSWORD': '', 'DATABASE': 1, 'SSL': False,
    },
}
SECRET_KEY = 'public-ci-placeholder-for-schema-migrations-not-a-real-secret-key'
API_TOKEN_PEPPERS = {1: 'public-ci-placeholder-for-schema-migrations-not-a-real-api-pepper'}
CONFIG
    if [[ "$netbox_version" == 4.4.9 ]]; then
      printf '\nPLUGINS = ["netbox_reorder_rack"]\n' >> /opt/netbox/netbox/netbox/configuration.py
    fi
    chown root:netbox /opt/netbox/netbox/netbox/configuration.py
    chmod 0640 /opt/netbox/netbox/netbox/configuration.py
    runuser -u netbox -- /opt/netbox/venv/bin/python \
      /opt/netbox/netbox/manage.py migrate --noinput
    test "$(runuser -u postgres -- psql -tAc 'SELECT count(*) FROM django_migrations' -d netbox)" -gt 0
    if [[ "$netbox_version" == 4.4.9 ]]; then
      runuser -u netbox -- /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py migrate --check --noinput
      test "$(runuser -u postgres -- psql -X -At -d netbox -c "SELECT count(*) FROM pg_extension WHERE extname='ltree'")" = 0
      /opt/netbox/venv/bin/python -c 'import ldap, importlib.metadata as m; assert m.version("netbox-reorder-rack")=="1.1.4"; assert m.version("tomli-w")=="1.2.0"; assert m.version("django-auth-ldap")=="5.4.0"; assert m.version("python-ldap")=="3.4.8"'
    fi
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
      -c 'ALTER ROLE netbox NOLOGIN PASSWORD NULL'
    rm -f /opt/netbox/netbox/netbox/configuration.py
    echo 'PASS: baseline database migrated; role locked and temporary config removed'

    cat > /etc/nginx/sites-available/netbox-ci <<'NGINX'
server {
    listen 80 default_server;
    server_name localhost;
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
    rm -f /etc/nginx/sites-enabled/default
    ln -s /etc/nginx/sites-available/netbox-ci /etc/nginx/sites-enabled/netbox-ci
    nginx -t
    systemctl reload nginx

    # Keep the reusable image independent of every future netbox-ops commit.
    test -x /opt/netbox/venv/bin/python
    test ! -e /opt/netbox-ops
    echo 'PASS: NetBox, PostgreSQL, Redis and nginx ready without netbox-ops source'
    ;;

  provision)
    # Each overlay receives independent credentials and a new NetBox database.
    systemctl start postgresql redis-server nginx
    password=$(openssl rand -hex 24)
    secret=$(openssl rand -hex 32)
    pepper=$(openssl rand -hex 32)
    # Each disposable VM re-enables the pre-migrated role with a new password.
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c \
      "ALTER ROLE netbox LOGIN PASSWORD '$password'"
    cat > /opt/netbox/netbox/netbox/configuration.py <<EOF
ALLOWED_HOSTS = ['localhost', '127.0.0.1']
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox',
        'USER': 'netbox',
        'PASSWORD': '$password',
        'HOST': '127.0.0.1',
        'PORT': '5432',
        'CONN_MAX_AGE': 300,
    }
}
REDIS = {
    'tasks': {
        'HOST': '127.0.0.1', 'PORT': 6379, 'USERNAME': '',
        'PASSWORD': '', 'DATABASE': 0, 'SSL': False,
    },
    'caching': {
        'HOST': '127.0.0.1', 'PORT': 6379, 'USERNAME': '',
        'PASSWORD': '', 'DATABASE': 1, 'SSL': False,
    },
}
SECRET_KEY = '$secret'
API_TOKEN_PEPPERS = {1: '$pepper'}
EOF
    if [[ "$netbox_version" == 4.4.9 ]]; then
      test "$(git -C /opt/netbox describe --tags --exact-match)" = v4.4.9
      printf '\nPLUGINS = ["netbox_reorder_rack"]\nLOGIN_REQUIRED = False\n' >> /opt/netbox/netbox/netbox/configuration.py
    fi
    chown root:netbox /opt/netbox/netbox/netbox/configuration.py
    chmod 0640 /opt/netbox/netbox/netbox/configuration.py
    unset password secret pepper

    # No migrations should be needed in a freshly provisioned overlay.
    runuser -u netbox -- /opt/netbox/venv/bin/python \
      /opt/netbox/netbox/manage.py migrate --check --noinput

    # Install netbox-ops from the checkout being tested (not the baseline).
    install -d -o root -g root -m 0755 /opt/netbox-ops
    tar -xzf /home/ubuntu/source.tar.gz -C /opt/netbox-ops
    chown -R root:root /opt/netbox-ops
    bash /opt/netbox-ops/install.sh
    /usr/local/sbin/netbox-ops --version
    systemctl enable --now netbox netbox-rq
    for i in $(seq 1 60); do
      if curl --fail --silent --show-error --output /dev/null \
        http://127.0.0.1/login/ 2>/dev/null; then
        break
      fi
      if (( i == 60 )); then
        journalctl -u netbox -n 80 --no-pager
        exit 1
      fi
      sleep 2
    done

    # Verify the real systemd units as well as the netbox-ops status command.
    for unit in netbox.service netbox-rq.service nginx.service redis-server.service postgresql@16-main.service; do
      systemctl is-active --quiet "$unit" || {
        systemctl status "$unit" --no-pager || true
        exit 1
      }
    done
    redis-cli ping | grep -qx PONG
    pg_isready -q -h 127.0.0.1
    /usr/local/sbin/netbox-ops --lang en services status
    curl --fail --silent --show-error --output /dev/null http://127.0.0.1/login/
    echo 'PASS: NetBox HTTP, PostgreSQL, Redis, nginx, RQ and netbox-ops service status'
    ;;

  sanitize)
    # No secrets are ever generated in prepare mode; remove build-time SSH identity.
    test ! -e /opt/netbox/netbox/netbox/configuration.py
    test ! -e /opt/netbox-ops
    role_state=$(runuser -u postgres -- psql -tA -d postgres \
      -c "SELECT rolcanlogin::text || '|' || (rolpassword IS NULL)::text FROM pg_authid WHERE rolname = 'netbox'")
    test "$role_state" = 'false|true'
    rm -f /home/ubuntu/netbox-vm-guest.sh /home/ubuntu/source.tar.gz
    cloud-init clean --logs --machine-id
    rm -rf /home/ubuntu/.ssh /root/.ssh
    rm -f /etc/ssh/ssh_host_*
    sync
    fstrim / || true
    test ! -e /home/ubuntu/.ssh/authorized_keys
    test ! -e /etc/ssh/ssh_host_ed25519_key
    echo 'PASS: build-time guest credentials removed'
    ;;

  *)
    echo "Usage: $0 prepare|provision|sanitize" >&2
    exit 2
    ;;
esac
