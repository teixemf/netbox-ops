"""Read the NetBox application release, never the Django management version.

For NetBox source/Git installations release.yaml is maintained by NetBox's
release process; do not import the NetBox venv or its local configuration.
"""
from __future__ import annotations
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
    except (OSError, UnicodeError):
        pass
    return ''
