"""Bounded read-only health checks against the operator's NetBox status URL."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .upgrade_execution import UpgradeExecutionError

_MAX_RESPONSE = 1024 * 1024


@dataclass(frozen=True)
class NetBoxHealth:
    version: str
    hostname: str | None
    plugins: tuple[tuple[str, str | None], ...]
    workers: int


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or not parsed.path.endswith('/api/status/')
                or any(ord(c) < 33 for c in url)):
            raise ValueError
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise UpgradeExecutionError('Provide a valid local HTTP or HTTPS NetBox API status URL') from None


def read_netbox_health(url: str, *, authorization: str | None = None,
                       opener=None) -> NetBoxHealth:
    """Verify HTTP 200, bounded JSON, loaded plugins and at least one RQ worker."""
    _validate_url(url)
    headers = {'Accept': 'application/json'}
    if authorization:
        if '\r' in authorization or '\n' in authorization:
            raise UpgradeExecutionError('Invalid NetBox authorization header')
        if urlsplit(url).scheme != 'https':
            raise UpgradeExecutionError('NetBox API credentials require HTTPS')
        headers['Authorization'] = authorization
    try:
        client = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        with client.open(Request(url, headers=headers), timeout=10) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError
            data = response.read(_MAX_RESPONSE + 1)
            if len(data) > _MAX_RESPONSE:
                raise ValueError
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError
        version = payload.get('netbox-version')
        hostname = payload.get('hostname')
        plugins = payload.get('plugins')
        workers = payload.get('rq-workers-running')
        if (not isinstance(version, str) or not version
                or (hostname is not None and not isinstance(hostname, str))
                or not isinstance(plugins, dict)
                or any(not isinstance(k, str) or (v is not None and not isinstance(v, str))
                       for k, v in plugins.items())
                or type(workers) is not int or workers < 1):
            raise ValueError
        return NetBoxHealth(version, hostname, tuple(sorted(plugins.items())), workers)
    except Exception:
        raise UpgradeExecutionError('NetBox HTTP status or plugin/worker health could not be verified') from None


def wait_for_netbox_health(url: str, expected_version: str, baseline: NetBoxHealth,
                           *, authorization: str | None = None,
                           attempts: int = 6) -> NetBoxHealth:
    """Require the expected release and unchanged loaded plugins on the same host."""
    for attempt in range(attempts):
        try:
            state = read_netbox_health(url, authorization=authorization)
            if (state.version == expected_version
                    and (baseline.hostname is None or state.hostname == baseline.hostname)
                    and state.plugins == baseline.plugins):
                return state
        except UpgradeExecutionError:
            pass
        if attempt + 1 < attempts:
            time.sleep(5)
    raise UpgradeExecutionError('NetBox HTTP release, hostname, plugins or workers did not validate')
