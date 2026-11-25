"""One command dispatcher shared by the interactive menu and the CLI."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import CONFIG_PATH, DEFAULT_RESTART, SERVICE_NAMES, ConfigError, Configuration, load_config
from .i18n import LANGUAGES, Translator, detect_language
from .menu import MENU_ACTIONS
from .services import (CRITICAL_SERVICES, ExclusiveLock, ServiceError, SystemdServices,
                       challenge, order_services, perform_service_batch as perform_service_batch,
                       require_service_action_allowed, write_audit)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='netbox-ops',
        description='NetBox maintenance CLI. Run without arguments for interactive menu.')
    parser.add_argument('--lang', choices=LANGUAGES, default=detect_language(), help='Interface language (pt/en; default: system locale).')
    parser.add_argument('--no-tui', action='store_true',
                        help='Use the original numbered text menu (terminal fallback).')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH,
                        help='Root-managed JSON configuration path.')
    parser.add_argument('--version', action='version', version=f'netbox-ops {__version__}')
    top = parser.add_subparsers(dest='command', metavar='COMMAND')

    services = top.add_parser('services', help='Systemd status, logs and lifecycle/startup operations.')
    service_commands = services.add_subparsers(dest='action', required=True)
    service_commands.add_parser('manage', help='Open interactive multi-service manager (TUI).')
    status = service_commands.add_parser('status', help='Read status of configured services.')
    status.add_argument('--service', choices=SERVICE_NAMES, action='append',
                        help='Show one or more services; defaults to all.')
    logs = service_commands.add_parser('logs', help='Read recent journal records.')
    logs.add_argument('--service', choices=SERVICE_NAMES, required=True)
    logs.add_argument('--lines', type=int, default=20, help='Number of records (1-500).')
    for change in ('start', 'stop', 'enable', 'disable'):
        cmd = service_commands.add_parser(change, help=f'{change.title()} selected service(s).')
        cmd.add_argument('--service', choices=SERVICE_NAMES, action='append', required=True)
        cmd.add_argument('--yes', action='store_true', help='Skip interactive confirmation.')
        cmd.add_argument('--allow-critical', action='store_true',
                         help='Required with --yes for PostgreSQL or Redis.')
    restart = service_commands.add_parser('restart',
        help='Restart NetBox + RQ by default, or selected services.')
    selector = restart.add_mutually_exclusive_group()
    selector.add_argument('--service', choices=SERVICE_NAMES, action='append',
                          help='May be repeated for multiple services.')
    selector.add_argument('--all', action='store_true',
                          help='Include PostgreSQL, Redis, Nginx, NetBox and RQ (disruptive).')
    restart.add_argument('--yes', action='store_true',
                         help='Skip confirmation (for deliberate automation).')

    restart.add_argument('--allow-critical', action='store_true',
                         help='Required with --yes for PostgreSQL or Redis.')

    backup = top.add_parser('backup', help='Create PostgreSQL, artifact or integrated archives.')
    backup_commands = backup.add_subparsers(dest='action', required=True)
    for operation in ('db', 'media', 'combined'):
        backup_commands.add_parser(operation, help='Create a protected, timestamped backup.')
    listed = backup_commands.add_parser('list', help='List local backup archives.')
    listed.add_argument('--kind', choices=('db', 'media', 'combined', 'full', 'legacy'))
    verified = backup_commands.add_parser('verify', help='Verify a backup archive and its checksums.')
    verified.add_argument('--kind', choices=('db', 'media', 'combined', 'full', 'legacy'), required=True)
    verified.add_argument('--file', type=Path, required=True)

    deleted = backup_commands.add_parser('delete', help='Delete one local backup, with explicit confirmation.')
    deleted.add_argument('--file', type=Path, required=True)
    deleted.add_argument('--yes', action='store_true')
    backup_commands.add_parser('manage', help='Open the unified backup TUI.')

    restore = top.add_parser('restore', help='Restore a verified backup (disruptive).')
    restore_commands = restore.add_subparsers(dest='action', required=True)
    for operation in ('db', 'media', 'combined', 'full'):
        command = restore_commands.add_parser(operation, help='Restore an archive of this type.')
        command.add_argument('--file', type=Path, required=True)
        if operation in ('combined', 'full'):
            command.add_argument('--only', choices=('db', 'media', 'all'), default='all',
                                 help='Restore only DB, only media, or both (default).')
        command.add_argument('--yes', action='store_true', help='Noninteractive restore confirmation.')
        command.add_argument('--acknowledge-data-loss', action='store_true',
                             help='Required together with --yes for automation.')
        command.add_argument('--accept-unknown-version', action='store_true',
                             help='Allow restore if source/current NetBox version is unknown.')
        command.add_argument('--allow-version-mismatch', action='store_true',
                             help='Allow a version mismatch after manual compatibility review.')

    updates = top.add_parser('updates', help='Update discovery (not yet implemented).')
    updates_commands = updates.add_subparsers(dest='action', required=True)
    updates_commands.add_parser('check', help='Not yet implemented.')

    upgrade = top.add_parser('upgrade', help='Inspect, plan and execute NetBox upgrades.')
    upgrade_commands = upgrade.add_subparsers(dest='action', required=True)
    upgrade_commands.add_parser('inspect', help='Inspect installed plugins and official NetBox releases.')
    pins = upgrade_commands.add_parser('pins', help='Review exact local plugin pin corrections.')
    pin_selection = pins.add_mutually_exclusive_group()
    pin_selection.add_argument('--plugin', action='append', help='Select a proposed pin; repeat for more.')
    pin_selection.add_argument('--all', action='store_true', help='Select all unambiguous proposals.')
    pins.add_argument('--apply', action='store_true', help='Write only explicitly approved pin corrections.')
    pins.add_argument('--approve-pins', help='Exact pin review fingerprint.')
    versions = upgrade_commands.add_parser('versions', help='List published alternative plugin versions.')
    versions.add_argument('package', help='Installed configured or declared package.')
    for operation in ('netbox', 'plugins', 'stack'):
        cmd = upgrade_commands.add_parser(operation, help='Preview an upgrade plan (read-only by default).')
        cmd.add_argument('--details', action='store_true', help='Show compatibility evidence and safeguards.')
        cmd.add_argument('--execute', action='store_true', help='Execute only with the approved fingerprint.')
        cmd.add_argument('--approve-plan', help='Exact fingerprint from the preview.')
        cmd.add_argument('--accept-unknowns', action='store_true', help='Explicitly acknowledge uncertain compatibility.')
        cmd.add_argument('--health-url', help='NetBox API status URL; required for execution.')
        cmd.add_argument('--prompt-authorization', action='store_true', help='Read an authorization header without echo.')
        if operation == 'netbox':
            cmd.add_argument('--version', help='Target stable NetBox release; defaults to newest.')
        else:
            selector = cmd.add_mutually_exclusive_group()
            selector.add_argument('--plugin', action='append', help='Package or package==version; repeat to select more.')
            if operation == 'plugins':
                selector.add_argument('--all', action='store_true', help='Select all discovered plugins.')
                cmd.add_argument('--version', help='Version for a single selected plugin.')
            else:
                selector.add_argument('--plugins', choices=('all',), help='Select all discovered plugins.')
                cmd.add_argument('--netbox-version', help='Target stable NetBox release; defaults to newest.')

    top.add_parser('platform', help='Show OS family, systemd units and PostgreSQL tools (read-only).')
    self_upgrade = top.add_parser('self-upgrade', help='Inspect and upgrade netbox-ops releases.')
    self_actions = self_upgrade.add_subparsers(dest='action')
    self_actions.add_parser('list', help='List official stable releases (read-only).')
    details = self_actions.add_parser('show', help='Show a release changelog (read-only).')
    details.add_argument('version', help='Stable release version, e.g. v0.6.0.')
    for operation in ('plan', 'install'):
        command = self_actions.add_parser(operation, help=f'{operation.capitalize()} a verified upgrade step.')
        command.add_argument('version', help='Target stable release.')
        if operation == 'install':
            command.add_argument('--confirm', help='Exact confirmation token: UPGRADE vX.Y.Z')
    self_actions.add_parser('status', help='Show the last upgrade result.')
    return parser


def display_status(systemd: SystemdServices, names: Sequence[str], t: Translator) -> int:
    states = systemd.status(list(names))
    print(t('status_header'))
    for state in states:
        print(f'{state.name:<13} {state.unit:<25} {state.active:<13} '
              f'{state.substate:<13} {state.enabled:<12} {state.loaded}')
    if any(not s.healthy for s in states):
        print(t('status_warning'), file=sys.stderr)
        return 1
    return 0



def confirmed(t: Translator, names: Sequence[str], action: str = 'restart') -> bool:
    token = challenge(action, names)
    print(t('action_target', names=', '.join(names), action=t('action_' + action)))
    print(t('startup_warning') if action in ('enable', 'disable') else t('restart_warning'))
    try:
        answer = input(t('action_confirm', token=token))
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == token


def perform_service_action(systemd: SystemdServices, action: str,
                           names: Sequence[str], t: Translator, yes: bool = False,
                           allow_critical: bool = False) -> int:
    if action not in ('start', 'stop', 'restart', 'enable', 'disable'):
        raise ValueError(f'Unsupported action: {action}')
    if os.geteuid() != 0:
        print(t('root_required_actions'), file=sys.stderr)
        return 2
    selected = list(dict.fromkeys(names))
    if not selected:
        print(t('service_error', error='No services selected'), file=sys.stderr)
        return 2
    if yes and CRITICAL_SERVICES.intersection(selected) and not allow_critical:
        print(t('critical_flag_required'), file=sys.stderr)
        return 2
    if not yes and not confirmed(t, selected, action):
        print(t('cancelled_action'), file=sys.stdout)
        return 2
    try:
        with ExclusiveLock():
            who = f'uid={os.getuid()} sudo_user={os.environ.get("SUDO_USER", "unknown")}'
            write_audit(f'{action.upper()} requested services={",".join(selected)} {who}')
            try:
                require_service_action_allowed(action, selected)
                states = getattr(systemd, action)(selected)
            except ServiceError as exc:
                write_audit(f'{action.upper()} failure services={",".join(selected)} error={exc}')
                raise
            write_audit(f'{action.upper()} success services={",".join(selected)}')
    except (ServiceError, OSError) as exc:
        print(t('service_error', error=exc), file=sys.stderr)
        return 1
    for state in states:
        result_state = state.enabled if action in ('enable', 'disable') else state.active
        print(t('action_success', action=t('action_' + action), name=state.name,
                state=result_state))
    return 0


def perform_restart(systemd: SystemdServices, names: Sequence[str], t: Translator,
                    yes: bool = False, allow_critical: bool = False) -> int:
    """Backward-compatible entrypoint for existing restart workflows."""
    return perform_service_action(systemd, 'restart', names, t, yes, allow_critical)



def not_implemented(t: Translator, name: str) -> int:
    print(t('not_implemented', command=name))
    return 3


def confirm_restore(kind: str, source: Path, t: Translator, only: str = 'all') -> bool:
    token = f'RESTORE {kind.upper()}' + (f' {only.upper()}' if kind in ('combined', 'full') else '')
    print(t('backup_restore_warning', kind=kind, path=str(source)))
    try:
        return input(t('backup_restore_confirmation', token=token)) == token
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def perform_backup_action(group: str, action: str, cfg: Configuration,
                          t: Translator, *, source: Path | None = None,
                          yes: bool = False, acknowledge: bool = False,
                          accept_unknown: bool = False, allow_mismatch: bool = False,
                          kind: str | None = None, only: str = 'all',
                          config_path: Path = CONFIG_PATH) -> int:
    from .backups import BackupError, BackupManager
    manager = BackupManager(cfg)
    try:
        if group == 'backup' and action == 'list':
            for path in manager.list_backups(kind):
                print(path)
            return 0
        if group == 'backup' and action == 'delete':
            if os.geteuid() != 0:
                print(t('root_required_actions'), file=sys.stderr)
                return 2
            token = 'DELETE BACKUP'
            if not yes:
                try:
                    if input(f'{token} ({source or manager.directory})? Type {token}: ') != token:
                        return 2
                except (EOFError, KeyboardInterrupt):
                    return 2
            released = manager.delete_backup(source)
            print(f'Deleted {source} ({released} bytes)')
            return 0
        if group == 'backup' and action == 'verify':
            manifest = manager.verify(kind or '', source)
            print(t('backup_verified', path=source,
                    version=(manifest or {}).get('netbox_version') or '?'))
            return 0
        if group == 'backup':
            if action == 'manage':
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    print('backup manage requires interactive terminal', file=sys.stderr)
                    return 2
                from .tui import run_tui
                return run_tui(t, config_path, initial_view='backups')
            report = manager.backup(action)
            print(t('backup_created', path=report.path))
            print(report.detail)
            return 0
        if group == 'restore':
            if source is None:
                print(t('backup_missing_source'), file=sys.stderr)
                return 2
            # Reject incorrect combinations, including --acknowledge-data-loss alone.
            if yes != acknowledge:
                print(t('backup_flags_required'), file=sys.stderr)
                return 2
            if not yes and not confirm_restore(action, source, t, only):
                print(t('cancelled_action'))
                return 2
            report = manager.restore(action, source, only=only, accept_unknown=accept_unknown,
                                     allow_mismatch=allow_mismatch)
            print(t('backup_restored', path=report.path))
            if report.detail:
                print(report.detail)
            return 0
    except (BackupError, OSError, ValueError, KeyError) as exc:
        print(t('backup_error', error=exc), file=sys.stderr)
        return 1
    return 3


def dispatch(args: argparse.Namespace, t: Translator) -> int:
    action = getattr(args, 'action', '')
    if args.command == 'upgrade':
        from .upgrade_ui import run_cli
        return run_cli(args, load_config(args.config), t)
    if args.command == 'self-upgrade' and action in ('list', 'show'):
        from .self_upgrade_ui import run_catalogue
        return run_catalogue(action, t, target=getattr(args, 'version', None))
    if args.command == 'self-upgrade' and action in ('plan', 'install', 'status'):
        from .self_upgrade_ui import run_execution
        return run_execution(action, t, target=getattr(args, 'version', None),
                             confirmation=getattr(args, 'confirm', None))
    if args.command == 'platform':
        from .platform import detect_platform, pg_binary, PlatformError
        profile = detect_platform()
        print(f'Linux: {profile.distro} {profile.version} (family: {profile.family})')
        print(f'Python: {sys.version_info.major}.{sys.version_info.minor}')
        if profile.family not in ('rhel', 'debian'):
            print('Unsupported OS family', file=sys.stderr)
            return 1
        config = load_config(args.config)
        for name in SERVICE_NAMES:
            print(f'  {name:<13} {config.units[name]}')
        for name in ('psql', 'pg_dump', 'pg_restore', 'createdb', 'dropdb'):
            try:
                binary = pg_binary(name, config.pg_bin_directory)
            except PlatformError as exc:
                print(f'  {name:<13} unavailable: {exc}')
            else:
                print(f'  {name:<13} {binary}')
        return 0
    if args.command in ('backup', 'restore'):
        config = load_config(args.config)
        return perform_backup_action(
            args.command, action, config, t,
            source=getattr(args, 'file', None),
            yes=getattr(args, 'yes', False),
            acknowledge=getattr(args, 'acknowledge_data_loss', False),
            accept_unknown=getattr(args, 'accept_unknown_version', False),
            allow_mismatch=getattr(args, 'allow_version_mismatch', False),
            kind=getattr(args, 'kind', None),
            only=getattr(args, 'only', 'all'), config_path=args.config)
    if args.command == 'services' and action == 'manage':
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print('services manage requires an interactive terminal', file=sys.stderr)
            return 2
        from .tui import run_tui
        return run_tui(t, args.config, initial_view='manage')
    if args.command != 'services' or action not in ('status', 'logs', 'start', 'stop', 'restart', 'enable', 'disable'):
        name = f'{args.command} {action}'.strip()
        return not_implemented(t, name)
    config = load_config(args.config)
    systemd = SystemdServices(config)
    if action == 'status':
        return display_status(systemd, args.service or SERVICE_NAMES, t)
    if action == 'logs':
        records = systemd.logs(args.service, args.lines)
        if not records:
            print(t('logs_empty'))
        else:
            for record in records:
                print(record)
        return 0
    if action == 'restart':
        names = SERVICE_NAMES if args.all else (args.service or DEFAULT_RESTART)
    else:
        names = args.service
    if action == 'restart':
        return perform_restart(systemd, names, t, yes=args.yes,
                               allow_critical=args.allow_critical)
    return perform_service_action(systemd, action, names, t, yes=args.yes,
                                  allow_critical=args.allow_critical)


def menu_language(t: Translator) -> Translator:
    print(t('language_prompt'))
    print('  1) Português (Portugal)')
    print('  2) English')
    try:
        response = input('> ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return t
    selected = {'1': 'pt', '2': 'en', 'pt': 'pt', 'en': 'en'}.get(response.lower())
    if not selected:
        print(t('invalid_choice'))
        return t
    result = Translator(selected)
    print(result('language_changed'))
    return result


def menu_manage(t: Translator, config_path: Path) -> None:
    """Numbered fallback for terminals without curses; no surprise default targets."""
    print(t('tui_manage_title'))
    for idx, name in enumerate(SERVICE_NAMES, 1):
        print(f'  {idx}) {name}')
    print(t('text_manage_prompt'))
    try:
        raw = input(t('prompt')).strip()
    except (EOFError, KeyboardInterrupt):
        return
    choices = [piece.strip() for piece in raw.split(',')]
    if not choices or any(piece not in {str(i) for i in range(1, 6)} for piece in choices):
        print(t('invalid_choice'))
        return
    names = [SERVICE_NAMES[int(piece)-1] for piece in dict.fromkeys(choices)]
    print('  1) ' + t('action_start'))
    print('  2) ' + t('action_stop'))
    print('  3) ' + t('action_restart'))
    print('  4) ' + t('action_enable'))
    print('  5) ' + t('action_disable'))
    try:
        choice = input(t('prompt')).strip()
    except (EOFError, KeyboardInterrupt):
        return
    action = {'1': 'start', '2': 'stop', '3': 'restart',
              '4': 'enable', '5': 'disable'}.get(choice)
    if not action:
        print(t('invalid_choice'))
        return
    perform_service_action(SystemdServices(load_config(config_path)), action,
                           order_services(action, names), t)


def interactive_menu(t: Translator, config_path: Path = CONFIG_PATH) -> int:
    lookup = {number: (group, action, key) for number, group, action, key in MENU_ACTIONS}
    while True:
        print(f'\n=== netbox-ops {__version__} ===')
        for number, _, _, key in MENU_ACTIONS:
            print(f'{int(number):2d}) {t(key)}')
        print(t('menu_language'))
        print(t('menu_exit'))
        try:
            choice = input(t('prompt')).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == '0':
            return 0
        if choice == 'l':
            t = menu_language(t)
            continue
        if choice not in lookup:
            print(t('invalid_choice'))
            continue
        group, action, _ = lookup[choice]
        if group == 'self-upgrade':
            from .self_upgrade_ui import text_menu
            result = text_menu(t)
            if result is not None:
                return result
            continue
        try:
            cfg = load_config(config_path)
            if (group, action) == ('services', 'manage'):
                menu_manage(t, config_path)
            elif (group, action) == ('backup', 'manage'):
                print('  1) Backup DB     2) Backup media     3) Backup combined')
                print('  4) List backups (CLI restore/delete requires explicit file and confirmation)')
                option = input(t('prompt')).strip()
                if option in ('1', '2', '3'):
                    kind = {'1': 'db', '2': 'media', '3': 'combined'}[option]
                    if input(f'Create {kind} backup? [yes/NO] ').lower() == 'yes':
                        perform_backup_action('backup', kind, cfg, t)
                elif option == '4':
                    perform_backup_action('backup', 'list', cfg, t)
            elif group == 'upgrade':
                from .upgrade_ui import run_cli
                from shlex import split
                print(t('upgrade_text_help'))
                raw = input(t('prompt'))
                try:
                    options = build_parser().parse_args(['upgrade', action, *split(raw)])
                except (SystemExit, ValueError):
                    continue
                run_cli(options, cfg, t)
            else:
                not_implemented(t, f'{group} {action}'.strip())
        except (ServiceError, ConfigError, OSError) as exc:
            print(t('service_error', error=exc), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    t = Translator(args.lang)
    if args.command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return 0
        if not args.no_tui and sys.stdout.isatty() and os.environ.get('TERM', '') not in ('', 'dumb'):
            try:
                from .tui import run_tui
                return run_tui(t, args.config)
            except (ImportError, OSError) as exc:
                print(f'TUI indisponível ({exc}); a utilizar menu de texto.', file=sys.stderr)
            except Exception as exc:
                # An unsupported terminal can fail during curses initialisation.
                import curses
                if not isinstance(exc, curses.error):
                    raise
                print(f'TUI indisponível ({exc}); a utilizar menu de texto.', file=sys.stderr)
        return interactive_menu(t, args.config)
    try:
        return dispatch(args, t)
    except (ServiceError, ConfigError, OSError) as exc:
        print(t('service_error', error=exc), file=sys.stderr)
        return 1
