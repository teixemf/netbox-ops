#!/usr/bin/env python3
"""Build and verify release assets using the same strict consumer contract.

Only the committed archive supplies the version, release notes and compatibility
requirements. This tool never creates a Git tag or publishes a release.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_LIMIT = 1024 * 1024
_HEADING = re.compile(r'^## \[([0-9]+\.[0-9]+\.[0-9]+)\] — .+$', re.MULTILINE)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate release requirements field')
        result[key] = value
    return result


def read_member(bundle, name):
    member = bundle.getmember(f'netbox-ops/{name}')
    if not member.isfile() or not 0 < member.size <= _LIMIT:
        raise ValueError(f'Invalid package metadata file: {name}')
    with bundle.extractfile(member) as stream:
        return stream.read(_LIMIT + 1).decode('utf-8')


def candidate_contents(archive, tag):
    from netbox_ops.self_upgrade_archive import validate_archive
    from netbox_ops.self_upgrade_manifest import encode_manifest, parse_manifest
    from netbox_ops.self_upgrade_releases import Version

    version = Version.parse(tag)
    if archive.name != f'netbox-ops-{tag}.tar.gz':
        raise ValueError('Archive filename does not match planned tag')
    validate_archive(archive)
    with tarfile.open(archive, 'r:gz') as bundle:
        tree = ast.parse(read_member(bundle, 'netbox_ops/__init__.py'))
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == '__version__'
                          for target in node.targets)]
        if len(values) != 1 or ast.literal_eval(values[0]) != str(version):
            raise ValueError('Package version does not match planned tag')
        readme = read_member(bundle, 'README.md')
        if f'**Current version: {version} —' not in readme:
            raise ValueError('README version does not match planned tag')
        changelog = read_member(bundle, 'CHANGELOG.md')
        headings = list(_HEADING.finditer(changelog))
        if (not headings or headings[0].group(1) != str(version)
                or sum(heading.group(1) == str(version) for heading in headings) != 1):
            raise ValueError('Latest changelog entry does not match planned tag')
        end = headings[1].start() if len(headings) > 1 else len(changelog)
        section = changelog[headings[0].end():end].strip()
        if not section:
            raise ValueError('Release notes are empty')
        notes = (headings[0].group(0) + '\n\n' + section + '\n').encode('utf-8')
        scripts = ['install.sh', 'upgrade.sh']
        if 'netbox-ops/uninstall.sh' in bundle.getnames():
            scripts.append('uninstall.sh')
        for name in scripts:
            mode = bundle.getmember(f'netbox-ops/{name}').mode
            if mode & 0o111 != 0o111:
                raise ValueError(f'Installer script is not executable: {name}')
        policy = json.loads(read_member(bundle, 'netbox_ops/release_requirements.json'),
                            object_pairs_hook=unique_keys)
        if not isinstance(policy, dict) or set(policy) != {
                'python_min', 'python_max_exclusive', 'linux_families',
                'layouts', 'required_intermediates'}:
            raise ValueError('Invalid committed release requirements')
        raw = json.dumps({'schema_version': 1, 'version': tag, **policy}).encode()
        manifest = encode_manifest(parse_manifest(raw, version))
    with archive.open('rb') as stream:
        checksum = f'{hashlib.file_digest(stream, "sha256").hexdigest()}  {archive.name}\n'.encode()
    return checksum, manifest, notes


def asset_paths(archive):
    stem = archive.name.removesuffix('.tar.gz')
    return (archive.with_name(archive.name + '.sha256'),
            archive.with_name(stem + '.compatibility.json'),
            archive.with_name(stem + '.notes.md'))


def build_assets(archive, tag):
    contents = candidate_contents(archive, tag)
    for path, data in zip(asset_paths(archive), contents, strict=True):
        # The shell builder uses a private temporary output directory.
        path.write_bytes(data)
    validate_assets(archive, tag)


def validate_assets(archive, tag):
    from netbox_ops.self_upgrade_archive import verify_release_archive
    from netbox_ops.self_upgrade_manifest import parse_manifest
    from netbox_ops.self_upgrade_releases import Version

    expected = candidate_contents(archive, tag)
    for path, data in zip(asset_paths(archive), expected, strict=True):
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size != len(data) or path.read_bytes() != data):
            raise ValueError(f'Release asset differs from committed package: {path.name}')
    checksum, manifest, _ = asset_paths(archive)
    verify_release_archive(archive, checksum.read_bytes())
    parse_manifest(manifest.read_bytes(), Version.parse(tag))


def main(argv=None):
    from netbox_ops.self_upgrade_archive import ReleaseArchiveError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'validate'))
    parser.add_argument('archive', type=Path)
    parser.add_argument('tag')
    args = parser.parse_args(argv)
    try:
        if args.action == 'build':
            build_assets(args.archive, args.tag)
        else:
            validate_assets(args.archive, args.tag)
    except (ReleaseArchiveError, OSError, ValueError, TypeError, KeyError,
            SyntaxError, tarfile.TarError, RecursionError) as exc:
        print(f'Release candidate rejected: {exc}', file=sys.stderr)
        return 1
    print(f'Validated release candidate {args.tag}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
