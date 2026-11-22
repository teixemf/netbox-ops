"""Ensure release metadata stays in sync with the package version."""

from pathlib import Path
import unittest

from netbox_ops import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_readme_version_matches_package(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"**Current version: {__version__} —", readme)

    def test_changelog_has_current_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{__version__}] —", changelog)


if __name__ == "__main__":
    unittest.main()
