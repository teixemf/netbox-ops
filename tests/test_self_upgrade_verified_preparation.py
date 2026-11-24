"""Exercise the production preparation gate with offline official release fixtures."""
from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netbox_ops.self_upgrade_archive import ReleaseArchiveError
from netbox_ops.self_upgrade_catalogue import CompatibilityCatalogue
from netbox_ops.self_upgrade_compatibility import LocalEnvironment, ReleaseRequirements
from netbox_ops.self_upgrade_download import download_release
from netbox_ops.self_upgrade_installation import InstalledRelease
from netbox_ops.self_upgrade_manifest import encode_manifest, read_release_requirements
from netbox_ops.self_upgrade_prepare import prepare_next_release
from netbox_ops.self_upgrade_releases import (
    DOWNLOAD_PREFIX, OfficialRelease, ReleaseAsset, ReleaseDiscoveryError, Version,
)


@unittest.skipUnless(sys.version_info >= (3, 12), 'Python 3.12 required')
class VerifiedPreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.temp = self.root / 'tmp'
        self.temp.mkdir()
        self.opt = self.root / 'opt'
        self.opt.mkdir()
        self.active = self.opt / 'netbox-ops'
        self.active.mkdir()
        (self.active / 'keep').write_text('untouched')
        self.old = Version(0, 5, 0)
        self.middle = Version(0, 6, 0)
        self.target = Version(0, 7, 0)
        self.local = InstalledRelease(
            self.old, LocalEnvironment(tuple(sys.version_info[:2]), 'debian', 'archive'),
            Path(sys._base_executable).resolve(), (self.active.stat().st_dev, self.active.stat().st_ino),
        )
        self.assets = {}
        self.requests = []
        self.releases = [
            self.make_release(self.middle),
            self.make_release(self.target, (self.middle,)),
        ]
        self.start_patch('netbox_ops.self_upgrade_prepare._require_root')
        self.start_patch('netbox_ops.self_upgrade_prepare.ACTIVE', self.active)
        self.inspect = self.start_patch('netbox_ops.self_upgrade_prepare.inspect_installation',
                                        return_value=self.local)
        self.discovery = self.start_patch(
            'netbox_ops.self_upgrade_prepare.discover_compatibility', side_effect=self.catalogue)
        self.download = self.start_patch(
            'netbox_ops.self_upgrade_prepare.download_release', side_effect=self.fetch_archive)

    def start_patch(self, target, *args, **kwargs):
        p = patch(target, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def make_release(self, version, steps=(), *, minimum=(3, 12), invalid_archive=False):
        tag = f'v{version}'
        name = f'netbox-ops-{tag}.tar.gz'
        prefix = f'{DOWNLOAD_PREFIX}{tag}/'
        data = io.BytesIO()
        files = {
            'LICENSE': b'license',
            'README.md': b'readme',
            'CHANGELOG.md': b'changes',
            'config.example.json': b'{}',
            'install.sh': b'#!/bin/sh\nexit 99\n',
            'upgrade.sh': b'#!/bin/sh\nexit 99\n',
            'netbox_ops/__init__.py': f'__version__ = "{version}"\n'.encode(),
            'netbox_ops.py': f'print("netbox-ops {version}")\n'.encode(),
        }
        if invalid_archive:
            files['local-secret'] = b'not part of the official layout'
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            for name_in_tar, content in files.items():
                member = tarfile.TarInfo(f'netbox-ops/{name_in_tar}')
                member.mode = 0o755
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        package = data.getvalue()
        checksum = f'{hashlib.sha256(package).hexdigest()}  {name}\n'.encode()
        metadata = encode_manifest(ReleaseRequirements(
            version, minimum, None, frozenset({'debian', 'rhel'}),
            frozenset({'archive'}), steps,
        ))

        def asset(filename, content):
            self.assets[prefix + filename] = content
            return ReleaseAsset(filename, prefix + filename, len(content))

        return OfficialRelease(
            version, tag, '', '2026-11-24', asset(name, package),
            asset(name + '.sha256', checksum),
            asset(f'netbox-ops-{tag}.compatibility.json', metadata),
        )

    def opener(self, request, timeout):
        self.requests.append(request.full_url)
        return io.BytesIO(self.assets[request.full_url])

    def catalogue(self, *, installed, environment):
        return CompatibilityCatalogue(
            self.releases, installed=installed, environment=environment,
            reader=lambda release: read_release_requirements(release, opener=self.opener),
        )

    def fetch_archive(self, release, *, temp_root):
        self.assertEqual(temp_root, Path('/tmp'))
        return download_release(release, temp_root=self.temp, opener=self.opener)

    def prepare(self, *, steps=None, target=None, installed=None):
        # Keep all extraction and staging inside the isolated test directory.
        from netbox_ops.self_upgrade_runtime import stage_installation

        def stage(archive, checksum, version, **kwargs):
            kwargs['temp_root'] = self.temp
            return stage_installation(archive, checksum, version, **kwargs)

        with patch('netbox_ops.self_upgrade_prepare.stage_installation', side_effect=stage):
            return prepare_next_release(
                target or self.target, expected_installed=installed or self.local,
                expected_steps=steps if steps is not None else (self.middle, self.target),
            )

    def assert_untouched(self):
        self.assertEqual((self.active / 'keep').read_text(), 'untouched')
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertEqual(list(self.opt.iterdir()), [self.active])

    def test_prepare_only_first_required_release(self):
        result = self.prepare()
        self.assertEqual(result.release.version, self.middle)
        self.assertEqual(result.steps, (self.middle, self.target))
        self.assertEqual(result.installed, self.local)
        self.assertEqual(result.identity, (result.path.stat().st_dev, result.path.stat().st_ino))
        self.assertTrue((result.path / 'venv/bin/python').is_file())
        self.assertEqual((self.active / 'keep').read_text(), 'untouched')
        self.assertEqual(list(self.temp.iterdir()), [])
        self.download.assert_called_once()
        self.assertEqual(self.download.call_args.args[0].version, self.middle)
        self.assertFalse(any(url.endswith('v0.7.0.tar.gz') for url in self.requests))

    def test_changed_reviewed_path_blocks_before_download(self):
        with self.assertRaisesRegex(ReleaseArchiveError, 'path changed'):
            self.prepare(steps=(self.target,))
        self.download.assert_not_called()
        self.assert_untouched()

    def test_missing_or_incompatible_metadata_blocks_before_download(self):
        for candidate in (
            replace(self.releases[1], compatibility=None),
            self.make_release(self.target, (self.middle,), minimum=(99, 0)),
        ):
            with self.subTest(candidate=candidate.version):
                self.releases[1] = candidate
                with self.assertRaises(ReleaseArchiveError):
                    self.prepare()
        self.download.assert_not_called()
        self.assert_untouched()

    def test_changed_installation_identity_blocks_before_network(self):
        self.inspect.return_value = replace(self.local, identity=(1, 2))
        with self.assertRaisesRegex(ReleaseArchiveError, 'Installation changed'):
            self.prepare()
        self.discovery.assert_not_called()
        self.download.assert_not_called()
        self.assert_untouched()

    def test_downgrade_and_invalid_steps_block_before_inspection(self):
        for target, steps in ((self.old, (self.old,)), (self.target, ()),
                              (self.target, (self.target, self.target)),
                              (self.target, (self.target, self.middle))):
            with self.subTest(target=target, steps=steps):
                with self.assertRaisesRegex(ReleaseArchiveError, 'Invalid reviewed'):
                    self.prepare(target=target, steps=steps)
        self.inspect.assert_not_called()
        self.download.assert_not_called()
        self.assert_untouched()

    def test_discovery_error_blocks_before_download(self):
        self.discovery.side_effect = ReleaseDiscoveryError('offline')
        with self.assertRaisesRegex(ReleaseArchiveError, 'Cannot verify'):
            self.prepare()
        self.download.assert_not_called()
        self.assert_untouched()

    def test_changed_installation_during_staging_cleans_candidate(self):
        self.inspect.side_effect = [self.local, replace(self.local, identity=(1, 2))]
        with self.assertRaisesRegex(ReleaseArchiveError, 'changed during preparation'):
            self.prepare()
        self.assert_untouched()

    def test_corrupt_archive_cleans_download(self):
        url = self.releases[0].archive.url
        package = self.assets[url]
        self.assets[url] = b'x' + package[1:]
        with self.assertRaises(ReleaseArchiveError):
            self.prepare()
        self.assert_untouched()

    def test_unknown_archive_layout_is_rejected(self):
        self.releases[0] = self.make_release(self.middle, invalid_archive=True)
        with self.assertRaisesRegex(ReleaseArchiveError, 'Unknown release archive layout'):
            self.prepare()
        self.assert_untouched()

    def test_failed_candidate_layout_validation_cleans_stage(self):
        with patch('netbox_ops.self_upgrade_prepare.verify_candidate_layout',
                   side_effect=ReleaseArchiveError('invalid candidate')):
            with self.assertRaisesRegex(ReleaseArchiveError, 'invalid candidate'):
                self.prepare()
        self.assert_untouched()


class PrivilegeTests(unittest.TestCase):
    def test_nonroot_is_rejected_before_inspection(self):
        with (patch('netbox_ops.self_upgrade_prepare.os.geteuid', return_value=1000),
              patch('netbox_ops.self_upgrade_prepare.inspect_installation') as inspect):
            with self.assertRaisesRegex(ReleaseArchiveError, 'root privileges'):
                prepare_next_release(Version(0, 6, 0), expected_installed=None,
                                     expected_steps=(Version(0, 6, 0),))
        inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
