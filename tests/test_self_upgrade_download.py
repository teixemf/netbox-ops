"""Offline download tests: no network or installation changes."""
from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_download import download_release
from netbox_ops.self_upgrade_releases import (
    DOWNLOAD_PREFIX, OfficialRelease, ReleaseAsset, Version,
)


class Response:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def __enter__(self):
        return self.stream

    def __exit__(self, *_):
        self.stream.close()


def package():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name in ('netbox-ops/netbox_ops.py', 'netbox-ops/install.sh',
                     'netbox-ops/upgrade.sh', 'netbox-ops/netbox_ops/__init__.py'):
            info = tarfile.TarInfo(name)
            info.size = 1
            bundle.addfile(info, io.BytesIO(b'x'))
    return output.getvalue()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = package()
        name = 'netbox-ops-v0.6.0.tar.gz'
        self.checksum = (hashlib.sha256(self.archive).hexdigest() + '  ' + name).encode()
        self.release = OfficialRelease(
            Version(0, 6, 0), 'v0.6.0', '', '2026-11-23',
            ReleaseAsset(name, DOWNLOAD_PREFIX + 'v0.6.0/' + name, len(self.archive)),
            ReleaseAsset(name + '.sha256', DOWNLOAD_PREFIX + 'v0.6.0/' + name + '.sha256',
                         len(self.checksum)),
        )

    def fetch(self, request, timeout):
        return Response(self.checksum if request.full_url.endswith('.sha256') else self.archive)

    def test_verified_download(self):
        path = download_release(self.release, temp_root=self.root, opener=self.fetch)
        self.assertEqual(path.read_bytes(), self.archive)
        self.assertEqual(path.parent.parent, self.root)

    def test_corrupt_archive_cleans_up(self):
        def fetch(request, timeout):
            return Response(b'bad' if request.full_url.endswith('.tar.gz') else self.checksum)
        with self.assertRaises(ReleaseArchiveError):
            download_release(self.release, temp_root=self.root, opener=fetch)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_oversized_stream_cleans_up(self):
        def fetch(request, timeout):
            return Response(self.archive + b'padding'
                            if request.full_url.endswith('.tar.gz') else self.checksum)
        with self.assertRaises(ReleaseArchiveError):
            download_release(self.release, temp_root=self.root, opener=fetch)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reject_unofficial_asset_before_network(self):
        bad = OfficialRelease(
            self.release.version, self.release.tag, '', '',
            ReleaseAsset(self.release.archive.name, 'https://example.com/archive', 10),
            self.release.checksum,
        )
        with self.assertRaises(ReleaseArchiveError):
            download_release(bad, temp_root=self.root,
                             opener=lambda *_args, **_kwargs: self.fail('network used'))
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
