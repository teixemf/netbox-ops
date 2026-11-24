"""Mock all Git operations: no checkout or network access during tests."""
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from netbox_ops.upgrade_checkout import checkout_pinned_stage, fetch_pinned_stage
from netbox_ops.upgrade_execution import UpgradeExecutionError
from netbox_ops.upgrade_git import PinnedStage, OFFICIAL_REMOTE


class CheckoutStageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('/opt/netbox')
        self.old = 'a' * 40
        self.new = 'b' * 40
        self.stage = PinnedStage('4.4.1', self.new)
        self.inspector = Mock(return_value=SimpleNamespace(
            root=self.root, origin=OFFICIAL_REMOTE, commit=self.old,
        ))
        self.runner = Mock(side_effect=self.respond)

    def respond(self, arguments, **kwargs):
        action = arguments[arguments.index('-C') + 2:]
        if action[:1] == ['rev-parse']:
            value = action[-1]
            if value == 'HEAD':
                return SimpleNamespace(returncode=0, stdout=self.new)
            return SimpleNamespace(returncode=0, stdout=self.new)
        return SimpleNamespace(returncode=0, stdout='')

    def checkout(self, **kwargs):
        return checkout_pinned_stage(
            self.root, self.stage, expected_commit=self.old,
            inspector=self.inspector, runner=self.runner, **kwargs,
        )

    def fetch(self):
        return fetch_pinned_stage(
            self.root, self.stage, inspector=self.inspector,
            runner=self.runner,
        )

    def commands(self):
        return [call.args[0][call.args[0].index('-C') + 2:]
                for call in self.runner.call_args_list]

    def test_fetch_official_tag_and_validate_commit(self):
        self.fetch()
        self.assertEqual(self.commands()[0],
                         ['fetch', '--no-tags', '--', OFFICIAL_REMOTE, 'refs/tags/v4.4.1'])
        self.assertEqual(self.commands()[1],
                         ['rev-parse', '--verify', 'FETCH_HEAD^{commit}'])
        self.assertTrue(all(call.kwargs['cwd'] == self.root
                            for call in self.runner.call_args_list))

    def test_checkout_requires_exact_expected_head(self):
        self.checkout()
        self.assertEqual(self.commands()[-2], ['checkout', '--detach', self.new])
        self.assertEqual(self.commands()[-1], ['rev-parse', '--verify', 'HEAD'])
        self.assertFalse(any('--force' in command for command in self.commands()))

    def test_stale_checkout_never_runs_git(self):
        self.inspector.return_value.commit = 'c' * 40
        with self.assertRaises(UpgradeExecutionError):
            self.checkout()
        self.runner.assert_not_called()

    def test_invalid_version_never_fetches(self):
        self.stage = PinnedStage('--upload-pack=secret', self.new)
        with self.assertRaises(UpgradeExecutionError):
            self.fetch()
        self.inspector.assert_not_called()
        self.runner.assert_not_called()

    def test_foreign_origin_blocks_fetch_and_checkout(self):
        self.inspector.return_value.origin = 'https://github.com/attacker/netbox.git'
        with self.assertRaises(UpgradeExecutionError):
            self.fetch()
        with self.assertRaises(UpgradeExecutionError):
            self.checkout()
        self.runner.assert_not_called()

    def test_supported_ssh_origin_uses_official_https_fetch(self):
        self.inspector.return_value.origin = 'git@github.com:netbox-community/netbox.git'
        self.fetch()
        self.assertIn(OFFICIAL_REMOTE, self.commands()[0])

    def test_invalid_commit_never_checks_out(self):
        self.stage = PinnedStage('4.4.1', '../secret')
        with self.assertRaises(UpgradeExecutionError):
            self.checkout()
        self.runner.assert_not_called()

    def test_remote_mismatch_blocks_fetch(self):
        self.runner.side_effect = lambda args, **kwargs: SimpleNamespace(
            returncode=0, stdout='c' * 40
            if args[-1] == 'FETCH_HEAD^{commit}' else '',
        )
        with self.assertRaises(UpgradeExecutionError):
            self.fetch()
        self.assertNotIn(['checkout', '--detach', self.new], self.commands())

    def test_tracked_changes_block_checkout(self):
        def dirty(args, **kwargs):
            command = args[args.index('-C') + 2:]
            if command[0] == 'status':
                return SimpleNamespace(returncode=0, stdout=' M netbox/settings.py')
            return SimpleNamespace(returncode=0, stdout=self.new)
        self.runner.side_effect = dirty
        with self.assertRaises(UpgradeExecutionError):
            self.checkout()
        self.assertFalse(any(command[0] == 'checkout' for command in self.commands()))

    def test_missing_pinned_object_blocks_checkout(self):
        self.runner.side_effect = lambda args, **kwargs: SimpleNamespace(
            returncode=1, stdout='', stderr='secret'
        )
        with self.assertRaises(UpgradeExecutionError) as caught:
            self.checkout()
        self.assertNotIn('secret', str(caught.exception))
        self.assertFalse(any(command[0] == 'checkout' for command in self.commands()))

    def test_timeout_does_not_disclose_command(self):
        self.runner.side_effect = subprocess.TimeoutExpired('secret', 120)
        with self.assertRaises(UpgradeExecutionError) as caught:
            self.fetch()
        self.assertNotIn('secret', str(caught.exception))

    def test_wrong_checkout_revision_detected(self):
        def stale(args, **kwargs):
            command = args[args.index('-C') + 2:]
            return SimpleNamespace(
                returncode=0,
                stdout=(self.old if command[-1] == 'HEAD' else
                        '' if command[0] in ('status', 'checkout') else self.new),
            )
        self.runner.side_effect = stale
        with self.assertRaises(UpgradeExecutionError):
            self.checkout()
        self.assertIn(['checkout', '--detach', self.new], self.commands())

    def test_runner_environment_disables_git_overrides(self):
        self.fetch()
        env = self.runner.call_args_list[0].kwargs['env']
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')


if __name__ == '__main__':
    unittest.main()
