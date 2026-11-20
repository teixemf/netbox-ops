from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from netbox_ops.cli import (MENU_ACTIONS, build_parser, dispatch, interactive_menu,
                            perform_restart, perform_service_action, perform_service_batch,
                            order_services, challenge)
from netbox_ops.config import (CONFIG_PATH, DEFAULT_UNITS, SERVICE_NAMES,
                               Configuration, ConfigError, load_config)
from netbox_ops.i18n import CATALOGUE_DIR, Translator
from netbox_ops.services import ServiceError, SystemdServices


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


class I18nTests(unittest.TestCase):
    def test_key_parity(self):
        pt = json.loads((CATALOGUE_DIR / 'pt.json').read_text())
        en = json.loads((CATALOGUE_DIR / 'en.json').read_text())
        self.assertEqual(set(pt), set(en))
        for key in pt:
            self.assertTrue(pt[key])
            self.assertTrue(en[key])

    def test_translation(self):
        self.assertIn('Gerir', Translator('pt')('menu_2'))
        self.assertIn('Manage', Translator('en')('menu_2'))

    def test_menu_language_switch_and_stub(self):
        answers = iter(['l', '2', '3', '0'])
        with patch('builtins.input', side_effect=lambda *_: next(answers)):
            with redirect_stdout(io.StringIO()) as output:
                rc = interactive_menu(Translator('pt'))
        self.assertEqual(rc, 0)
        self.assertIn('Language changed to English', output.getvalue())
        self.assertIn('[NOT IMPLEMENTED] updates check', output.getvalue())


class CliParserTests(unittest.TestCase):
    def test_every_menu_action_has_cli_parser(self):
        parser = build_parser()
        self.assertEqual(len(MENU_ACTIONS), 7)
        for _, group, action, _ in MENU_ACTIONS:
            argv = [group] + ([action] if action else [])
            if group == 'restore':
                argv.extend(['--file', '/tmp/example.dump'])
            args = parser.parse_args(argv)
            self.assertEqual(args.command, group)
            if action:
                self.assertEqual(args.action, action)

    def test_stubs_do_not_read_config_or_invoke_systemd(self):
        parser = build_parser()
        with patch('netbox_ops.cli.load_config', side_effect=AssertionError('must not load')):
            with patch('netbox_ops.cli.SystemdServices', side_effect=AssertionError('must not run')):
                for argv in (['updates', 'check'], ['upgrade', 'netbox', '--version', '5.0.0'],
                             ['upgrade', 'plugins', '--plugin', 'example'],
                             ['upgrade', 'stack', '--plugins', 'all'], ['self-upgrade']):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(dispatch(parser.parse_args(argv), Translator('pt')), 3)

    def test_restarts_default_to_netbox_and_rq(self):
        parser = build_parser()
        args = parser.parse_args(['services', 'restart', '--yes'])
        mock_sys = MagicMock()
        with patch('netbox_ops.cli.load_config', return_value=Configuration(DEFAULT_UNITS)):
            with patch('netbox_ops.cli.SystemdServices', return_value=mock_sys):
                with patch('netbox_ops.cli.perform_restart', return_value=0) as restart:
                    dispatch(args, Translator('pt'))
        self.assertEqual(tuple(restart.call_args.args[1]), ('netbox', 'netbox-rq'))

    def test_restarts_all_only_when_explicit(self):
        parser = build_parser()
        args = parser.parse_args(['services', 'restart', '--all', '--yes', '--allow-critical'])
        with patch('netbox_ops.cli.load_config', return_value=Configuration(DEFAULT_UNITS)):
            with patch('netbox_ops.cli.SystemdServices'):
                with patch('netbox_ops.cli.perform_restart', return_value=0) as restart:
                    dispatch(args, Translator('pt'))
        self.assertEqual(tuple(restart.call_args.args[1]), SERVICE_NAMES)


