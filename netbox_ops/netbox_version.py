"""Read the NetBox application release, never the Django management version.

For NetBox source/Git installations release.yaml is maintained by NetBox's
release process; do not import the NetBox venv or its local configuration.
"""
from __future__ import annotations
import ast
import re
from pathlib import Path

DEFAULT_RELEASE = Path('/opt/netbox/netbox/release.yaml')
_VERSION = re.compile(r"^\s*version\s*:\s*['\"]?(v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.]+)?)['\"]?\s*(?:#.*)?$")

def read_netbox_version(release_file: Path = DEFAULT_RELEASE) -> str:
    """Return a release version, or empty string if untrusted/unavailable."""
    try:
        with release_file.open(encoding='utf-8') as f:
            for i, line in enumerate(f):
                if i > 80:
                    break
                match = _VERSION.fullmatch(line.strip())
                if match:
                    return match.group(1).removeprefix('v')
    except FileNotFoundError:
        # Older releases store a literal VERSION in netbox/settings.py.
        # Do not import or execute settings, or fall back on malformed YAML.
        try:
            settings = release_file.parent / 'netbox/settings.py'
            if settings.is_symlink() or settings.stat().st_size > 1024 * 1024:
                return ''
            tree = ast.parse(settings.read_text(encoding='utf-8'))
            values = [node.value.value for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets)
                      and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
            if len(values) == 1 and re.fullmatch(r'\d+\.\d+\.\d+', values[0]):
                return values[0]
        except (OSError, UnicodeError, SyntaxError):
            pass
    except (OSError, UnicodeError):
        pass
    return ''
