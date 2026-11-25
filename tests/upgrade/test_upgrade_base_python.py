"""Exercise the base Python safety checks without touching the host interpreter."""
import stat
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_stage import resolve_base_python


class BasePythonTests(unittest.TestCase):
    def setUp(self):
        root = Path('/opt/netbox')
        self.installation = SimpleNamespace(
            root=root, python=root / 'venv/bin/python', python_version='3.12.1',
        )
        self.selected = Path('/usr/bin/python3.12')
        self.probe = SimpleNamespace(returncode=0, stdout=str(self.selected) + '\n')
        self.version = SimpleNamespace(returncode=0, stdout='3.12.1\n')

    def resolve(self, *, probe=None, version=None, selected=None,
                file_mode=stat.S_IFREG | 0o755, file_uid=0,
                parent_mode=stat.S_IFDIR | 0o755, parent_uid=0,
                executable=True, resolve_error=None):
        if selected is None:
            selected = self.selected
        runner = Mock(side_effect=[
            self.probe if probe is None else probe,
            self.version if version is None else version,
        ])

        def metadata(path):
            if path == selected:
                return SimpleNamespace(st_mode=file_mode, st_uid=file_uid)
            return SimpleNamespace(st_mode=parent_mode, st_uid=parent_uid)

        with (
            patch.object(Path, 'resolve', return_value=selected, side_effect=resolve_error),
            patch.object(Path, 'stat', autospec=True, side_effect=metadata),
            patch('netbox_ops.upgrade_stage.os.access', return_value=executable),
        ):
            return resolve_base_python(self.installation, runner=runner)

    def test_accepts_verified_matching_interpreter(self):
        self.assertEqual(self.resolve(), self.selected)

    def test_rejects_unsafe_probe_results(self):
        for result in (
            SimpleNamespace(returncode=1, stdout=str(self.selected)),
            SimpleNamespace(returncode=0, stdout='python3.12'),
            SimpleNamespace(returncode=0, stdout='/usr/bin/python;id'),
            SimpleNamespace(returncode=0, stdout='/usr/bin/python with spaces'),
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(UpgradeExecutionError, 'matching base Python'):
                    self.resolve(probe=result)

    def test_rejects_unsafe_resolved_interpreter(self):
        for selected in (
            self.installation.root / 'venv/bin/python',
            Path('/usr/bin/python with spaces'),
        ):
            with self.subTest(selected=selected):
                with self.assertRaises(UpgradeExecutionError):
                    self.resolve(selected=selected)

    def test_rejects_unsafe_metadata(self):
        for kwargs in (
            {'file_mode': stat.S_IFDIR | 0o755},
            {'file_uid': 1000},
            {'file_mode': stat.S_IFREG | 0o775},
            {'file_mode': stat.S_IFREG | 0o757},
            {'parent_uid': 1000},
            {'parent_mode': stat.S_IFDIR | 0o777},
            {'executable': False},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(UpgradeExecutionError):
                    self.resolve(**kwargs)

    def test_rejects_missing_interpreter(self):
        with self.assertRaises(UpgradeExecutionError):
            self.resolve(resolve_error=FileNotFoundError())

    def test_rejects_failed_or_mismatched_version(self):
        for result in (
            SimpleNamespace(returncode=1, stdout='3.12.1'),
            SimpleNamespace(returncode=0, stdout='3.13.1'),
        ):
            with self.subTest(result=result):
                with self.assertRaises(UpgradeExecutionError):
                    self.resolve(version=result)

    def test_probe_timeout_is_redacted(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired('private-command', 15))
        with self.assertRaises(UpgradeExecutionError) as caught:
            resolve_base_python(self.installation, runner=runner)
        self.assertNotIn('private-command', str(caught.exception))