class ServiceTests(unittest.TestCase):
    def test_status_reads_only_systemd(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE, ''))
        manager = SystemdServices(Configuration(DEFAULT_UNITS), runner=runner)
        states = manager.status(['netbox', 'redis'])
        self.assertEqual(len(states), 2)
        self.assertTrue(all(s.healthy for s in states))
        self.assertEqual([x.args[0][1] for x in runner.call_args_list], ['show', 'show'])

    def test_missing_unit_is_unhealthy(self):
        response = GOOD_SYSTEMD_RESPONSE.replace('loaded', 'not-found').replace('active', 'inactive')
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, response, ''))
        state = SystemdServices(Configuration(DEFAULT_UNITS), runner).status(['redis'])[0]
        self.assertFalse(state.healthy)

    def test_restart_checks_result(self):
        runner = MagicMock(side_effect=[
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE, ''),
        ])
        service = SystemdServices(Configuration(DEFAULT_UNITS), runner)
        self.assertTrue(service.restart(['netbox'])[0].healthy)
        self.assertEqual(runner.call_args_list[0].args[0], ['systemctl', 'restart', 'netbox.service'])

    def test_restart_error_stops(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 1, '', 'permission denied'))
        service = SystemdServices(Configuration(DEFAULT_UNITS), runner)
        with self.assertRaises(ServiceError):
            service.restart(['netbox', 'redis'])
        self.assertEqual(runner.call_count, 1)

    def test_nonroot_never_restarts(self):
        mock_sys = MagicMock()
        with patch('netbox_ops.cli.os.geteuid', return_value=1000):
            with redirect_stderr(io.StringIO()):
                rc = perform_restart(mock_sys, ['netbox'], Translator('pt'), yes=True)
        self.assertEqual(rc, 2)
        mock_sys.restart.assert_not_called()

    def test_cancelled_restart_never_restarts(self):
        mock_sys = MagicMock()
        with patch('netbox_ops.cli.os.geteuid', return_value=0):
            with patch('builtins.input', return_value='yes'):
                with redirect_stdout(io.StringIO()):
                    rc = perform_restart(mock_sys, ['netbox'], Translator('en'))
        self.assertEqual(rc, 2)
        mock_sys.restart.assert_not_called()

    def test_confirmed_restart_requires_lock_and_audit(self):
        mock_sys = MagicMock()
        mock_sys.restart.return_value = [MagicMock(name='netbox', active='active')]
        with patch('netbox_ops.cli.os.geteuid', return_value=0):
            with patch('netbox_ops.cli.ExclusiveLock') as lock:
                with patch('netbox_ops.cli.write_audit') as audit:
                    with redirect_stdout(io.StringIO()):
                        rc = perform_restart(mock_sys, ['netbox'], Translator('en'), yes=True)
        self.assertEqual(rc, 0)
        mock_sys.restart.assert_called_once_with(['netbox'])
        lock.assert_called_once()
        self.assertEqual(audit.call_count, 2)


