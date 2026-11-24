"""Offline coverage for the proposed compatibility metadata consumer."""
from __future__ import annotations

import io
import json
import unittest
from dataclasses import replace
from urllib.error import URLError
from unittest.mock import Mock
from urllib.request import Request

from netbox_ops.self_upgrade_catalogue import CompatibilityCatalogue
from netbox_ops.self_upgrade_compatibility import LocalEnvironment
from netbox_ops.self_upgrade_manifest import (
    ManifestError, _OfficialAssetRedirect, parse_manifest, read_release_requirements,
)
from netbox_ops.self_upgrade_releases import (
    DOWNLOAD_PREFIX, MAX_MANIFEST_BYTES, OfficialRelease, Version,
    parse_releases,
)


def payload(tag='v0.6.0', **changes):
    data = {
        'schema_version': 1, 'version': tag, 'python_min': '3.12',
        'python_max_exclusive': None, 'linux_families': ['debian', 'rhel'],
        'layouts': ['archive'], 'required_intermediates': [],
    }
    data.update(changes)
    return json.dumps(data).encode()


def row(tag, content):
    prefix = f'{DOWNLOAD_PREFIX}{tag}/'
    archive = f'netbox-ops-{tag}.tar.gz'
    manifest = f'netbox-ops-{tag}.compatibility.json'
    return {
        'tag_name': tag, 'draft': False, 'prerelease': False, 'body': 'Changes',
        'published_at': '2026-11-24T00:00:00Z',
        'assets': [
            {'name': name, 'browser_download_url': prefix + name, 'size': size}
            for name, size in ((archive, 100), (archive + '.sha256', 100),
                               (manifest, len(content)))
        ],
    }


