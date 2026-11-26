"""Shared menu definitions; the TUI groups upgrades without changing CLI paths."""

# Keep actual command paths and labels in one place.
_COMMON_ACTIONS = (
    ('services', 'manage', 'menu_2'),
    ('backup', 'manage', 'menu_backup_manage'),
    ('updates', 'check', 'menu_9'),
)
UPGRADE_ACTIONS = (
    ('netbox', 'menu_10'),
    ('plugins', 'menu_11'),
    ('stack', 'menu_12'),
)
_SELF_UPGRADE = ('self-upgrade', '', 'menu_13')

# Preserve the numbering and direct commands of the text-mode CLI menu.
MENU_ACTIONS = tuple(
    (str(index), *entry) for index, entry in enumerate(
        (*_COMMON_ACTIONS, *(('upgrade', action, label) for action, label in UPGRADE_ACTIONS),
         _SELF_UPGRADE), 1)
)
TUI_MENU_ACTIONS = tuple(
    (str(index), *entry) for index, entry in enumerate(
        (*_COMMON_ACTIONS[:2], ('upgrade', 'menu', 'tui_main_upgrade'),
         ('self-upgrade', '', 'tui_main_self_upgrade')), 1)
)
