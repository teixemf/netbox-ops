"""Mock package managers; Python package installation is never performed by tests."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALL = Path(__file__).resolve().parents[2] / 'install.sh'


class PythonPackageInstallTests(unittest.TestCase):
    def run_bash(self, bash: str, directory: Path, *, available: bool = True,
                 dnf_available: bool = True) -> subprocess.CompletedProcess[str]:
        bin_dir = directory / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'apt-cache').write_text(
            '#!/bin/bash\n'
            'if [[ "$1" == policy ]]; then\n'
            '  if [[ "$2" == *-venv && "$APT_VENV_AVAILABLE" == 0 ]]; then\n'
            '    printf "Candidate: (none)\\n"\n'
            '  else\n'
            '    printf "Candidate: 3.12.3-1\\n"\n'
            '  fi\n'
            'fi\n', encoding='utf-8')
        (bin_dir / 'apt-get').write_text(
            '#!/bin/bash\nprintf "apt-get %s\\n" "$*" >> "$PACKAGE_LOG"\n',
            encoding='utf-8')
        (bin_dir / 'dnf').write_text(
            '#!/bin/bash\n'
            'if [[ "$1" == -q ]]; then\n'
            '  [[ "$DNF_PACKAGES_AVAILABLE" == 1 ]] && printf "python3.12-3.12.3.x86_64\\n"\n'
            'else\n'
            '  printf "dnf %s\\n" "$*" >> "$PACKAGE_LOG"\n'
            'fi\n', encoding='utf-8')
        for executable in bin_dir.iterdir():
            executable.chmod(0o755)
        return subprocess.run(
            ['bash', '-c', f'source "{INSTALL}"\n' + bash],
            text=True, capture_output=True,
            env={**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                 'PACKAGE_LOG': str(directory / 'calls.log'),
                 'APT_VENV_AVAILABLE': str(int(available)),
                 'DNF_PACKAGES_AVAILABLE': str(int(dnf_available))},
        )

    def test_apt_confirmed_install_uses_only_configured_package_manager(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { return 0; }\n'
                'install_python_if_requested debian', directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (directory / 'calls.log').read_text(encoding='utf-8')
            self.assertIn('apt-get --no-remove install --yes --no-install-recommends python3.12 python3.12-venv', calls)
            self.assertNotIn(' update ', calls)

    def test_declining_apt_install_leaves_system_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { return 1; }\n'
                'install_python_if_requested debian', directory,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((directory / 'calls.log').exists())

    def test_apt_package_missing_does_not_prompt_or_install(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { echo UNEXPECTED_PROMPT; return 0; }\n'
                'install_python_if_requested debian', directory, available=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('UNEXPECTED_PROMPT', result.stdout)
            self.assertIn('No compatible Python packages', result.stderr)
            self.assertFalse((directory / 'calls.log').exists())

    def test_dnf_confirmed_install(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { return 0; }\n'
                'install_python_if_requested rhel', directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                'dnf -y --setopt=install_weak_deps=False install python3.12 python3.12-pip',
                (directory / 'calls.log').read_text(encoding='utf-8'),
            )

    def test_dnf_unavailable_does_not_prompt_or_install(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { echo UNEXPECTED_PROMPT; return 0; }\n'
                'install_python_if_requested rhel', directory, dnf_available=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('UNEXPECTED_PROMPT', result.stdout)
            self.assertFalse((directory / 'calls.log').exists())


    def test_apt_install_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash(
                'confirm_python_install() { return 0; }\\n'
                'apt-get() { return 9; }\\n'
                'install_python_if_requested debian', directory,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn('Python package installation failed', result.stderr)
            self.assertFalse((directory / 'calls.log').exists())

    def test_unsupported_distribution_does_not_install_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self.run_bash('install_python_if_requested unknown', directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Cannot offer automatic Python installation', result.stderr)
            self.assertFalse((directory / 'calls.log').exists())

    def test_confirmation_decline_has_distinct_status(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_bash(
                'PYTHON_PACKAGE_VERSION=3.12; '
                'confirm_python_install() { return 2; }; '
                'install_python_if_requested debian',
                Path(temp),
            )
            self.assertEqual(result.returncode, 2)

    def test_confirmation_requires_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_bash('confirm_python_install', Path(temp))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('interactive terminal', result.stderr)


if __name__ == '__main__':
    unittest.main()
