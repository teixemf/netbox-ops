"""Bounded, reviewed diagnostics for upgrade subprocesses.

Never include command arguments, environment, exception text or raw output.
The label and hint are selected from local constants, not subprocess data.
"""
from __future__ import annotations

import subprocess
from typing import Callable

from .upgrade_execution import UpgradeExecutionError
from .upgrade_process import ManagedOutputError, ManagedProcessError, UpgradeInterrupted


_COMMANDS = {
    'netbox.upgrade script': ('NetBox upgrade script', 'review the target release notes and Python/PostgreSQL prerequisites'),
    'netbox.Django system check': ('NetBox Django system check', 'check configuration and configured plugin compatibility'),
    'netbox.migration check': ('NetBox migration check', 'inspect database migration state before restarting services'),
    'plugin.installation': ('Plugin installation', 'verify approved package pins and wheel availability'),
    'plugin.dependency check': ('Plugin dependency check', 'review installed package versions and dependency constraints'),
    'plugin.Django system check': ('Plugin Django system check', 'check plugin configuration and compatibility'),
    'plugin.migration': ('Plugin migration', 'inspect database migration state and the recovery point'),
    'plugin.static collection': ('Plugin static collection', 'check static-file directory permissions and free space'),
    'plugin.migration check': ('Plugin migration check', 'inspect pending migrations before restarting services'),
    'plugin.loading check': ('Plugin loading check', 'check configured plugin imports and runtime versions'),
    'git.fetch': ('NetBox Git fetch', 'check official release availability and network access'),
    'git.checkout': ('NetBox Git checkout', 'check local tracked files and the approved commit'),
    'git.status': ('NetBox Git status', 'check Git worktree access and ownership'),
    'git.rev-parse': ('NetBox Git revision lookup', 'check that the approved Git object exists'),
    'git.show': ('NetBox Git requirements lookup', 'check the approved release requirements'),
    'git.init': ('NetBox Git scratch initialization', 'check temporary storage and permissions'),
    'git.release_lookup': ('Official release lookup', 'check network access to the official NetBox remote'),
    'wheel.prepare': ('Upgrade wheel preparation', 'check package availability, Python compatibility and free space'),
    'wheel.psycopg': ('Upgrade wheel preparation', 'check Python headers, C compiler and PostgreSQL client development files'),
    'wheel.ldap': ('Isolated python-ldap wheel build', 'check Python headers, C compiler, OpenLDAP and SASL development files and systemd sandbox availability'),
}
_UNKNOWN = ('Upgrade command', 'check the operation phase and recovery evidence')


class CommandFailure(UpgradeExecutionError):
    """Only structured, locally reviewed fields may leave a failed command."""

    def __init__(self, command_id: str, reason: str, *, code: int | None = None,
                 timeout: int | None = None):
        self.command_id = command_id if command_id in _COMMANDS else ''
        self.reason = reason if reason in (
            'exit', 'timeout', 'unavailable', 'interrupted', 'descendants', 'output', 'artifacts',
        ) else 'unavailable'
        self.code = code if type(code) is int and -255 <= code <= 255 else None
        self.timeout = timeout if type(timeout) is int and 0 < timeout <= 86400 else None
        super().__init__(self.safe_message())

    def safe_message(self) -> str:
        label, hint = _COMMANDS.get(self.command_id, _UNKNOWN)
        if self.reason == 'exit':
            if type(self.code) is int and -255 <= self.code < 0:
                outcome = f'failed (terminated by signal {-self.code})'
            elif type(self.code) is int and 0 <= self.code <= 255:
                outcome = f'failed (exit status {self.code})'
            else:
                outcome = 'failed (exit status unavailable)'
        elif self.reason == 'timeout':
            outcome = (f'timed out after {self.timeout}s'
                       if type(self.timeout) is int and 0 < self.timeout <= 86400 else 'timed out')
        elif self.reason == 'interrupted':
            outcome = 'interruption requested'
        elif self.reason == 'descendants':
            outcome = 'left active descendants; managed group stopped'
        elif self.reason == 'output':
            outcome = 'failed (captured output exceeded safety limits or was invalid)'
        elif self.reason == 'artifacts':
            outcome = 'failed (no usable wheel artifacts)'
        else:
            outcome = 'unavailable'
        return f'{label} {outcome}; {hint}'


def run_checked(command_id: str, command, *, runner: Callable, timeout: int, **kwargs):
    """Run a command and translate failures without copying arbitrary text."""
    try:
        result = runner(command, timeout=timeout, check=False, **kwargs)
    except subprocess.TimeoutExpired:
        raise CommandFailure(command_id, 'timeout', timeout=timeout) from None
    except UpgradeInterrupted:
        raise CommandFailure(command_id, 'interrupted') from None
    except ManagedProcessError:
        raise CommandFailure(command_id, 'descendants') from None
    except (ManagedOutputError, UnicodeError):
        raise CommandFailure(command_id, 'output') from None
    except OSError:
        raise CommandFailure(command_id, 'unavailable') from None
    if result.returncode:
        raise CommandFailure(command_id, 'exit', code=result.returncode) from None
    return result
