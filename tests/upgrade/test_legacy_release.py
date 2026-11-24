"""Version detection must never import a NetBox configuration."""
import tempfile
import unittest
from pathlib import Path

from netbox_ops.netbox_version import read_netbox_version


class LegacyReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.release = Path(temporary.name) / 'release.yaml'
        self.settings = self.release.parent / 'netbox/settings.py'
        self.settings.parent.mkdir()

    def test_reads_literal_old_version_without_execution(self):
        self.settings.write_text("raise RuntimeError('never execute this')\nVERSION = '3.7.8'\n")
        self.assertEqual(read_netbox_version(self.release), '3.7.8')

    def test_yaml_remains_authoritative(self):
        self.settings.write_text("VERSION = '3.7.8'\n")
        self.release.write_text('version: "4.7.3"\n')
        self.assertEqual(read_netbox_version(self.release), '4.7.3')
        self.release.write_text('invalid yaml')
        self.assertEqual(read_netbox_version(self.release), '')

    def test_ambiguous_or_nonliteral_assignments_are_rejected(self):
        for code in ("VERSION = '3.7.8'\nVERSION = '4.0.0'",
                     "VERSION = '3.7.8'\nif True:\n    VERSION = get_version()",
                     "VERSION = get_version()", "VERSION = '3.7.8-beta1'"):
            with self.subTest(code=code):
                self.settings.write_text(code)
                self.assertEqual(read_netbox_version(self.release), '')

    def test_symlinked_settings_not_followed(self):
        other = self.release.parent / 'outside.py'
        other.write_text("VERSION = '3.7.8'\n")
        self.settings.symlink_to(other)
        self.assertEqual(read_netbox_version(self.release), '')


if __name__ == '__main__':
    unittest.main()
