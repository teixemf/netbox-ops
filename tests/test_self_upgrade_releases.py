"""Offline tests for the official release catalogue (no network or writes)."""
from __future__ import annotations

import io
import json
import unittest
from urllib.error import URLError

from netbox_ops.self_upgrade_releases import (
    MAX_RESPONSE_BYTES, ReleaseDiscoveryError, Version,
    discover_releases, parse_releases,
)


def release(version='1.2.3', *, draft=False, prerelease=False, assets=True):
    tag = f'v{version}'
    base = f'https://github.com/teixemf/netbox-ops/releases/download/{tag}/'
    archive = f'netbox-ops-{tag}.tar.gz'
    return {
        'tag_name': tag, 'draft': draft, 'prerelease': prerelease,
        'body': f'Changes for {version}', 'published_at': '2026-11-23T00:00:00Z',
        'assets': [
            {'name': archive, 'browser_download_url': base + archive, 'size': 100},
            {'name': archive + '.sha256', 'browser_download_url': base + archive + '.sha256', 'size': 64},
        ] if assets else [],
    }


class FakeResponse:
    def __init__(self, data):
        self.data = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.data.read(size)


class ReleaseVersionTests(unittest.TestCase):
    def test_stable_version_order_not_lexical(self):
        self.assertLess(Version.parse('v1.9.9'), Version.parse('v1.10.0'))
        self.assertEqual(str(Version.parse('v0.6.0')), '0.6.0')

    def test_reject_prerelease_noncanonical_and_invalid_versions(self):
        for tag in ('1.2.3', 'v1.2.3-rc1', 'v01.2.3', 'v1.2', 'v1.2.3+build', 'v1.2.3x', None):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                Version.parse(tag)


class ReleaseListTests(unittest.TestCase):
    def test_filters_drafts_prereleases_test_tags_and_missing_assets(self):
        rows = [release('0.6.0'), release('0.5.0', assets=False),
                release('0.7.0', draft=True), release('0.8.0', prerelease=True),
                {**release(), 'tag_name': 'vm-poc-123'}]
        self.assertEqual([str(item.version) for item in parse_releases(rows)], ['0.6.0'])

    def test_keeps_intermediate_versions_and_changelogs(self):
        rows = [release('0.7.0'), release('0.6.0'), release('1.0.0'), release('0.5.1')]
        result = parse_releases(rows)
        self.assertEqual([str(item.version) for item in result], ['1.0.0', '0.7.0', '0.6.0', '0.5.1'])
        self.assertEqual(result[2].changelog, 'Changes for 0.6.0')

    def test_invalid_asset_urls_and_ambiguous_assets_are_rejected(self):
        bad = release()
        bad['assets'][0]['browser_download_url'] = 'https://attacker.example/archive.tar.gz'
        self.assertEqual(parse_releases([bad]), [])
        duplicate_asset = release()
        duplicate_asset['assets'].append(duplicate_asset['assets'][0])
        self.assertEqual(parse_releases([duplicate_asset]), [])
        wrong_size = release()
        wrong_size['assets'][1]['size'] = True
        self.assertEqual(parse_releases([wrong_size]), [])

    def test_duplicate_versions_fail_closed(self):
        self.assertEqual(parse_releases([release('0.6.0')] * 3), [])

    def test_duplicate_tag_with_invalid_assets_fails_closed(self):
        valid = release('0.6.0')
        invalid = release('0.6.0', assets=False)
        for rows in ([valid, invalid], [invalid, valid], [invalid, valid, valid]):
            with self.subTest(rows=rows):
                self.assertEqual(parse_releases(rows), [])

    def test_duplicate_asset_name_with_invalid_url_fails_closed(self):
        for index in (0, 1):
            row = release()
            duplicate = dict(row['assets'][index])
            duplicate['browser_download_url'] = 'https://attacker.example/file'
            row['assets'].append(duplicate)
            with self.subTest(index=index):
                self.assertEqual(parse_releases([row]), [])

    def test_unsupported_responses_and_missing_changelog(self):
        with self.assertRaises(ReleaseDiscoveryError):
            parse_releases({'message': 'API error'})
        wrong = release()
        wrong['body'] = None
        self.assertEqual(parse_releases([wrong]), [])

    def test_paginated_fetch_and_fixed_api_host(self):
        urls = []
        def opener(request, timeout):
            urls.append((request.full_url, timeout))
            return FakeResponse(json.dumps([release('0.6.0')] + [release('0.7.0', draft=True)] * 99 if len(urls) == 1
                                           else [release('0.5.1')]).encode())
        releases = discover_releases(opener=opener)
        self.assertEqual([str(r.version) for r in releases], ['0.6.0', '0.5.1'])
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(url.startswith('https://api.github.com/repos/teixemf/netbox-ops/releases?')
                            and timeout == 10 for url, timeout in urls))

    def test_http_and_json_errors_fail_closed(self):
        def bad_network(*_args, **_kwargs):
            raise URLError('offline')
        with self.assertRaises(ReleaseDiscoveryError):
            discover_releases(opener=bad_network)
        with self.assertRaises(ReleaseDiscoveryError):
            discover_releases(opener=lambda *_a, **_k: FakeResponse(b'{oops'))
        with self.assertRaises(ReleaseDiscoveryError):
            discover_releases(opener=lambda *_a, **_k: FakeResponse(b'x' * (MAX_RESPONSE_BYTES + 1)))

    def test_page_limit_is_bounded(self):
        for value in (0, 6, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                discover_releases(max_pages=value)


if __name__ == '__main__':
    unittest.main()
