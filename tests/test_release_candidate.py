"""Release producer and consumer share the same strict asset contract."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from netbox_ops.self_upgrade_archive import verify_release_archive
from netbox_ops.self_upgrade_manifest import parse_manifest
from netbox_ops.self_upgrade_releases import Version

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/release-candidate.py'
TAG = 'v0.6.0'
POLICY = {
    'python_min': '3.12', 'python_max_exclusive': None,
    'linux_families': ['debian', 'rhel'], 'layouts': ['archive'],
    'required_intermediates': ['v0.5.1'],
}


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.archive = Path(work.name) / f'netbox-ops-{TAG}.tar.gz'
        self.checksum = self.archive.with_name(self.archive.name + '.sha256')
        self.manifest = self.archive.with_name(f'netbox-ops-{TAG}.compatibility.json')
        self.notes = self.archive.with_name(f'netbox-ops-{TAG}.notes.md')

    def package(self, *, version='0.6.0', readme_version='0.6.0',
                changelog=None, mode=0o755, policy=None, uninstall_mode=None):
        files = {
            'netbox_ops/__init__.py': f'__version__ = "{version}"\n',
            'README.md': f'**Current version: {readme_version} — Example**\n',
            'CHANGELOG.md': changelog if changelog is not None else
            '# Changelog\n\n## [0.6.0] — Example\n\n- New feature.\n\n'
            '## [0.5.0] — Old\n\n- Old change.\n',
            'netbox_ops/release_requirements.json': json.dumps(POLICY) if policy is None else policy,
            'netbox_ops.py': 'pass\n', 'install.sh': '#!/bin/sh\n',
            'upgrade.sh': '#!/bin/sh\n', 'LICENSE': 'Example\n',
            'config.example.json': '{}\n',
        }
        if uninstall_mode is not None:
            files['uninstall.sh'] = '#!/bin/sh\n'
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for name, content in files.items():
                raw = content.encode()
                member = tarfile.TarInfo(f'netbox-ops/{name}')
                member.size = len(raw)
                member.mode = (uninstall_mode if name == 'uninstall.sh' else
                               mode if name in ('install.sh', 'upgrade.sh') else 0o644)
                bundle.addfile(member, io.BytesIO(raw))

    def invoke(self, action, *, tag=TAG, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), action, str(self.archive), tag],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_build_and_validate_round_trip(self):
        self.package()
        self.invoke('build')
        verify_release_archive(self.archive, self.checksum.read_bytes())
        manifest = parse_manifest(self.manifest.read_bytes(), Version.parse(TAG))
        self.assertEqual(manifest.python_min, (3, 12))
        self.assertEqual(manifest.required_intermediates, (Version(0, 5, 1),))
        self.assertEqual(self.notes.read_text(), '## [0.6.0] — Example\n\n- New feature.\n')
        self.invoke('validate')
        before = [p.read_bytes() for p in (self.checksum, self.manifest, self.notes)]
        self.invoke('build')
        self.assertEqual(before, [p.read_bytes() for p in (self.checksum, self.manifest, self.notes)])

    def test_new_uninstaller_must_be_executable_when_present(self):
        self.package(uninstall_mode=0o755)
        self.invoke('build')
        self.invoke('validate')
        self.package(uninstall_mode=0o644)
        self.invoke('build', expected=1)

    def test_invalid_candidate_does_not_generate_assets(self):
        for kwargs in (
            {'version': '0.6.1'}, {'readme_version': '0.5.0'}, {'mode': 0o644},
            {'changelog': '## [0.5.0] — Old\n\n- Old.\n'},
            {'changelog': '## [0.6.0] — Empty\n'},
            {'policy': json.dumps({**POLICY, 'required_intermediates': ['v0.6.0']})},
            {'policy': json.dumps({**POLICY, 'python_min': 'invalid'})},
            {'policy': '{"python_min":"3.12","python_min":"3.13"}'},
        ):
            with self.subTest(kwargs=kwargs):
                self.package(**kwargs)
                self.invoke('build', expected=1)
                self.assertFalse(any(p.exists() for p in (self.checksum, self.manifest, self.notes)))

    def test_tag_mismatch_blocks_build(self):
        self.package()
        self.invoke('build', tag='v0.6.1', expected=1)
        self.assertFalse(self.manifest.exists())

    def test_tampered_assets_are_rejected(self):
        self.package()
        self.invoke('build')
        for path in (self.checksum, self.manifest, self.notes):
            original = path.read_bytes()
            path.write_bytes(original + b'\n')
            self.invoke('validate', expected=1)
            path.write_bytes(original)
        self.invoke('validate')
        self.manifest.unlink()
        self.invoke('validate', expected=1)
