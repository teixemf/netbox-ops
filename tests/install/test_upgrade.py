"""Installer Python checks run in temporary directories without touching production."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


UPGRADE = Path(__file__).resolve().parents[2] / 'upgrade.sh'


class PythonPreflightTests(unittest.TestCase):
    def bash(self, commands: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['bash', '-c', f'source "{UPGRADE}"\n' + commands],
            capture_output=True, text=True, env={**os.environ, **env},
        )

    @staticmethod
    def fake_python(path: Path, version: str) -> None:
        path.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-c" ]; then\n'
            f'    echo "{version}"\n'
            '    exit 0\n'
            'fi\n'
            'exit 1\n', encoding='utf-8'
        )
        path.chmod(0o755)

    def test_compatible_and_incompatible_python_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / 'python'
            for version, accepted in [('3.11', False), ('3.12', True),
                                      ('3.13', True), ('3.14', True), ('3.15', True), ('4.0', False)]:
                with self.subTest(version=version):
                    self.fake_python(interpreter, version)
                    result = self.bash(f'python_is_supported "{interpreter}"')
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_explicit_python_bin_is_used(self):
        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / 'custom-python'
            self.fake_python(interpreter, '3.13')
            result = self.bash('select_python\nprintf "%s\\n" "$PYTHON"', PYTHON_BIN=str(interpreter))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(interpreter))

    def test_invalid_explicit_python_bin_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / 'custom-python'
            self.fake_python(interpreter, '3.11')
            result = self.bash('select_python', PYTHON_BIN=str(interpreter))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Python 3.12 or newer', result.stderr)


    def test_detects_versioned_python_in_path(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            interpreter = directory / 'python3.15'
            self.fake_python(interpreter, '3.15')
            result = self.bash(
                'select_python\nprintf "%s\\n" "$PYTHON"',
                PATH=str(directory) + os.pathsep + os.environ['PATH'],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(interpreter))

    def test_explicit_python_bin_overrides_path_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            interpreter = directory / 'custom-python'
            self.fake_python(interpreter, '3.12')
            (directory / 'python3.15').write_text(
                '#!/bin/sh\necho 3.15\n', encoding='utf-8')
            (directory / 'python3.15').chmod(0o755)
            result = self.bash(
                'select_python\nprintf "%s\\n" "$PYTHON"',
                PYTHON_BIN=str(interpreter),
                PATH=str(directory) + os.pathsep + os.environ['PATH'],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(interpreter))

    def test_missing_venv_does_not_touch_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = root / 'python'
            self.fake_python(interpreter, '3.12')
            existing = root / 'venv'
            existing.mkdir()
            (existing / 'keep-me').write_text('preserved', encoding='utf-8')
            result = self.bash(
                f'ROOT="{root}"\nPYTHON="{interpreter}"\n'
                'trap cleanup_preflight EXIT\npreflight_python',
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('could not create a temporary virtual environment', result.stderr)
            self.assertEqual((existing / 'keep-me').read_text(), 'preserved')
            self.assertEqual(list(root.glob('.venv-check.*')), [])

    def test_pip_failure_does_not_touch_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = root / 'python'
            interpreter.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
                '    mkdir -p "$3/bin"\n'
                '    printf "#!/bin/sh\\nexit 1\\n" > "$3/bin/python"\n'
                '    chmod +x "$3/bin/python"\n'
                '    exit 0\n'
                'fi\n'
                'exit 1\n', encoding='utf-8'
            )
            interpreter.chmod(0o755)
            existing = root / 'venv'
            existing.mkdir()
            (existing / 'keep-me').write_text('preserved', encoding='utf-8')
            result = self.bash(
                f'ROOT="{root}"\nPYTHON="{interpreter}"\n'
                'trap cleanup_preflight EXIT\npreflight_python',
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('pip is unavailable', result.stderr)
            self.assertEqual((existing / 'keep-me').read_text(), 'preserved')
            self.assertEqual(list(root.glob('.venv-check.*')), [])

    @unittest.skipUnless(os.geteuid() == 0, 'Full upgrade check requires root')
    def test_upgrade_fails_safely_before_deleting_existing_venv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'upgrade.sh'
            shutil.copyfile(UPGRADE, script)
            (root / 'requirements.txt').write_text('', encoding='utf-8')
            (root / 'local_requirements.txt').write_text('', encoding='utf-8')
            interpreter = root / 'python'
            self.fake_python(interpreter, '3.12')
            existing = root / 'venv'
            existing.mkdir()
            (existing / 'keep-me').write_text('preserved', encoding='utf-8')
            result = subprocess.run(
                ['bash', str(script)], capture_output=True, text=True,
                env={**os.environ, 'PYTHON_BIN': str(interpreter)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('could not create a temporary virtual environment', result.stderr)
            self.assertEqual((existing / 'keep-me').read_text(), 'preserved')

    @unittest.skipUnless(os.geteuid() == 0 and sys.version_info >= (3, 12),
                         'Full isolated upgrade requires root and Python 3.12+')
    def test_full_upgrade_rebuilds_sandbox_venv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'upgrade.sh'
            shutil.copyfile(UPGRADE, script)
            (root / 'requirements.txt').write_text('', encoding='utf-8')
            (root / 'local_requirements.txt').write_text('', encoding='utf-8')
            (root / 'netbox_ops').mkdir()
            (root / 'netbox_ops' / '__init__.py').write_text('', encoding='utf-8')
            (root / 'netbox_ops.py').write_text('print("test-version")\n', encoding='utf-8')
            old = root / 'venv'
            old.mkdir()
            (old / 'keep-me').write_text('old', encoding='utf-8')
            result = subprocess.run(
                ['bash', str(script)], capture_output=True, text=True,
                env={**os.environ, 'PYTHON_BIN': sys.executable},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('test-version', result.stdout)
            self.assertFalse((old / 'keep-me').exists())
            self.assertTrue((old / 'bin' / 'python').exists())
            self.assertEqual(list(root.glob('.venv-check.*')), [])

    @unittest.skipUnless(sys.version_info >= (3, 12), 'Requires Python 3.12+')
    def test_real_python_can_create_venv_with_pip_without_touching_existing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / 'venv'
            existing.mkdir()
            (existing / 'keep-me').write_text('preserved', encoding='utf-8')
            result = self.bash(
                f'ROOT="{root}"\nPYTHON="{sys.executable}"\n'
                'trap cleanup_preflight EXIT\npreflight_python',
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((existing / 'keep-me').read_text(), 'preserved')
            self.assertEqual(list(root.glob('.venv-check.*')), [])


if __name__ == '__main__':
    unittest.main()
