"""Mocked NetBox API health checks; no network access."""
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_health import (
    NetBoxHealth, read_netbox_health, wait_for_netbox_health,
)

URL = 'https://netbox.example/api/status/'


class UpgradeHealthTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            'netbox-version': '4.4.1', 'hostname': 'netbox',
            'plugins': {'example_plugin': '1.2.3'}, 'rq-workers-running': 1,
        }
        self.opener = MagicMock()
        self.response = self.opener.open.return_value.__enter__.return_value
        self.response.status = 200
        self.response.geturl.return_value = URL
        self.response.read.side_effect = lambda n: json.dumps(self.payload).encode()

    def test_status_checks_version_plugins_and_workers(self):
        state = read_netbox_health(URL, opener=self.opener)
        self.assertEqual(state, NetBoxHealth(
            '4.4.1', 'netbox', (('example_plugin', '1.2.3'),), 1,
        ))

    def test_invalid_url_rejected_before_request(self):
        for url in ('file:///etc/shadow', 'https://name:secret@host/api/status/',
                    'http://remote.example/api/status/', 'https://host/api/status/?token=secret'):
            with self.subTest(url=url), self.assertRaises(UpgradeExecutionError):
                read_netbox_health(url, opener=self.opener)
        self.opener.open.assert_not_called()

    def test_unhealthy_workers_are_rejected(self):
        self.payload['rq-workers-running'] = 0
        with self.assertRaises(UpgradeExecutionError):
            read_netbox_health(URL, opener=self.opener)

    def test_wrong_endpoint_or_redirect_is_rejected(self):
        self.response.geturl.return_value = 'https://other.example/api/status/'
        with self.assertRaises(UpgradeExecutionError):
            read_netbox_health(URL, opener=self.opener)

    def test_network_failure_does_not_echo_credentials(self):
        self.opener.open.side_effect = OSError('secret token')
        with self.assertRaises(UpgradeExecutionError) as caught:
            read_netbox_health(URL, authorization='Bearer secret', opener=self.opener)
        self.assertNotIn('secret', str(caught.exception))

    def test_older_status_without_hostname_and_plugin_version(self):
        self.payload.pop('hostname')
        self.payload['plugins']['example_plugin'] = None
        old = read_netbox_health(URL, opener=self.opener)
        self.assertIsNone(old.hostname)
        new = NetBoxHealth('4.4.2', 'netbox', old.plugins, 1)
        with patch('netbox_ops.upgrade_health.read_netbox_health', return_value=new):
            self.assertEqual(wait_for_netbox_health(URL, '4.4.2', old, attempts=1), new)

    def test_credentials_require_https(self):
        with self.assertRaises(UpgradeExecutionError):
            read_netbox_health('http://127.0.0.1/api/status/',
                               authorization='Bearer secret', opener=self.opener)
        self.opener.open.assert_not_called()

    def test_changed_plugin_or_hostname_never_validates(self):
        baseline = NetBoxHealth('4.3.9', 'netbox', (('example_plugin', '1.2.3'),), 1)
        for state in (NetBoxHealth('4.4.1', 'netbox', (), 1),
                      NetBoxHealth('4.4.1', 'other', baseline.plugins, 1),
                      NetBoxHealth('4.4.0', 'netbox', baseline.plugins, 1)):
            with self.subTest(state=state), patch(
                'netbox_ops.upgrade_health.read_netbox_health', return_value=state,
            ), self.assertRaises(UpgradeExecutionError):
                wait_for_netbox_health(URL, '4.4.1', baseline, attempts=1)

    def test_retries_startup_until_matching_release(self):
        old = NetBoxHealth('4.3.9', 'netbox', (), 1)
        new = NetBoxHealth('4.4.1', 'netbox', (), 1)
        read = Mock(side_effect=[UpgradeExecutionError('not ready'), new])
        with patch('netbox_ops.upgrade_health.read_netbox_health', read), patch(
            'netbox_ops.upgrade_health.time.sleep',
        ) as sleep:
            self.assertEqual(wait_for_netbox_health(URL, '4.4.1', old), new)
        sleep.assert_called_once_with(5)


if __name__ == '__main__':
    unittest.main()