class NewServiceOperationsTests(unittest.TestCase):
    def test_service_logs_executes_bounded_journalctl(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, 'hello\x1b[31m\nnext\n', ''))
        manager = SystemdServices(Configuration(DEFAULT_UNITS), runner)
        self.assertEqual(manager.logs('netbox', 50), ['hello [31m', 'next'])
        call = runner.call_args.args[0]
        self.assertEqual(call[0], 'journalctl')
        self.assertEqual(call[call.index('--unit') + 1], 'netbox.service')
        self.assertEqual(call[call.index('--lines') + 1], '50')
        self.assertIn('--no-pager', call)

    def test_invalid_log_count_never_runs(self):
        runner = MagicMock()
        manager = SystemdServices(Configuration(DEFAULT_UNITS), runner)
        for count in (0, 501, -1, '20', True):
            with self.subTest(count=count):
                with self.assertRaises(ServiceError):
                    manager.logs('netbox', count)
        runner.assert_not_called()

    def test_start_and_stop_verify_state(self):
        active = subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE, '')
        inactive = subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE.replace('active', 'inactive').replace('running','dead'), '')
        runner = MagicMock(side_effect=[subprocess.CompletedProcess([], 0, '', ''), active,
                                        subprocess.CompletedProcess([], 0, '', ''), inactive])
        manager = SystemdServices(Configuration(DEFAULT_UNITS), runner)
        self.assertTrue(manager.start(['netbox'])[0].healthy)
        self.assertEqual(manager.stop(['netbox'])[0].active, 'inactive')
        self.assertEqual(runner.call_args_list[0].args[0], ['systemctl', 'start', 'netbox.service'])
        self.assertEqual(runner.call_args_list[2].args[0], ['systemctl', 'stop', 'netbox.service'])

    def test_failed_stop_is_reported(self):
        runner = MagicMock(side_effect=[subprocess.CompletedProcess([], 0, '', ''),
                                        subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE, '')])
        with self.assertRaises(ServiceError):
            SystemdServices(Configuration(DEFAULT_UNITS), runner).stop(['redis'])

    def test_confirmation_tokens(self):
        self.assertEqual(challenge('restart', ['netbox', 'netbox-rq']), 'RESTART')
        self.assertEqual(challenge('stop', ['redis']), 'STOP redis')
        self.assertEqual(challenge('restart', ['postgresql', 'redis']), 'RESTART postgresql,redis')

    def test_critical_automation_needs_extra_flag(self):
        sysd = MagicMock()
        with patch('netbox_ops.cli.os.geteuid', return_value=0):
            with redirect_stderr(io.StringIO()):
                rc = perform_service_action(sysd, 'stop', ['redis'], Translator('pt'), yes=True)
        self.assertEqual(rc, 2)
        sysd.stop.assert_not_called()

    def test_no_root_never_starts_or_stops(self):
        sysd = MagicMock()
        with patch('netbox_ops.cli.os.geteuid', return_value=1000):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(perform_service_action(sysd, 'start', ['netbox'], Translator('pt'), yes=True), 2)
        sysd.start.assert_not_called()

    def test_service_change_reuses_lock_and_audit(self):
        sysd = MagicMock()
        sysd.start.return_value = [MagicMock(name='netbox', active='active')]
        with patch('netbox_ops.cli.os.geteuid', return_value=0), \
             patch('netbox_ops.cli.ExclusiveLock') as lock, \
             patch('netbox_ops.cli.write_audit') as audit:
            with redirect_stdout(io.StringIO()):
                rc = perform_service_action(sysd, 'start', ['netbox'], Translator('en'), yes=True)
        self.assertEqual(rc, 0)
        sysd.start.assert_called_once_with(['netbox'])
        lock.assert_called_once()
        self.assertEqual(audit.call_count, 2)

    def test_parser_start_stop_logs_and_stub(self):
        parser = build_parser()
        for action in ('start', 'stop'):
            args = parser.parse_args(['services', action, '--service', 'netbox'])
            self.assertEqual(args.action, action)
        logs = parser.parse_args(['services', 'logs', '--service', 'redis', '--lines', '15'])
        self.assertEqual(logs.lines, 15)
        self.assertEqual(logs.service, 'redis')
        with patch('netbox_ops.cli.load_config', return_value=Configuration(DEFAULT_UNITS)), \
             patch('netbox_ops.cli.SystemdServices') as obj:
            obj.return_value.logs.return_value = ['line one']
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(dispatch(logs, Translator('pt')), 0)
            self.assertEqual(out.getvalue().strip(), 'line one')


