"""Report changed executable Python lines from coverage JSON; fail on missing data."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SHA = re.compile(r'[0-9a-f]{40}')
HUNK = re.compile(rb'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '--literal-pathspecs', *args], cwd=root)


def checked_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value) or value == '0' * 40:
        raise ValueError('Invalid commit SHA')
    return value


def pr_base(root: Path, event: dict) -> str:
    """Require the actual GitHub PR merge checkout, not a moving branch ref."""
    pr = event['pull_request']
    base = checked_sha(pr['base']['sha'])
    head = checked_sha(pr['head']['sha'])
    parents = git(root, 'show', '-s', '--format=%P', 'HEAD').decode().split()
    if parents != [base, head]:
        raise ValueError('Checkout does not match the PR base and head')
    return base


def changed_lines(root: Path, base: str, path: str) -> set[int]:
    patch = git(root, 'diff', '--no-renames', '--no-ext-diff', '--no-textconv',
                '--unified=0', base, 'HEAD', '--', path)
    lines = set()
    for row in patch.splitlines():
        match = HUNK.match(row)
        if match:
            start = int(match[1])
            count = int(match[2]) if match[2] is not None else 1
            lines.update(range(start, start + count))
    return lines


def line_set(value: list[int]) -> set[int]:
    if not isinstance(value, list) or any(type(n) is not int or n < 1 for n in value):
        raise ValueError('Invalid coverage line data')
    return set(value)


def report(root: Path, base: str, coverage: dict) -> dict:
    base = checked_sha(base)
    git(root, 'merge-base', '--is-ancestor', base, 'HEAD')
    # Reports must describe the checked-out package, not a modified worktree.
    if git(root, 'status', '--porcelain', '--untracked-files=all', '--', 'netbox_ops/'):
        raise ValueError('Package worktree is not clean')
    files = coverage['files']
    if not isinstance(files, dict):
        raise ValueError('Invalid coverage file map')
    raw = git(root, 'diff', '--name-status', '-z', '--no-renames', base, 'HEAD',
              '--', 'netbox_ops/')
    fields = raw.split(b'\0')
    if fields.pop() != b'' or len(fields) % 2:
        raise ValueError('Invalid changed file list')
    result = {}
    for status, path_bytes in zip(fields[::2], fields[1::2], strict=True):
        path = path_bytes.decode('utf-8')
        if not path.endswith('.py') or status == b'D':
            continue
        if status not in {b'A', b'M'}:
            raise ValueError(f'Unsupported Python change: {path}')
        entry = git(root, 'ls-tree', 'HEAD', '--', path)
        if not entry.startswith((b'100644 blob ', b'100755 blob ')):
            raise ValueError(f'Not a regular Python source file: {path}')
        # Even comment-only changes need a report entry: missing data is not N/A.
        if path not in files:
            raise ValueError(f'Missing coverage for {path}')
        covered = line_set(files[path]['executed_lines'])
        missed = line_set(files[path]['missing_lines'])
        if covered & missed:
            raise ValueError(f'Conflicting coverage for {path}')
        changed = changed_lines(root, base, path)
        relevant = changed & (covered | missed)
        if relevant:
            result[path] = {
                'covered': sorted(relevant & covered),
                'missing': sorted(relevant & missed),
            }
    covered_count = sum(len(item['covered']) for item in result.values())
    missing_count = sum(len(item['missing']) for item in result.values())
    total = covered_count + missing_count
    return {
        'base': base,
        'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
        'covered': covered_count,
        'missing': missing_count,
        'total': total,
        'percent': 100 * covered_count / total if total else None,
        'files': result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument('--base')
    base.add_argument('--pr-event', type=Path)
    parser.add_argument('--coverage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel').decode().strip())
    try:
        selected = (pr_base(root, json.loads(args.pr_event.read_text(encoding='utf-8')))
                    if args.pr_event else args.base)
        data = report(root, selected, json.loads(args.coverage.read_text(encoding='utf-8')))
        args.output.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Cannot report differential coverage: {exc}\n')
    if data['percent'] is None:
        print('Changed executable Python lines: N/A')
    else:
        print(f"Changed executable Python lines: {data['covered']}/{data['total']} "
              f"({data['percent']:.2f}%)")


if __name__ == '__main__':
    main()