def official(tag, content):
    return parse_releases([row(tag, content)])[0]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.version = Version(0, 6, 0)
        self.data = payload()
        self.release = official('v0.6.0', self.data)

    def test_normalizes_versioned_requirements(self):
        result = parse_manifest(self.data, self.version)
        self.assertEqual(result.version, self.version)
        self.assertEqual(result.python_min, (3, 12))
        self.assertEqual(result.layouts, frozenset({'archive'}))
        self.assertEqual(result.linux_families, frozenset({'debian', 'rhel'}))
        self.assertIsNone(result.python_max_exclusive)

    def test_invalid_schema_and_requirements(self):
        for changes in (
            {'schema_version': True}, {'schema_version': 2},
            {'version': 'v0.7.0'}, {'extra': 0}, {'python_min': '3.12.1'},
            {'python_min': [3, 12]}, {'python_max_exclusive': '3.12'},
            {'linux_families': []}, {'layouts': ['archive', 'archive']},
            {'layouts': ['bad\nname']}, {'required_intermediates': ['v0.6.0']},
            {'required_intermediates': ['v0.5.1', 'v0.5.0']},
            {'required_intermediates': ['v0.5.0', 'v0.5.0']},
            {'required_intermediates': 'v0.5.0'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ManifestError):
                parse_manifest(payload(**changes), self.version)

    def test_missing_duplicate_and_malformed_fields(self):
        missing = json.loads(self.data)
        del missing['required_intermediates']
        duplicate = self.data[:-1] + b', "version": "v0.6.0"}'
        for data in (json.dumps(missing).encode(), duplicate, b'{', b'\xff',
                     b'[]', b'[' * 2000 + b']' * 2000, b'x' * (MAX_MANIFEST_BYTES + 1)):
            with self.subTest(size=len(data)), self.assertRaises(ManifestError):
                parse_manifest(data, self.version)

    def test_discovery_keeps_legacy_and_rejects_ambiguous_manifest(self):
        for kind in ('missing', 'duplicate', 'wrong_url', 'oversized'):
            item = row('v0.6.0', self.data)
            if kind == 'missing':
                item['assets'].pop()
            elif kind == 'duplicate':
                item['assets'].append(dict(item['assets'][-1]))
            elif kind == 'wrong_url':
                item['assets'][-1]['browser_download_url'] = 'https://example.com/x'
            else:
                item['assets'][-1]['size'] = MAX_MANIFEST_BYTES + 1
            with self.subTest(kind=kind):
                releases = parse_releases([item])
                self.assertEqual(len(releases), 1)
                self.assertIsNone(releases[0].compatibility)

    def test_fetch_exact_official_url_and_bounded_content(self):
        opener = Mock(return_value=io.BytesIO(self.data))
        result = read_release_requirements(self.release, opener=opener)
        self.assertEqual(result.version, self.version)
        self.assertEqual(opener.call_args.args[0].full_url, self.release.compatibility.url)
        self.assertEqual(opener.call_args.kwargs, {'timeout': 10})

    def test_reject_invalid_asset_before_network(self):
        opener = Mock(side_effect=AssertionError('network used'))
        for asset in (None, replace(self.release.compatibility, url='https://example.com/x'),
                      replace(self.release.compatibility, size=True),
                      replace(self.release.compatibility, size=MAX_MANIFEST_BYTES + 1)):
            with self.subTest(asset=asset), self.assertRaises(ManifestError):
                read_release_requirements(replace(self.release, compatibility=asset), opener=opener)
        opener.assert_not_called()

    def test_reject_truncated_oversized_and_failed_download(self):
        for opener in (Mock(return_value=io.BytesIO(self.data[:-1])),
                       Mock(return_value=io.BytesIO(b'x' * (MAX_MANIFEST_BYTES + 1))),
                       Mock(side_effect=URLError('offline'))):
            with self.assertRaises(ManifestError):
                read_release_requirements(self.release, opener=opener)

    def test_redirect_policy_rejects_insecure_or_unrelated_hosts(self):
        handler = _OfficialAssetRedirect()
        req = Request(self.release.compatibility.url)
        for url in ('http://release-assets.githubusercontent.com/file',
                    'https://example.com/file',
                    'https://release-assets.githubusercontent.com.example.com/file',
                    'https://name@release-assets.githubusercontent.com/file',
                    'https://release-assets.githubusercontent.com:8443/file'):
            with self.subTest(url=url), self.assertRaises(ManifestError):
                handler.redirect_request(req, None, 302, 'Found', {}, url)
        url = 'https://release-assets.githubusercontent.com/file?signature=fixture'
        self.assertEqual(handler.redirect_request(req, None, 302, 'Found', {}, url).full_url, url)


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.installed = Version(0, 5, 0)
        self.middle = Version(0, 6, 0)
        self.target = Version(0, 7, 0)
        self.environment = LocalEnvironment((3, 12), 'debian', 'archive')
        self.contents = {
            'v0.4.0': payload('v0.4.0'), 'v0.5.0': payload('v0.5.0'),
            'v0.6.0': payload('v0.6.0'),
            'v0.7.0': payload('v0.7.0', required_intermediates=['v0.6.0']),
        }
        self.releases = [official(tag, data) for tag, data in self.contents.items()]
        self.reader = Mock(side_effect=self.read)

    def read(self, release: OfficialRelease):
        return read_release_requirements(
            release, opener=lambda *args, **kwargs: io.BytesIO(self.contents[release.tag]))

    def catalogue(self, **kwargs):
        return CompatibilityCatalogue(self.releases, installed=self.installed,
                                      environment=self.environment, reader=self.reader, **kwargs)

    def test_classifies_all_releases_and_fetches_each_metadata_once(self):
        catalogue = self.catalogue()
        result = catalogue.decisions()
        self.assertEqual([decision.status for _, decision in result],
                         ['compatible', 'compatible', 'current', 'downgrade'])
        self.assertEqual(result[0][1].steps, (self.middle, self.target))
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(catalogue.decision_for(self.target), result[0][1])
        self.assertEqual(self.reader.call_count, 2)

    def test_missing_manifest_blocks_target_and_no_network_for_missing(self):
        self.releases[2] = replace(self.releases[2], compatibility=None)
        decision = self.catalogue().decision_for(self.target)
        self.assertEqual(decision.status, 'unverified')
        self.assertEqual(decision.blocked_at, self.middle)
        self.assertEqual(decision.steps, ())
        self.assertEqual(self.reader.call_count, 1)

    def test_incompatible_manifest_and_fetch_failure_are_distinct(self):
        data = payload('v0.7.0', python_min='3.13')
        self.contents['v0.7.0'] = data
        self.releases[-1] = official('v0.7.0', data)
        self.assertEqual(self.catalogue().decision_for(self.target).status, 'incompatible')
        self.reader.side_effect = ManifestError('offline')
        self.assertEqual(self.catalogue().decision_for(self.target).status, 'unverified')

    def test_budget_exhaustion_blocks_required_step(self):
        decision = self.catalogue(max_requests=1).decision_for(self.target)
        self.assertEqual(decision.status, 'unverified')
        self.assertEqual(decision.steps, ())
        self.assertEqual(self.reader.call_count, 1)

    def test_unknown_environment_never_fetches_metadata(self):
        self.environment = replace(self.environment, layout=None)
        self.assertEqual(self.catalogue().decision_for(self.target).status, 'unverified')
        self.reader.assert_not_called()

    def test_duplicate_version_blocks_before_network(self):
        self.releases.append(self.releases[-1])
        self.assertEqual(self.catalogue().decision_for(self.target).status, 'unverified')
        self.reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