class LifecycleAndBatchTests(unittest.TestCase):
    def test_enable_disable_never_use_now(self):
        enabled = GOOD_SYSTEMD_RESPONSE
        disabled = GOOD_SYSTEMD_RESPONSE.replace('UnitFileState=enabled', 'UnitFileState=disabled')
        run = MagicMock(side_effect=[
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, enabled, ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, disabled, ''),
        ])
        systemd = SystemdServices(Configuration(DEFAULT_UNITS), run)
        self.assertEqual(systemd.enable(['netbox'])[0].enabled, 'enabled')
        self.assertEqual(systemd.disable(['netbox'])[0].enabled, 'disabled')
        self.assertEqual(run.call_args_list[0].args[0], ['systemctl', 'enable', 'netbox.service'])
        self.assertEqual(run.call_args_list[2].args[0], ['systemctl', 'disable', 'netbox.service'])

    def test_enable_without_correct_unit_file_state_fails(self):
        run = MagicMock(side_effect=[
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, GOOD_SYSTEMD_RESPONSE.replace('enabled', 'disabled'), ''),
        ])
        with self.assertRaises(ServiceError):
            SystemdServices(Configuration(DEFAULT_UNITS), run).enable(['netbox'])

    def test_new_actions_in_parser_and_cli(self):
        parser = build_parser()
        for action in ('enable', 'disable'):
            args = parser.parse_args(['services', action, '--service', 'netbox'])
            self.assertEqual(args.action, action)
        args = parser.parse_args(['services', 'manage'])
        self.assertEqual(args.action, 'manage')
        for action in ('enable', 'disable'):
            systemd = MagicMock()
            setattr(systemd, action, MagicMock(return_value=[MagicMock(name='netbox', active='active')]))
            with patch('netbox_ops.cli.os.geteuid', return_value=0), \
                 patch('netbox_ops.cli.ExclusiveLock'), \
                 patch('netbox_ops.cli.write_audit'):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(perform_service_action(systemd, action, ['netbox'],
                                                             Translator('pt'), yes=True), 0)
            getattr(systemd, action).assert_called_once_with(['netbox'])

    def test_order_respects_dependencies_and_targets_only_selected(self):
        self.assertEqual(order_services('start', ['netbox', 'postgresql', 'redis']),
                         ['postgresql', 'redis', 'netbox'])
        self.assertEqual(order_services('stop', ['postgresql', 'netbox', 'redis']),
                         ['netbox', 'redis', 'postgresql'])
        self.assertEqual(order_services('enable', ['netbox', 'postgresql']),
                         ['netbox', 'postgresql'])
        with self.assertRaises(ServiceError):
            order_services('restart', ['fake'])

    def test_batch_real_progress_after_each_success(self):
        manager = MagicMock()
        manager.start.side_effect = [
            [MagicMock(active='active', enabled='enabled')],
            [MagicMock(active='active', enabled='enabled')],
        ]
        events = []
        with patch('netbox_ops.cli.os.geteuid', return_value=0), \
             patch('netbox_ops.cli.ExclusiveLock'), \
             patch('netbox_ops.cli.write_audit') as audit:
            results, failure = perform_service_batch(manager, 'start', ['netbox', 'redis'],
                                                     lambda *event: events.append(event))
        self.assertIsNone(failure)
        self.assertEqual(results, [('redis', 'active'), ('netbox', 'active')])
        self.assertEqual([e[:4] for e in events],
                         [('running', 0, 2, 'redis'), ('done', 1, 2, 'redis'),
                          ('running', 1, 2, 'netbox'), ('done', 2, 2, 'netbox')])
        self.assertGreaterEqual(audit.call_count, 4)

    def test_batch_aborts_failure_without_touching_remaining(self):
        manager = MagicMock()
        manager.stop.side_effect = ServiceError('simulated failure')
        with patch('netbox_ops.cli.os.geteuid', return_value=0), \
             patch('netbox_ops.cli.ExclusiveLock'), \
             patch('netbox_ops.cli.write_audit'):
            results, failure = perform_service_batch(manager, 'stop',
                                                     ['postgresql', 'netbox'])
        self.assertEqual(results, [])
        self.assertIn('netbox', failure)
        manager.stop.assert_called_once_with(['netbox'])

    def test_batch_requires_root_and_never_runs_any_service(self):
        manager = MagicMock()
        with patch('netbox_ops.cli.os.geteuid', return_value=1000):
            with self.assertRaises(ServiceError):
                perform_service_batch(manager, 'disable', ['netbox'])
        manager.disable.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_default_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(Path(temp) / 'missing.json')
        self.assertEqual(config.units['postgresql'], 'postgresql.service')

    def test_local_unit_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': 'postgresql-16.service'}}))
            config = load_config(path)
        self.assertEqual(config.units['postgresql'], 'postgresql-16.service')

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'services': {'postgresql': '--dangerous'}}))
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
