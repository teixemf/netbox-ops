#!/usr/bin/env python3
"""Publish a validated candidate; never replace tags or existing assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r'[0-9a-f]{40}')
_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
_MAX_RESPONSE = 8 * 1024 * 1024


class PublishError(RuntimeError):
    pass


class ApiError(PublishError):
    def __init__(self, status, method, path):
        self.status = status
        super().__init__(f'GitHub API {method} {path} failed with HTTP {status}')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    """Fixed GitHub hosts; never forward credentials through redirects."""
    def __init__(self, repo, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token:
            raise PublishError('Missing or invalid repository credentials')
        self.repo = repo
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, payload=None, *, binary=None):
        host = 'https://uploads.github.com' if binary is not None else 'https://api.github.com'
        headers = {'Authorization': f'Bearer {self.token}',
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'netbox-ops-release'}
        data = None
        if binary is not None:
            headers['Content-Type'] = 'application/octet-stream'
            data = binary
        elif payload is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(payload).encode()
        request = Request(f'{host}/repos/{self.repo}{path}', data=data,
                          headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise PublishError('Oversized GitHub API response')
            return json.loads(raw)
        except HTTPError as exc:
            raise ApiError(exc.code, method, path) from None
        except (URLError, OSError, ValueError) as exc:
            raise PublishError(f'Unable to read GitHub API response: {method} {path}') from exc

    def optional(self, path):
        try:
            return self.request('GET', path)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def pages(self, path):
        for page in range(1, 101):
            rows = self.request('GET', f'{path}?per_page=100&page={page}')
            if not isinstance(rows, list) or len(rows) > 100:
                raise PublishError('Invalid paginated GitHub response')
            yield from rows
            if len(rows) < 100:
                return
        raise PublishError('GitHub pagination limit reached')


def require_main(api, commit):
    ref = api.request('GET', '/git/ref/heads/main')
    obj = ref.get('object', {})
    if obj.get('type') != 'commit' or obj.get('sha') != commit:
        raise PublishError('main changed since validation; run the workflow again')


def tag_exists(api, tag, commit):
    ref = api.optional(f'/git/ref/tags/{tag}')
    if ref is None:
        return False
    obj = ref.get('object', {})
    if obj.get('type') != 'tag' or not _SHA.fullmatch(str(obj.get('sha', ''))):
        raise PublishError('Existing tag is not an annotated release tag')
    annotated = api.request('GET', f"/git/tags/{obj['sha']}")
    target = annotated.get('object', {})
    if annotated.get('tag') != tag or target.get('type') != 'commit' or target.get('sha') != commit:
        raise PublishError('Existing tag points to a different commit; refusing to move it')
    return True


def matching_release(api, tag):
    matches = [row for row in api.pages('/releases') if row.get('tag_name') == tag]
    if len(matches) > 1:
        raise PublishError('Multiple releases use the planned tag')
    return matches[0] if matches else None


def release_id(release, tag, notes):
    number = release.get('id')
    if (type(number) is not int or number <= 0
            or release.get('tag_name') != tag or release.get('name') != f'netbox-ops {tag}'
            or release.get('body') != notes or release.get('prerelease') is not False
            or type(release.get('draft')) is not bool):
        raise PublishError('Existing release metadata differs; review it manually')
    return number


def checked_assets(api, number, expected):
    seen = set()
    for asset in api.pages(f'/releases/{number}/assets'):
        name = asset.get('name')
        if name not in expected or name in seen:
            raise PublishError('Unexpected or duplicate release asset; review it manually')
        data = expected[name]
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        if (asset.get('state') != 'uploaded' or asset.get('size') != len(data)
                or asset.get('digest') != digest):
            raise PublishError(f'Existing asset differs or cannot be verified: {name}')
        seen.add(name)
    return seen


def publish(api, *, tag, commit, notes, assets):
    """Resume only matching drafts; publish only after every asset is verified."""
    if not _TAG.fullmatch(tag) or not _SHA.fullmatch(commit) or not notes.strip():
        raise PublishError('Invalid release tag, commit or notes')
    stem = f'netbox-ops-{tag}'
    if set(assets) != {stem + '.tar.gz', stem + '.tar.gz.sha256', stem + '.compatibility.json'}:
        raise PublishError('Invalid publication asset set')
    require_main(api, commit)
    exists = tag_exists(api, tag, commit)
    release = matching_release(api, tag)
    if release is not None:
        if not exists:
            raise PublishError('Existing release has no verified annotated tag')
        number = release_id(release, tag, notes)
        seen = checked_assets(api, number, assets)
        if not release['draft']:
            if seen != set(assets):
                raise PublishError('Published release is incomplete; refusing to modify it')
            return 'already published and verified'
    else:
        if not exists:
            obj = api.request('POST', '/git/tags', {
                'tag': tag, 'message': f'netbox-ops {tag}', 'object': commit,
                'type': 'commit', 'tagger': {'name': 'github-actions[bot]',
                    'email': '41898282+github-actions[bot]@users.noreply.github.com',
                    'date': datetime.now(timezone.utc).isoformat()},
            })
            sha = obj.get('sha')
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise PublishError('Invalid annotated tag response')
            api.request('POST', '/git/refs', {'ref': f'refs/tags/{tag}', 'sha': sha})
        if not tag_exists(api, tag, commit):
            raise PublishError('Annotated tag could not be verified')
        release = api.request('POST', '/releases', {
            'tag_name': tag, 'target_commitish': commit, 'name': f'netbox-ops {tag}',
            'body': notes, 'draft': True, 'prerelease': False,
        })
        number = release_id(release, tag, notes)
        if not release['draft']:
            raise PublishError('Expected a draft release')
        seen = checked_assets(api, number, assets)
    for name, data in assets.items():
        if name not in seen:
            api.request('POST', f'/releases/{number}/assets?name={quote(name, safe="")}',
                        binary=data)
    if checked_assets(api, number, assets) != set(assets):
        raise PublishError('Draft is missing validated assets; retry safely')
    require_main(api, commit)
    if not tag_exists(api, tag, commit):
        raise PublishError('Release tag disappeared before publication')
    current = api.request('GET', f'/releases/{number}')
    release_id(current, tag, notes)
    if not current['draft'] or current['id'] != number:
        raise PublishError('Draft was changed concurrently; review the release')
    published = api.request('PATCH', f'/releases/{number}', {'draft': False})
    release_id(published, tag, notes)
    if (published['draft'] or published['id'] != number
            or checked_assets(api, number, assets) != set(assets)
            or not tag_exists(api, tag, commit)):
        raise PublishError('Publication could not be verified')
    return 'published and verified'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('archive', type=Path)
    args = parser.parse_args(argv)
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true'
                or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
            raise PublishError('Publication is restricted to a manual main workflow')
        commit = os.environ.get('GITHUB_SHA', '')
        if not _TAG.fullmatch(args.tag) or not _SHA.fullmatch(commit):
            raise PublishError('Invalid planned tag or validated commit')
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                       text=True).strip()
        if head != commit:
            raise PublishError('Checkout differs from the validated commit')
        subprocess.run([sys.executable, str(ROOT / 'scripts/release-candidate.py'),
                        'validate', str(args.archive), args.tag], check=True)
        stem = f'netbox-ops-{args.tag}'
        assets = {name: args.archive.with_name(name).read_bytes()
                  for name in (stem + '.tar.gz', stem + '.tar.gz.sha256',
                               stem + '.compatibility.json')}
        notes = args.archive.with_name(stem + '.notes.md').read_text(encoding='utf-8')
        api = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
        print(f'{args.tag}: {publish(api, tag=args.tag, commit=commit, notes=notes, assets=assets)}')
        return 0
    except (PublishError, OSError, ValueError, TypeError, KeyError,
            subprocess.CalledProcessError) as exc:
        print(f'Release publication stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
