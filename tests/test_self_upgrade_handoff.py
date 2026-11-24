"""Offline tests for the independent production self-upgrade helper."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops import self_upgrade_handoff as handoff
from netbox_ops import self_upgrade_helper as helper
from netbox_ops.self_upgrade_compatibility import LocalEnvironment, UpgradeDecision
from netbox_ops.self_upgrade_installation import InstalledRelease
from netbox_ops.self_upgrade_prepare import PreparedRelease
from netbox_ops.self_upgrade_releases import OfficialRelease, ReleaseAsset, Version
from netbox_ops.self_upgrade_transaction import SwapError, begin_swap, finish_swap


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name).resolve()
        self.active = self.parent / 'netbox-ops'
        self.active.mkdir(mode=0o755)
        self.active.chmod(0o755)
        package = self.active / 'netbox_ops'
        package.mkdir()
        (package / '__init__.py').write_text('__version__ = "0.5.0"\n')
        self.stage = self.parent / '.netbox-ops-stage-test'
        self.stage.mkdir(mode=0o700)
        (self.stage / 'version').write_text('new')
        self.old = Version(0, 5, 0)
        self.new = Version(0, 6, 0)
        self.target = Version(0, 7, 0)
        environment = LocalEnvironment((3, 12), 'debian', 'archive')
        self.python = Path(sys._base_executable).resolve()
        self.local = InstalledRelease(self.old, environment, self.python,
                                      handoff._identity(self.active))
        asset = ReleaseAsset('test', 'test', 1)
        self.release = OfficialRelease(self.new, 'v0.6.0', '', '2026-11-24', asset, asset)
        self.prepared = PreparedRelease(self.stage, self.local, self.target,
                                        (self.new, self.target), self.release,
                                        handoff._identity(self.stage))
        self.bundle = handoff._handoff(self.active)
        self.backup = self.parent / '.netbox-ops-backup'
        self.state = self.parent / '.netbox-ops-transaction.json'
        self.lock_path = self.parent / 'lock'
        self.audit_path = self.parent / 'audit'
        self.start_patch('netbox_ops.self_upgrade_handoff.ACTIVE', self.active)
        self.start_patch('netbox_ops.self_upgrade_handoff.LOCK_PATH', self.lock_path)
        self.start_patch('netbox_ops.self_upgrade_handoff.LOG_PATH', self.audit_path)
        self.inspect = self.start_patch('netbox_ops.self_upgrade_handoff.inspect_installation',
                                        return_value=self.local)
        self.layout = self.start_patch('netbox_ops.self_upgrade_handoff.verify_candidate_layout')
        self.runtime = self.start_patch('netbox_ops.self_upgrade_handoff.validate_installation_runtime')
        self.catalogue = MagicMock()
        self.catalogue.releases = (self.release,)
        self.catalogue.decision_for.return_value = UpgradeDecision('compatible', 'verified_plan',
                                                                  self.prepared.steps)
        self.start_patch('netbox_ops.self_upgrade_handoff.discover_compatibility',
                         return_value=self.catalogue)

    def start_patch(self, name, *args, **kwargs):
        p = patch(name, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def build(self):
        self.assertEqual(handoff._build_bundle(self.prepared, self.active, self.python), self.bundle)
        return handoff._read_record(self.bundle)

    def test_bundle_is_private_and_independent_of_active_tree(self):
        record = self.build()
        self.assertEqual(record['identity'], list(self.prepared.identity))
        self.assertEqual(record['steps'], ['v0.6.0', 'v0.7.0'])
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.bundle / 'request.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.bundle / 'recover').stat().st_mode & 0o777, 0o700)
        self.active.rename(self.backup)
        self.assertTrue((self.bundle / 'netbox_ops/__init__.py').is_file())
        self.assertEqual(handoff._read_record(self.bundle), record)

    def test_bundle_refuses_existing_handoff_and_pending_transaction(self):
        self.build()
        with self.assertRaisesRegex(SwapError, 'recovery first'):
            self.build()
        shutil.rmtree(self.bundle)
        self.state.write_text('pending')
        with self.assertRaisesRegex(SwapError, 'recovery first'):
            self.build()

    def test_bundle_rejects_stale_installed_identity(self):
        self.inspect.return_value = replace(self.local, identity=(1, 2))
        with self.assertRaisesRegex(SwapError, 'changed since preparation'):
            self.build()
        self.assertFalse(self.bundle.exists())

    def test_record_rejects_traversal_and_bad_permissions(self):
        record = self.build()
        path = self.bundle / 'request.json'
        path.write_text(json.dumps({**record, 'stage': '../netbox'}))
        with self.assertRaises(SwapError):
            handoff._read_record(self.bundle)
        path.write_text(json.dumps(record))
        path.chmod(0o666)
        with self.assertRaises(SwapError):
            handoff._read_record(self.bundle)

    def test_preflight_rechecks_compatibility_and_content(self):
        record = self.build()
        handoff._preflight(self.active, self.stage, record)
        self.runtime.assert_called_once_with(self.stage, self.new)
        self.catalogue.decision_for.assert_called_once_with(self.target)
        self.runtime.reset_mock()
        (self.stage / 'version').write_text('changed')
        with self.assertRaisesRegex(SwapError, 'content changed'):
            handoff._preflight(self.active, self.stage, record)
        self.runtime.assert_not_called()

    def test_preflight_rejects_changed_plan_or_release(self):
        record = self.build()
        self.catalogue.decision_for.return_value = UpgradeDecision(
            'compatible', 'verified_plan', (self.target,))
        with self.assertRaisesRegex(SwapError, 'compatibility changed'):
            handoff._preflight(self.active, self.stage, record)
        self.catalogue.decision_for.return_value = UpgradeDecision(
            'compatible', 'verified_plan', self.prepared.steps)
        self.catalogue.releases = (replace(self.release, archive=ReleaseAsset('x', 'x', 2)),)
        with self.assertRaisesRegex(SwapError, 'compatibility changed'):
            handoff._preflight(self.active, self.stage, record)
        self.runtime.assert_not_called()

    def test_preflight_rejects_changed_installed_identity(self):
        record = self.build()
        self.inspect.return_value = replace(self.local, identity=(1, 2))
        with self.assertRaisesRegex(SwapError, 'changed after handoff'):
            handoff._preflight(self.active, self.stage, record)
        self.catalogue.decision_for.assert_not_called()

    def test_validation_checks_new_active_and_restores_original_mode(self):
        record = self.build()
        begin_swap(self.active, self.stage, self.backup, self.state)
        self.inspect.return_value = replace(self.local, version=self.new,
                                            identity=self.prepared.identity)
        handoff._validate(self.active, record)
        self.assertEqual(self.active.stat().st_mode & 0o777, 0o755)
        self.assertTrue(self.backup.is_dir())
        self.runtime.assert_called_once_with(self.active, self.new)

    def test_cleanup_refuses_pending_transaction_or_replaced_stage(self):
        record = self.build()
        begin_swap(self.active, self.stage, self.backup, self.state)
        with self.assertRaisesRegex(SwapError, 'recovery'):
            handoff._clear_handoff(self.active, self.bundle, record)
        from netbox_ops.self_upgrade_transaction import recover_swap
        recover_swap(self.active)
        self.stage.rename(self.parent / 'saved')
        self.stage.mkdir()
        with self.assertRaisesRegex(SwapError, 'identity changed'):
            handoff._clear_handoff(self.active, self.bundle, record)
        self.assertTrue(self.stage.exists())
        self.assertTrue(self.bundle.exists())

    def test_launch_passes_lock_and_pidfd_to_independent_interpreter(self):
        lock = MagicMock()
        lock.__enter__.return_value.fd = 42
        self.build()
        with (patch.object(handoff, '_build_bundle', return_value=self.bundle),
              patch.object(handoff, '_root'), patch.object(handoff, 'ExclusiveLock', return_value=lock),
              patch.object(handoff, '_trusted_python', return_value=self.python),
              patch.object(handoff, 'open_initiator_pidfd', return_value=43),
              patch.object(handoff, 'write_audit'),
              patch.object(handoff.subprocess, 'Popen', return_value=MagicMock(pid=123)) as launch,
              patch.object(handoff.os, 'close') as close):
            self.assertEqual(handoff.launch_prepared_release(self.prepared), 123)
            launch.assert_called_once()
            args, kwargs = launch.call_args
            self.assertEqual(args[0], [str(self.python), '-I', '-B',
                                      str(self.bundle / 'runner.py'), 'apply', '43', '42'])
            self.assertEqual(kwargs['pass_fds'], (43, 42))
            self.assertTrue(kwargs['start_new_session'])
            close.assert_any_call(43)
        self.assertTrue(self.active.exists())
        self.assertTrue(self.stage.exists())

    def test_recovery_from_missing_active_removes_only_bound_candidate(self):
        record = self.build()
        # A crash after moving the old installation, before activating the new one.
        original = os.rename

        def interrupt(source, destination):
            if source == self.stage:
                raise KeyboardInterrupt
            return original(source, destination)

        with patch('netbox_ops.self_upgrade_transaction.os.rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                begin_swap(self.active, self.stage, self.backup, self.state)
        self.assertFalse(self.active.exists())
        self.assertEqual(handoff._read_record(self.bundle), record)
        lock = MagicMock()
        lock.__enter__.return_value.fd = 42
        with (patch.object(handoff, 'ExclusiveLock', return_value=lock),
              patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'operation_lock', return_value=MagicMock()),
              patch.object(helper, 'write_audit')):
            self.assertEqual(handoff._recover(self.bundle), 'rollback')
        self.assertTrue((self.active / 'netbox_ops/__init__.py').is_file())
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.bundle.exists())

    def test_recovery_after_validation_does_not_roll_back(self):
        self.build()
        begin_swap(self.active, self.stage, self.backup, self.state)
        finish_swap(self.active, self.stage, self.backup, self.state)
        lock = MagicMock()
        lock.__enter__.return_value.fd = 42
        with (patch.object(handoff, 'ExclusiveLock', return_value=lock),
              patch.object(helper.os, 'geteuid', return_value=0),
              patch.object(helper, 'operation_lock', return_value=MagicMock()),
              patch.object(helper, 'write_audit')):
            self.assertEqual(handoff._recover(self.bundle), 'cleanup')
        self.assertEqual((self.active / 'version').read_text(), 'new')
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.bundle.exists())


@unittest.skipUnless(os.geteuid() == 0, 'root-owned isolated lock required')
class InheritedLockTests(unittest.TestCase):
    def test_real_child_retains_flock_when_parent_descriptor_closes(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'lock'
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                source = (
                    'import sys; from pathlib import Path; '
                    'from netbox_ops.self_upgrade_helper import operation_lock; '
                    'c=operation_lock(Path(sys.argv[1]), int(sys.argv[2])); '
                    'c.__enter__(); print("ready", flush=True); sys.stdin.read(); c.__exit__(None,None,None)'
                )
                with subprocess.Popen([sys.executable, '-c', source, str(path), str(descriptor)],
                                      pass_fds=(descriptor,), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True) as child:
                    try:
                        self.assertEqual(child.stdout.readline().strip(), 'ready')
                        os.close(descriptor)
                        descriptor = None
                        other = os.open(path, os.O_RDWR)
                        try:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            child.communicate(timeout=5)
                            self.assertEqual(child.returncode, 0)
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        finally:
                            os.close(other)
                    finally:
                        if child.poll() is None:
                            child.kill()
            finally:
                if descriptor is not None:
                    os.close(descriptor)


if __name__ == '__main__':
    unittest.main()
