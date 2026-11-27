"""Safe plugin inventory tests with a disposable filesystem fixture."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.plugin_inventory import (
    PluginInventoryError, apply_approved_pins, configured_plugin_packages, inventory_plugins,
)
from netbox_ops.upgrade_discovery import GitNetBoxInstallation


class PluginInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'netbox'
        self.root.mkdir()
        (self.root / 'netbox/netbox').mkdir(parents=True)
        (self.root / 'netbox/netbox/configuration.py').write_text("PLUGINS = ['sample_plugin']\n")
        self.requirements = self.root / 'local_requirements.txt'
        self.requirements.write_text('sample-plugin  # keep this comment\nother-package>=2\n')
        self.requirements.chmod(0o640)
        self.add_package('sample-plugin', '2.4.1', 'sample_plugin')
        self.installation = GitNetBoxInstallation(self.root, '4.5.0', 'a' * 40,
                                                  'v4.5.0', 'HEAD',
                                                  'https://github.com/netbox-community/netbox.git',
                                                  self.root / 'venv/bin/python', '3.12.3', (), ())

    def add_package(self, name: str, version: str, module: str):
        directory = self.root / 'venv/lib/python3.12/site-packages' / f'{name}-{version}.dist-info'
        directory.mkdir(parents=True)
        (directory / 'METADATA').write_text(f'Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n')
        (directory / 'top_level.txt').write_text(f'{module}\n')

    def inventory(self):
        return inventory_plugins(self.installation)

    def test_inventory_distinguishes_declared_installed_enabled(self):
        result = self.inventory()
        self.assertEqual(result.enabled, ('sample_plugin',))
        self.assertEqual(result.packages[0].name, 'sample-plugin')
        self.assertEqual(result.requirements[0].kind, 'bare')
        self.assertEqual(result.requirements[1].kind, 'ambiguous')
        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(result.proposals[0].after, 'sample-plugin==2.4.1  # keep this comment\n')

    def test_wheel_record_identifies_plugin_without_top_level_metadata(self):
        directory = self.root / 'venv/lib/python3.12/site-packages/sample-plugin-2.4.1.dist-info'
        (directory / 'top_level.txt').unlink()
        (directory / 'RECORD').write_text(
            'sample_plugin/__init__.py,,\n../../../bin/sample_plugin,,\n')
        result = self.inventory()
        self.assertEqual(configured_plugin_packages(result), (('sample-plugin', '2.4.1'),))
        self.assertEqual(len(result.proposals), 1)

    def test_record_data_and_dotted_filenames_do_not_prove_module_ownership(self):
        directory = self.root / 'venv/lib/python3.12/site-packages/sample-plugin-2.4.1.dist-info'
        (directory / 'top_level.txt').unlink()
        (directory / 'RECORD').write_text(
            'sample_plugin.data.py,,\nsample_plugin/data/example.py,,\n'
            '../../../sample_plugin/__init__.py,,\n')
        result = self.inventory()
        self.assertEqual(configured_plugin_packages(result), ())
        self.assertEqual(result.proposals, ())

    def test_matching_name_without_module_metadata_is_not_evidence(self):
        directory = self.root / 'venv/lib/python3.12/site-packages/sample-plugin-2.4.1.dist-info'
        (directory / 'top_level.txt').unlink()
        result = self.inventory()
        self.assertEqual(configured_plugin_packages(result), ())
        self.assertEqual(result.proposals, ())
        self.assertTrue(result.warnings)

    def test_ambiguous_module_ownership_is_not_eligible(self):
        self.add_package('another-package', '1.0.0', 'sample_plugin')
        result = self.inventory()
        self.assertEqual(configured_plugin_packages(result), ())
        self.assertEqual(result.proposals, ())
        self.assertTrue(result.warnings)

    def test_comments_options_and_others_are_unchanged(self):
        text = '# header\n--extra-index-url https://example.invalid/simple\n-r extra.txt\nsample-plugin # plugin\nother>=2\n'
        self.requirements.write_text(text)
        result = self.inventory()
        proposal, = result.proposals
        self.assertEqual(proposal.after, 'sample-plugin==2.4.1 # plugin\n')
        self.assertEqual(self.requirements.read_text(), text)

    def test_missing_requirements_proposes_new_file(self):
        self.requirements.unlink()
        result = self.inventory()
        proposal, = result.proposals
        self.assertIsNone(proposal.line)
        self.assertEqual(proposal.after, 'sample-plugin==2.4.1\n')
        self.assertFalse(self.requirements.exists())

    def test_ambiguous_specifier_requires_manual_review(self):
        self.requirements.write_text('sample-plugin>=2.0; python_version >= "3.12"\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertTrue(any('complex' in w for w in result.warnings))

    def test_duplicate_declarations_not_rewritten(self):
        self.requirements.write_text('sample_plugin\nsample-plugin>=2.0\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertTrue(any('Duplicate' in w for w in result.warnings))

    def test_existing_pin_not_modified(self):
        self.requirements.write_text('sample-plugin==2.4.1  # keep\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertFalse(result.warnings)

    def test_conflicting_installed_version_reported(self):
        self.requirements.write_text('sample-plugin==2.3.0\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertTrue(any('differs' in w for w in result.warnings))

    def test_dynamic_plugin_setting_is_unknown(self):
        (self.root / 'netbox/netbox/configuration.py').write_text('PLUGINS = load_plugins()\n')
        result = self.inventory()
        self.assertIsNone(result.enabled)
        self.assertFalse(result.proposals)

    def test_plugin_append_is_unknown(self):
        (self.root / 'netbox/netbox/configuration.py').write_text("PLUGINS = ['sample_plugin']\nPLUGINS.append('another')\n")
        self.assertIsNone(self.inventory().enabled)

    def test_unrelated_unpinned_requirements_not_rewritten(self):
        (self.root / 'netbox/netbox/configuration.py').write_text('PLUGINS = []\n')
        self.assertEqual(self.inventory().proposals, ())

    def test_symlink_requirements_refused(self):
        self.requirements.unlink()
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('sample-plugin\n')
        self.requirements.symlink_to(outside)
        with self.assertRaisesRegex(PluginInventoryError, 'symlink'):
            self.inventory()
        self.assertEqual(outside.read_text(), 'sample-plugin\n')

    def test_lib64_symlink_does_not_duplicate_installed_package(self):
        (self.root / 'venv/lib64').symlink_to('lib')
        self.assertEqual(len(self.inventory().packages), 1)

    def _apply(self, result, selected=('sample-plugin',), approved=True):
        with patch('netbox_ops.plugin_inventory.inspect_git_installation', return_value=self.installation), \
             patch('netbox_ops.plugin_inventory.ExclusiveLock'), \
             patch('netbox_ops.plugin_inventory.write_audit'), \
             patch('netbox_ops.plugin_inventory._PIN_RECOVERY_DIRECTORY', self.root.parent / 'pin-recovery'):
            return apply_approved_pins(result, selected, approved=approved)

    def test_confirmation_required_without_writing(self):
        result = self.inventory()
        original = self.requirements.read_bytes()
        with self.assertRaisesRegex(PluginInventoryError, 'Explicit approval'):
            self._apply(result, approved=False)
        self.assertEqual(self.requirements.read_bytes(), original)

    def test_pending_recovery_blocks_pin_changes(self):
        result = self.inventory()
        original = self.requirements.read_bytes()
        with patch('netbox_ops.plugin_inventory.upgrade_recovery_pending', return_value=True):
            with self.assertRaisesRegex(PluginInventoryError, 'recovery is pending'):
                self._apply(result)
        self.assertEqual(self.requirements.read_bytes(), original)

    def test_approved_pin_replaces_only_selected_line(self):
        result = self.inventory()
        self._apply(result)
        self.assertEqual(self.requirements.read_text(),
                         'sample-plugin==2.4.1  # keep this comment\nother-package>=2\n')
        self.assertEqual(self.requirements.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.requirements.stat().st_uid, 0)

    def test_contextual_local_pin_preserves_extras_comments_and_protected_backup(self):
        self.add_package('local-extra', '2.1.0', 'local_extra')
        (self.root / 'requirements.txt').write_text('Django==5.2.1\n')
        original = b'sample-plugin==2.4.1\r\nlocal-extra[foo]  # preserve\r\n'
        self.requirements.write_bytes(original)
        self.assertFalse(self.inventory().proposals)
        inventory = inventory_plugins(self.installation, local_pin_names=('local-extra',))
        self.assertEqual(configured_plugin_packages(inventory), (('sample-plugin', '2.4.1'),))
        self.assertEqual(len(inventory.proposals), 1)
        self.assertEqual(inventory.proposals[0].after, 'local-extra[foo]==2.1.0  # preserve\r\n')
        self.assertEqual(self.requirements.read_bytes(), original)
        self._apply(inventory, ('local-extra',))
        self.assertEqual(self.requirements.read_bytes(),
                         b'sample-plugin==2.4.1\r\nlocal-extra[foo]==2.1.0  # preserve\r\n')
        backup, = (self.root.parent / 'pin-recovery').glob('pins-*/local_requirements.txt')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.requirements.stat().st_mode & 0o777, 0o640)

    def test_contextual_local_pin_refuses_overlap_and_complex_file(self):
        self.add_package('local-extra', '2.1.0', 'local_extra')
        self.requirements.write_text('sample-plugin==2.4.1\nlocal-extra\n')
        core = self.root / 'requirements.txt'
        core.write_text('local-extra==2.1.0\n')
        inventory = inventory_plugins(self.installation, local_pin_names=('local-extra',))
        self.assertFalse(inventory.proposals)
        core.write_text('Django==5.2.1\n')
        self.requirements.write_text('sample-plugin==2.4.1\nlocal-extra\n-r other.txt\n')
        inventory = inventory_plugins(self.installation, local_pin_names=('local-extra',))
        self.assertFalse(inventory.proposals)

    def test_backup_failure_prevents_pin_replacement(self):
        inventory = self.inventory()
        original = self.requirements.read_bytes()
        with patch('netbox_ops.plugin_inventory._save_pin_backup',
                   side_effect=PluginInventoryError('Cannot save protected original requirements')):
            with self.assertRaisesRegex(PluginInventoryError, 'Cannot save'):
                self._apply(inventory)
        self.assertEqual(self.requirements.read_bytes(), original)

    def test_unsafe_existing_permissions_are_not_rewritten(self):
        self.requirements.chmod(0o666)
        original = self.requirements.read_bytes()
        result = self.inventory()
        with self.assertRaisesRegex(PluginInventoryError, 'root-controlled'):
            self._apply(result)
        self.assertEqual(self.requirements.read_bytes(), original)
        self.assertEqual(self.requirements.stat().st_mode & 0o777, 0o666)

    def test_approved_append_creates_pinned_file(self):
        self.requirements.unlink()
        result = self.inventory()
        self._apply(result)
        self.assertEqual(self.requirements.read_text(), 'sample-plugin==2.4.1\n')
        self.assertEqual(self.requirements.stat().st_mode & 0o777, 0o600)

    def test_changed_requirements_cannot_be_overwritten(self):
        result = self.inventory()
        self.requirements.write_text('sample-plugin==3.0.0\n')
        with self.assertRaisesRegex(PluginInventoryError, 'changed'):
            self._apply(result)
        self.assertEqual(self.requirements.read_text(), 'sample-plugin==3.0.0\n')

    def test_installed_metadata_drift_blocks_approval(self):
        result = self.inventory()
        metadata = next((self.root / 'venv/lib/python3.12/site-packages').glob('*.dist-info/METADATA'))
        metadata.write_text('Metadata-Version: 2.3\nName: sample-plugin\nVersion: 2.5.0\n')
        with self.assertRaisesRegex(PluginInventoryError, 'inventory changed'):
            self._apply(result)

    def test_vcs_egg_never_gets_an_automatic_duplicate_pin(self):
        self.requirements.write_text('git+https://example.invalid/repo#egg=sample-plugin\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertTrue(any('VCS' in warning for warning in result.warnings))

    def test_unknown_include_blocks_appending_new_pin(self):
        self.requirements.write_text('-r extras.txt\n')
        result = self.inventory()
        self.assertEqual(result.proposals, ())
        self.assertTrue(any('requirements sources' in warning for warning in result.warnings))

    def test_unproposed_pin_cannot_be_selected(self):
        result = self.inventory()
        with self.assertRaisesRegex(PluginInventoryError, 'Only unambiguous'):
            self._apply(result, selected=('other-package',))

    def test_crlf_is_preserved(self):
        self.requirements.write_bytes(b'sample-plugin # comment\r\nother-package>=2\r\n')
        result = self.inventory()
        self._apply(result)
        self.assertEqual(self.requirements.read_bytes(),
                         b'sample-plugin==2.4.1 # comment\r\nother-package>=2\r\n')


if __name__ == '__main__':
    unittest.main()
