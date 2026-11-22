from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import MagicMock, patch

from netbox_ops.cli import build_parser, dispatch, perform_restart, perform_service_action, perform_service_batch, order_services, challenge
from netbox_ops.config import DEFAULT_UNITS, Configuration
from netbox_ops.i18n import Translator
from netbox_ops.services import ServiceError, SystemdServices


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


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
        with patch('netbox_ops.services.os.geteuid', return_value=0), \
             patch('netbox_ops.services.ExclusiveLock'), \
             patch('netbox_ops.services.write_audit') as audit:
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
        with patch('netbox_ops.services.os.geteuid', return_value=0), \
             patch('netbox_ops.services.ExclusiveLock'), \
             patch('netbox_ops.services.write_audit'):
            results, failure = perform_service_batch(manager, 'stop',
                                                     ['postgresql', 'netbox'])
        self.assertEqual(results, [])
        self.assertIn('netbox', failure)
        manager.stop.assert_called_once_with(['netbox'])

    def test_batch_requires_root_and_never_runs_any_service(self):
        manager = MagicMock()
        with patch('netbox_ops.services.os.geteuid', return_value=1000):
            with self.assertRaises(ServiceError):
                perform_service_batch(manager, 'disable', ['netbox'])
        manager.disable.assert_not_called()
