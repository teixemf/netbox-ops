"""Read the NetBox application release, never the Django management version.

Read release.yaml for recent installations and a literal settings.VERSION for
older releases. Never import the NetBox venv or its local configuration.
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
        if release_file.is_symlink():
            return ''
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
            assignments = [node for node in ast.walk(tree)
                           if isinstance(node, ast.Name) and node.id == 'VERSION'
                           and isinstance(node.ctx, (ast.Store, ast.Del))]
            if len(assignments) != 1:
                return ''
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and node.targets[0] is assignments[0]
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and re.fullmatch(r'\d+\.\d+\.\d+', node.value.value)):
                    return node.value.value
        except (OSError, UnicodeError, SyntaxError):
            pass
    except (OSError, UnicodeError):
        pass
    return ''
