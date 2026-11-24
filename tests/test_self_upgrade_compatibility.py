"""Offline tests of the normalized compatibility policy (not manifest parsing)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from netbox_ops.self_upgrade_compatibility import (
    LocalEnvironment, ReleaseRequirements, evaluate_upgrade,
)
from netbox_ops.self_upgrade_releases import OfficialRelease, ReleaseAsset, Version


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.old = Version(0, 5, 0)
        self.middle = Version(0, 6, 0)
        self.target = Version(0, 7, 0)
        self.environment = LocalEnvironment((3, 12), 'debian', 'archive')
        self.releases = [self.release(version) for version in (self.middle, self.target)]
        self.requirements = {
            version: ReleaseRequirements(version, (3, 12), None,
                                         frozenset({'debian', 'rhel'}), frozenset({'archive'}))
            for version in (self.middle, self.target)
        }

    @staticmethod
    def release(version):
        asset = ReleaseAsset('unused', 'unused', 1)
        return OfficialRelease(version, f'v{version}', '', '2026-11-24', asset, asset)

    def evaluate(self, target=None, installed=None):
        return evaluate_upgrade(installed or self.old, target or self.target,
                                self.releases, self.requirements, self.environment)

    def require(self, key, **changes):
        self.requirements[key] = replace(self.requirements[key], **changes)

    def test_direct_upgrade_does_not_include_optional_versions(self):
        result = self.evaluate()
        self.assertEqual(result.status, 'compatible')
        self.assertEqual(result.steps, (self.target,))

    def test_mandatory_intermediate(self):
        self.require(self.target, required_intermediates=(self.middle,))
        self.assertEqual(self.evaluate().steps, (self.middle, self.target))
        self.assertEqual(self.evaluate(installed=self.middle).steps, (self.target,))

    def test_nested_intermediates(self):
        first = Version(0, 5, 1)
        self.releases.append(self.release(first))
        self.requirements[first] = replace(self.requirements[self.middle], version=first)
        self.require(self.middle, required_intermediates=(first,))
        self.require(self.target, required_intermediates=(self.middle,))
        self.assertEqual(self.evaluate().steps, (first, self.middle, self.target))

    def test_unavailable_intermediate_blocks_whole_plan(self):
        self.require(self.target, required_intermediates=(self.middle,))
        self.releases.pop(0)
        result = self.evaluate()
        self.assertEqual(result.status, 'unverified')
        self.assertEqual(result.blocked_at, self.middle)
        self.assertEqual(result.steps, ())

    def test_incompatible_intermediate_blocks_whole_plan(self):
        self.require(self.target, required_intermediates=(self.middle,))
        self.require(self.middle, python_min=(3, 13))
        result = self.evaluate()
        self.assertEqual(result.status, 'incompatible')
        self.assertEqual(result.blocked_at, self.middle)
        self.assertEqual(result.steps, ())

    def test_missing_metadata_never_implies_compatibility(self):
        del self.requirements[self.target]
        result = self.evaluate()
        self.assertEqual(result.status, 'unverified')
        self.assertEqual(result.reason, 'missing_or_invalid_metadata')

    def test_python_minimum_and_exclusive_maximum(self):
        self.require(self.target, python_min=(3, 13))
        self.assertEqual(self.evaluate().reason, 'unsupported_python')
        self.require(self.target, python_min=(3, 11), python_max_exclusive=(3, 12))
        self.assertEqual(self.evaluate().reason, 'unsupported_python')

    def test_unsupported_linux_and_layout(self):
        self.environment = replace(self.environment, linux_family='other')
        self.assertEqual(self.evaluate().reason, 'unsupported_linux')
        self.environment = LocalEnvironment((3, 12), 'rhel', 'unknown')
        self.assertEqual(self.evaluate().reason, 'unsupported_layout')

    def test_unknown_environment_is_not_compatible(self):
        self.environment = replace(self.environment, layout=None)
        self.assertEqual(self.evaluate().reason, 'unknown_local_environment')

    def test_invalid_metadata_and_intermediate_cycle(self):
        for changes in ({'version': self.middle},
                        {'linux_families': frozenset()},
                        {'required_intermediates': (self.target,)},
                        {'required_intermediates': (self.middle, self.middle)},
                        {'python_max_exclusive': (3, 11)}):
            with self.subTest(changes=changes):
                old = self.requirements[self.target]
                self.require(self.target, **changes)
                self.assertEqual(self.evaluate().status, 'unverified')
                self.requirements[self.target] = old

    def test_duplicate_release_cannot_be_planned(self):
        self.releases.append(self.release(self.target))
        self.assertEqual(self.evaluate().reason, 'release_unavailable')

    def test_same_version_and_downgrades(self):
        self.assertEqual(self.evaluate(target=self.old).status, 'current')
        self.assertEqual(self.evaluate(target=self.old, installed=self.target).status, 'downgrade')


if __name__ == '__main__':
    unittest.main()
