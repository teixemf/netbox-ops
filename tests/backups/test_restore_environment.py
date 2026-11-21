"""Offline tests for service environment discovery and staged Django checks."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netbox_ops.config import Configuration, DEFAULT_UNITS
from netbox_ops.restore_environment import (
    DJANGO_STAGE_CHECK, NetBoxRuntime, RuntimeErrorSafe,
    _assignments, _environment_files, _read_environment_file,
    _running_service_environment, _stopped_service_environment,
    check_staged_migrations, netbox_runtime,
)


class ServiceEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = self.root / 'netbox'
        self.cwd.mkdir()
        (self.cwd / 'manage.py').write_text('# Test manage.py\n')
        self.bin = self.root / 'venv' / 'bin'
        self.bin.mkdir(parents=True)
        (self.bin / 'python').touch()
        self.cfg = Configuration({**DEFAULT_UNITS, 'netbox': 'custom-netbox.service'})

    def _unit(self, **overrides):
        values = {
            'User': 'netbox',
            'WorkingDirectory': str(self.cwd),
            'ExecStart': f'{{ path={self.bin / "gunicorn"} ; argv[]=/example ; }}',
            'MainPID': '0',
            'Environment': 'NETBOX_DB_PASSWORD=inline ENV_SETTING="two words"',
            'EnvironmentFiles': '',
            'UnsetEnvironment': '',
            'PassEnvironment': '',
            'PAMName': '',
        }
        values.update(overrides)
        return '\n'.join(f'{name}={value}' for name, value in values.items()) + '\n'

    def _runner(self, output):
        def run(argv, **kwargs):
            self.assertEqual(argv[-1], 'custom-netbox.service')
            self.assertIn('EnvironmentFiles', argv[3])
            self.assertTrue(kwargs['capture_output'])
            return subprocess.CompletedProcess(argv, 0, output, '')
        return run

    def test_service_environment_and_paths_are_discovered(self):
        runtime = netbox_runtime(self.cfg, runner=self._runner(self._unit()))
        self.assertEqual(runtime.username, 'netbox')
        self.assertEqual(runtime.python, self.bin / 'python')
        self.assertEqual(runtime.working_directory, self.cwd)
        self.assertEqual(runtime.environment['NETBOX_DB_PASSWORD'], 'inline')
        self.assertEqual(runtime.environment['ENV_SETTING'], 'two words')

    def test_environment_file_from_resolved_unit_and_drop_in_overrides_inline(self):
        envfile = self.root / 'my-netbox.env'
        envfile.write_text(
            '# test\nNETBOX_DB_PASSWORD="file password"\nNETBOX_CONFIGURATION=custom.settings\n'
        )
        unit = self._unit(
            EnvironmentFiles=f'{envfile} (ignore_errors=no)',
            UnsetEnvironment='ENV_SETTING',
        )
        runtime = netbox_runtime(self.cfg, runner=self._runner(unit))
        self.assertEqual(runtime.environment['NETBOX_DB_PASSWORD'], 'file password')
        self.assertEqual(runtime.environment['NETBOX_CONFIGURATION'], 'custom.settings')
        self.assertNotIn('ENV_SETTING', runtime.environment)

    def test_multiple_files_and_optional_missing_file(self):
        first = self.root / 'first.env'
        second = self.root / 'second.env'
        first.write_text('ONE=one\nKEY=old\n')
        second.write_text('KEY=new\n')
        settings = {
            'EnvironmentFiles': (f'{first} (ignore_errors=no) '
                                 f'{self.root / "missing.env"} (ignore_errors=yes) '
                                 f'{second} (ignore_errors=no)'),
        }
        self.assertEqual(_stopped_service_environment(settings), {'ONE': 'one', 'KEY': 'new'})

    def test_environment_file_preserves_plain_space_and_literal_dollar(self):
        config = self.root / 'secrets.env'
        config.write_text(
            'NETBOX_DB_PASSWORD=two words $NOT_EXPANDED\n'
            'NETBOX_TOKEN="quoted value with spaces"\n'
        )
        variables = _read_environment_file(config)
        self.assertEqual(variables['NETBOX_DB_PASSWORD'], 'two words $NOT_EXPANDED')
        self.assertEqual(variables['NETBOX_TOKEN'], 'quoted value with spaces')

    def test_running_service_environment_is_preferred(self):
        unit = self._unit(MainPID='4321', Environment='NETBOX_DB_PASSWORD=stale')
        with patch('netbox_ops.restore_environment._running_service_environment',
                   return_value={'NETBOX_DB_PASSWORD': 'active process'}) as get_env:
            runtime = netbox_runtime(self.cfg, runner=self._runner(unit))
        get_env.assert_called_once_with('4321')
        self.assertEqual(runtime.environment['NETBOX_DB_PASSWORD'], 'active process')

    def test_live_proc_environment_is_binary_null_separated(self):
        with patch('pathlib.Path.read_bytes',
                   return_value=b'NETBOX_DB_PASSWORD=secret\x00NETBOX_CONFIGURATION=plugins\x00'):
            values = _running_service_environment('1234')
        self.assertEqual(values['NETBOX_DB_PASSWORD'], 'secret')
        self.assertEqual(values['NETBOX_CONFIGURATION'], 'plugins')

    def test_invalid_or_unsupported_environment_fails_closed(self):
        for properties in (
            {'PassEnvironment': 'NETBOX_DB_PASSWORD'},
            {'PAMName': 'login'},
            {'EnvironmentFiles': '/not/real (ignore_errors=no)'},
            {'Environment': 'INVALID'},
        ):
            with self.subTest(properties=properties):
                with self.assertRaises(RuntimeErrorSafe):
                    _stopped_service_environment(properties)
        with self.assertRaises(RuntimeErrorSafe):
            _environment_files('unexpected-format')
        with self.assertRaises(RuntimeErrorSafe):
            _assignments('PASSWORD="unterminated')

    def test_service_user_and_runtime_must_be_valid(self):
        with self.assertRaisesRegex(RuntimeErrorSafe, 'non-root'):
            netbox_runtime(self.cfg, runner=self._runner(self._unit(User='root')))
        with self.assertRaisesRegex(RuntimeErrorSafe, 'locate'):
            netbox_runtime(
                self.cfg, runner=self._runner(self._unit(WorkingDirectory=str(self.root / 'absent')))
            )

    def test_service_errors_never_reveal_credentials(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, '', 'password=LEAKED_SECRET')
        with self.assertRaises(RuntimeErrorSafe) as failure:
            netbox_runtime(self.cfg, runner=runner)
        self.assertNotIn('LEAKED_SECRET', str(failure.exception))


class StagedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Configuration(DEFAULT_UNITS.copy())
        self.stage = 'nbo_stage_0123456789abcdef'
        self.runtime = NetBoxRuntime(
            'netbox', Path('/custom/venv/bin/python'), Path('/custom/netbox'),
            {'NETBOX_DB_PASSWORD': 'SECRET_NEVER_LOG', 'NETBOX_CONFIGURATION': 'my-config'},
        )

    def test_runs_as_service_user_and_overrides_only_database_in_child(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        with patch('netbox_ops.restore_environment.netbox_runtime', return_value=self.runtime):
            check_staged_migrations(self.cfg, self.stage, runner=runner)
        argv, options = calls[0]
        self.assertEqual(argv[:4], ['runuser', '-u', 'netbox', '--'])
        self.assertEqual(argv[4], '/custom/venv/bin/python')
        self.assertEqual(options['cwd'], Path('/custom/netbox'))
        self.assertEqual(options['env']['NBO_STAGE_DATABASE'], self.stage)
        self.assertEqual(options['env']['NBO_ACTIVE_DATABASE'], 'netbox')
        self.assertEqual(options['env']['NETBOX_CONFIGURATION'], 'my-config')
        self.assertNotIn('SECRET_NEVER_LOG', ' '.join(argv))

    def test_plugin_migrations_fail_without_disclosing_child_output(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 10, b'', b'SECRET_NEVER_LOG')
        with patch('netbox_ops.restore_environment.netbox_runtime', return_value=self.runtime):
            with self.assertRaisesRegex(RuntimeErrorSafe, 'unapplied') as failure:
                check_staged_migrations(self.cfg, self.stage, runner=runner)
        self.assertNotIn('SECRET_NEVER_LOG', str(failure.exception))

    def test_django_startup_failure_is_sanitized(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 11, b'', b'password=SECRET_NEVER_LOG')
        with patch('netbox_ops.restore_environment.netbox_runtime', return_value=self.runtime):
            with self.assertRaises(RuntimeErrorSafe) as failure:
                check_staged_migrations(self.cfg, self.stage, runner=runner)
        self.assertNotIn('SECRET_NEVER_LOG', str(failure.exception))

    def test_rejects_unexpected_stage_name_before_execution(self):
        with patch('netbox_ops.restore_environment.netbox_runtime') as runtime:
            with self.assertRaisesRegex(RuntimeErrorSafe, 'Invalid staged'):
                check_staged_migrations(self.cfg, 'netbox')
        runtime.assert_not_called()

    def test_wrapper_checks_django_plugin_migration_graph_without_writing(self):
        # A minimal fake Django environment tests the actual embedded Python
        # wrapper's exit statuses without installing NetBox or PostgreSQL.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                'django/__init__.py': 'def setup(): pass\n',
                'django/db/__init__.py': (
                    'from django.db.backends.base.base import BaseDatabaseWrapper\n'
                    'from django.conf import settings\n'
                    'class Cursor:\n'
                    '    def __enter__(self):\n'
                    '        connections["default"].ensure_connection()\n'
                    '        return self\n'
                    '    def __exit__(self, *args): pass\n'
                    '    def execute(self, sql):\n'
                    '        assert sql == "SELECT current_database()"\n'
                    '    def fetchone(self):\n'
                    '        return (settings.DATABASES["default"]["NAME"],)\n'
                    'class Connection(BaseDatabaseWrapper):\n'
                    '    def __init__(self):\n'
                    '        self.settings_dict = settings.DATABASES["default"]\n'
                    '    def cursor(self): return Cursor()\n'
                    'connections = {"default": Connection()}\n'
                ),
                'django/db/backends/__init__.py': '',
                'django/db/backends/base/__init__.py': '',
                'django/db/backends/base/base.py': (
                    'class BaseDatabaseWrapper:\n'
                    '    def ensure_connection(self): pass\n'
                ),
                'django/conf/__init__.py': (
                    'import os\n'
                    'class Settings:\n'
                    '    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql",\n'
                    '                            "NAME": "netbox"}}\n'
                    'settings = Settings()\n'
                ),
                'django/db/migrations/__init__.py': '',
                'django/db/migrations/executor.py': (
                    'import os\n'
                    'class Graph:\n'
                    '    def leaf_nodes(self): return ["netbox.0001", "plugin.0002"]\n'
                    'class Loader:\n'
                    '    graph = Graph()\n'
                    'class MigrationExecutor:\n'
                    '    loader = Loader()\n'
                    '    def __init__(self, connection): pass\n'
                    '    def migration_plan(self, targets):\n'
                    '        return ["plugin.0002"] if os.getenv("PENDING_PLUGIN") else []\n'
                ),
                'django/core/__init__.py': '',
                'django/core/management/__init__.py': (
                    'def call_command(name, **kwargs):\n'
                    '    assert name == "migrate" and kwargs["check"] is True\n'
                ),
            }
            for name, body in paths.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            environment = {
                **os.environ,
                'PYTHONPATH': str(root),
                'NBO_STAGE_DATABASE': self.stage,
                'NBO_ACTIVE_DATABASE': 'netbox',
            }
            okay = subprocess.run(
                [sys.executable, '-c', DJANGO_STAGE_CHECK],
                env=environment, capture_output=True, check=False,
            )
            self.assertEqual(okay.returncode, 0, okay.stderr)
            pending = subprocess.run(
                [sys.executable, '-c', DJANGO_STAGE_CHECK],
                env={**environment, 'PENDING_PLUGIN': 'yes'},
                capture_output=True, check=False,
            )
            self.assertEqual(pending.returncode, 10, pending.stderr)


if __name__ == '__main__':
    unittest.main()
