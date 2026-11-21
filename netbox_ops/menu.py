"""Shared main-menu definitions for the CLI and TUI."""

# Menu numbers and CLI paths are maintained together. Stubs never touch NetBox.
MENU_ACTIONS = (
    ('1', 'services', 'manage', 'menu_2'),
    ('2', 'backup', 'manage', 'menu_backup_manage'),
    ('3', 'updates', 'check', 'menu_9'),
    ('4', 'upgrade', 'netbox', 'menu_10'),
    ('5', 'upgrade', 'plugins', 'menu_11'),
    ('6', 'upgrade', 'stack', 'menu_12'),
    ('7', 'self-upgrade', '', 'menu_13'),
)
