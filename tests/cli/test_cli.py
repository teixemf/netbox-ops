from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from netbox_ops.cli import MENU_ACTIONS, build_parser, dispatch
from netbox_ops.config import DEFAULT_UNITS, SERVICE_NAMES, Configuration
from netbox_ops.i18n import Translator


GOOD_SYSTEMD_RESPONSE = '\n'.join([
    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
    'UnitFileState=enabled']) + '\n'


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
                for argv in (['updates', 'check'], ['self-upgrade']):
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
