#!/usr/bin/env bash
# Prepare an Ubuntu test guest, or provision a fresh NetBox instance for a disposable boot.
set -euo pipefail

mode=$1
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
    netbox_version=4.7.3
    install -d -m 0755 /opt/netbox
    curl -fLsS --retry 3 \
      -o /tmp/netbox-source.tar.gz \
      "https://github.com/netbox-community/netbox/archive/refs/tags/v$netbox_version.tar.gz"
    tar -xzf /tmp/netbox-source.tar.gz -C /opt/netbox --strip-components=1
    rm -f /tmp/netbox-source.tar.gz

    adduser --system --group --quiet netbox
    chown -R netbox:netbox /opt/netbox/netbox/media \
      /opt/netbox/netbox/reports /opt/netbox/netbox/scripts
    install -m 0644 /opt/netbox/contrib/gunicorn.py /opt/netbox/gunicorn.py
    sed -i 's/^workers = 5$/workers = 2/' /opt/netbox/gunicorn.py
    install -m 0644 /opt/netbox/contrib/netbox.service /etc/systemd/system/netbox.service
    install -m 0644 /opt/netbox/contrib/netbox-rq.service /etc/systemd/system/netbox-rq.service
    systemctl daemon-reload

    # Install Python dependencies, but do not initialize DB or generate secrets yet.
    (cd /opt/netbox && PYTHON=/usr/bin/python3.12 bash ./upgrade.sh --readonly)
    /opt/netbox/venv/bin/python -c 'import django; print("Django", django.get_version())'

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

    # netbox-ops has its own virtual environment and never alters NetBox's.
    bash /opt/netbox-ops/install.sh
    /usr/local/sbin/netbox-ops --version
    test -x /opt/netbox/venv/bin/python
    test -x /opt/netbox-ops/venv/bin/python
    echo 'PASS: NetBox, PostgreSQL, Redis, nginx and netbox-ops installed (no credentials)'
    ;;

  provision)
    # Each overlay receives independent credentials and a new NetBox database.
    systemctl start postgresql redis-server nginx
    password=$(openssl rand -hex 24)
    secret=$(openssl rand -hex 32)
    pepper=$(openssl rand -hex 32)
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c \
      "CREATE ROLE netbox LOGIN PASSWORD '$password'"
    runuser -u postgres -- createdb --owner=netbox netbox
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
    chown root:netbox /opt/netbox/netbox/netbox/configuration.py
    chmod 0640 /opt/netbox/netbox/netbox/configuration.py
    unset password secret pepper

    runuser -u netbox -- /opt/netbox/venv/bin/python \
      /opt/netbox/netbox/manage.py migrate --noinput
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
    cloud-init clean --logs --machine-id
    rm -rf /home/ubuntu/.ssh /root/.ssh
    rm -f /etc/ssh/ssh_host_*
    sync
    fstrim / || true
    systemctl poweroff --no-block
    ;;

  *)
    echo "Usage: $0 prepare|provision|sanitize" >&2
    exit 2
    ;;
esac
