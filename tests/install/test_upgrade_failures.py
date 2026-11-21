"""Failure-path tests for an isolated copy of upgrade.sh."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


UPGRADE = Path(__file__).resolve().parents[2] / "upgrade.sh"


@unittest.skipUnless(os.geteuid() == 0, "Full upgrade script requires root")
class UpgradeFailureTests(unittest.TestCase):
    @staticmethod
    def prepare(root: Path) -> Path:
        script = root / "upgrade.sh"
        shutil.copyfile(UPGRADE, script)
        (root / "netbox_ops").mkdir()
        (root / "netbox_ops" / "__init__.py").write_text("", encoding="utf-8")
        (root / "netbox_ops.py").write_text("print('test-version')\n", encoding="utf-8")
        interpreter = root / "mock-python"
        interpreter.write_text(
            "#!/bin/bash\n"
            "if [[ ${1:-} == -c ]]; then\n"
            "    if [[ ${2:-} == *version_info* ]]; then echo 3.12; fi\n"
            "    exit 0\n"
            "fi\n"
            "if [[ ${1:-} == -m && ${2:-} == venv ]]; then\n"
            "    count=0\n"
            "    [[ -f $CALL_COUNT ]] && count=$(cat \"$CALL_COUNT\")\n"
            "    count=$((count + 1))\n"
            "    printf '%s' \"$count\" > \"$CALL_COUNT\"\n"
            "    if [[ ${FAIL_STAGE:-} == final_venv && $count -eq 2 ]]; then exit 9; fi\n"
            "    mkdir -p \"$4/bin\"\n"
            "    cp -- \"$0\" \"$4/bin/python\"\n"
            "    exit 0\n"
            "fi\n"
            "if [[ ${1:-} == -m && ${2:-} == pip ]]; then\n"
            "    touch \"$PIP_CALLED\"\n"
            "    exit 9\n"
            "fi\n"
            "if [[ ${1:-} == -m && ${2:-} == compileall ]]; then\n"
            "    [[ ${FAIL_STAGE:-} != compileall ]]\n"
            "    exit $?\n"
            "fi\n"
            "if [[ ${1:-} == *netbox_ops.py ]]; then\n"
            "    [[ ${FAIL_STAGE:-} != version ]] || exit 9\n"
            "    echo test-version\n"
            "    exit 0\n"
            "fi\n"
            "exit 9\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        return interpreter

    def run_upgrade(self, root: Path, interpreter: Path, stage: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(root / "upgrade.sh")],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHON_BIN": str(interpreter),
                "CALL_COUNT": str(root / "venv-calls"),
                "PIP_CALLED": str(root / "pip-called"),
                "FAIL_STAGE": stage,
            },
            timeout=30,
        )

    def test_no_dependency_files_or_pip_needed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = self.prepare(root)
            result = self.run_upgrade(root, interpreter)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Virtual environment rebuilt successfully.", result.stdout)
            self.assertFalse((root / "pip-called").exists())
            self.assertEqual((root / "venv-calls").read_text(), "2")
            self.assertFalse(list(root.glob(".venv-check.*")))

    def test_final_venv_creation_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = self.prepare(root)
            result = self.run_upgrade(root, interpreter, "final_venv")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Virtual environment rebuilt successfully.", result.stdout)
            self.assertFalse((root / "pip-called").exists())
            self.assertFalse(list(root.glob(".venv-check.*")))

    def test_post_creation_failures_are_reported(self):
        for stage in ("compileall", "version"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                interpreter = self.prepare(root)
                result = self.run_upgrade(root, interpreter, stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Virtual environment rebuilt successfully.", result.stdout)
                self.assertFalse((root / "pip-called").exists())
                self.assertFalse(list(root.glob(".venv-check.*")))

    def test_symlinked_venv_is_rejected_before_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = self.prepare(root)
            destination = root / "outside"
            destination.mkdir()
            sentinel = destination / "keep-me"
            sentinel.write_text("preserved", encoding="utf-8")
            (root / "venv").symlink_to(destination, target_is_directory=True)
            result = self.run_upgrade(root, interpreter)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid virtual environment path", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserved")
            self.assertFalse((root / "venv-calls").exists())


if __name__ == "__main__":
    unittest.main()
