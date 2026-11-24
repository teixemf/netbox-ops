"""Offline publication tests: no GitHub write calls leave the test process."""
from __future__ import annotations

import hashlib
import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'release_publisher', Path(__file__).resolve().parents[1] / 'scripts/publish-release.py')
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)

TAG = 'v0.6.0'
COMMIT = 'a' * 40
TAG_SHA = 'b' * 40
NOTES = '## [0.6.0] — Example\n\n- New feature.\n'
ASSETS = {f'netbox-ops-{TAG}{suffix}': data for suffix, data in (
    ('.tar.gz', b'archive'), ('.tar.gz.sha256', b'checksum'), ('.compatibility.json', b'manifest'))}


class FakeGitHub:
    def __init__(self):
        self.main = COMMIT
        self.ref = None
        self.annotated = None
        self.release = None
        self.assets = []
        self.writes = []
        self.fail_upload = None

    def optional(self, path):
        if path == f'/git/ref/tags/{TAG}':
            return deepcopy(self.ref)
        raise AssertionError(path)

    def pages(self, path):
        if path == '/releases':
            return [deepcopy(self.release)] if self.release is not None else []
        if path == '/releases/1/assets':
            return deepcopy(self.assets)
        raise AssertionError(path)

    def request(self, method, path, payload=None, *, binary=None):
        if method == 'GET':
            if path == '/git/ref/heads/main':
                return {'object': {'type': 'commit', 'sha': self.main}}
            if path == f'/git/tags/{TAG_SHA}':
                return deepcopy(self.annotated)
            if path == '/releases/1':
                return deepcopy(self.release)
            raise AssertionError(path)
        self.writes.append((method, path))
        if method == 'POST' and path == '/git/tags':
            self.annotated = {'tag': payload['tag'], 'object': {
                'type': payload['type'], 'sha': payload['object']}, 'sha': TAG_SHA}
            return deepcopy(self.annotated)
        if method == 'POST' and path == '/git/refs':
            assert self.ref is None, 'Attempt to move an existing tag'
            self.ref = {'object': {'type': 'tag', 'sha': payload['sha']}}
            return deepcopy(self.ref)
        if method == 'POST' and path == '/releases':
            assert self.release is None and payload['draft'] is True
            self.release = {'id': 1, **payload}
            return deepcopy(self.release)
        if method == 'POST' and path.startswith('/releases/1/assets?name='):
            name = path.split('=', 1)[1]
            assert self.release['draft'], 'Upload to public release'
            assert name not in {a['name'] for a in self.assets}, 'Overwrite attempt'
            if name == self.fail_upload:
                raise publisher.ApiError(502, method, path)
            asset = {'name': name, 'state': 'uploaded', 'size': len(binary),
                     'digest': 'sha256:' + hashlib.sha256(binary).hexdigest()}
            self.assets.append(asset)
            return deepcopy(asset)
        if method == 'PATCH' and path == '/releases/1':
            assert len(self.assets) == 3, 'Published an incomplete release'
            self.release.update(payload)
            return deepcopy(self.release)
        raise AssertionError((method, path))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def publish(self):
        return publisher.publish(self.api, tag=TAG, commit=COMMIT, notes=NOTES, assets=ASSETS)

    def test_new_release_and_idempotent_retry(self):
        self.assertEqual(self.publish(), 'published and verified')
        self.assertFalse(self.api.release['draft'])
        self.assertEqual(self.api.annotated['object']['sha'], COMMIT)
        self.assertEqual(len(self.api.assets), 3)
        self.assertEqual(self.api.writes[0:3],
                         [('POST', '/git/tags'), ('POST', '/git/refs'), ('POST', '/releases')])
        self.assertEqual(self.api.writes[-1], ('PATCH', '/releases/1'))
        self.api.writes.clear()
        self.assertEqual(self.publish(), 'already published and verified')
        self.assertEqual(self.api.writes, [])

    def test_partial_upload_is_resumed_without_overwriting(self):
        names = list(ASSETS)
        self.api.fail_upload = names[1]
        with self.assertRaises(publisher.ApiError):
            self.publish()
        self.assertTrue(self.api.release['draft'])
        self.assertEqual(len(self.api.assets), 1)
        self.api.fail_upload = None
        self.api.writes.clear()
        self.assertEqual(self.publish(), 'published and verified')
        self.assertEqual(len(self.api.assets), 3)
        self.assertNotIn(('POST', f'/releases/1/assets?name={names[0]}'), self.api.writes)
        self.assertNotIn(('POST', '/git/refs'), self.api.writes)

    def test_existing_matching_tag_without_release_can_resume(self):
        self.api.ref = {'object': {'type': 'tag', 'sha': TAG_SHA}}
        self.api.annotated = {'tag': TAG, 'object': {'type': 'commit', 'sha': COMMIT}}
        self.assertEqual(self.publish(), 'published and verified')
        self.assertNotIn(('POST', '/git/tags'), self.api.writes)
        self.assertNotIn(('POST', '/git/refs'), self.api.writes)

    def test_lightweight_or_mismatched_tag_is_never_moved(self):
        for obj in ({'type': 'commit', 'sha': COMMIT}, {'type': 'tag', 'sha': TAG_SHA}):
            with self.subTest(obj=obj):
                self.api.ref = {'object': obj}
                self.api.annotated = {'tag': TAG, 'object': {'type': 'commit', 'sha': 'c' * 40}}
                with self.assertRaises(publisher.PublishError):
                    self.publish()
                self.assertEqual(self.api.writes, [])

    def test_mismatched_existing_asset_is_not_replaced(self):
        self.publish()
        for change in ({'digest': 'sha256:' + '0' * 64}, {'digest': None},
                       {'size': 0}, {'state': 'starter'}, {'name': 'unexpected.zip'}):
            with self.subTest(change=change):
                original = self.api.assets[0].copy()
                self.api.assets[0].update(change)
                self.api.writes.clear()
                with self.assertRaises(publisher.PublishError):
                    self.publish()
                self.assertEqual(self.api.writes, [])
                self.api.assets[0] = original

    def test_incomplete_published_release_or_changed_notes_are_refused(self):
        self.publish()
        self.api.writes.clear()
        original = self.api.assets.pop()
        with self.assertRaisesRegex(publisher.PublishError, 'incomplete'):
            self.publish()
        self.api.assets.append(original)
        self.api.release['body'] = 'Changed'
        with self.assertRaisesRegex(publisher.PublishError, 'metadata differs'):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_changed_main_blocks_before_writes(self):
        self.api.main = 'd' * 40
        with self.assertRaisesRegex(publisher.PublishError, 'main changed'):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_main_changes_after_upload_leaves_draft(self):
        request = self.api.request

        def change_after_upload(method, path, payload=None, *, binary=None):
            response = request(method, path, payload, binary=binary)
            if binary is not None:
                self.api.main = 'd' * 40
            return response

        self.api.request = change_after_upload
        with self.assertRaisesRegex(publisher.PublishError, 'main changed'):
            self.publish()
        self.assertTrue(self.api.release['draft'])
        self.assertEqual(len(self.api.assets), 3)
        self.assertNotIn(('PATCH', '/releases/1'), self.api.writes)

    def test_existing_release_without_verified_tag_refused(self):
        self.publish()
        self.api.ref = None
        self.api.writes.clear()
        with self.assertRaisesRegex(publisher.PublishError, 'no verified annotated tag'):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_only_manual_main_workflow_may_call_publisher(self):
        for env in ({}, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push',
                         'GITHUB_REF': 'refs/heads/main'},
                    {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                     'GITHUB_REF': 'refs/heads/dev'}):
            with self.subTest(env=env), patch.dict(publisher.os.environ, env, clear=True):
                with patch.object(publisher, 'GitHub') as api:
                    self.assertEqual(publisher.main([TAG, 'unused.tar.gz']), 1)
                    api.assert_not_called()

    def test_validation_failure_precedes_api_access(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': COMMIT}
        with (patch.dict(publisher.os.environ, env, clear=True),
              patch.object(publisher.subprocess, 'check_output', return_value=COMMIT + '\n'),
              patch.object(publisher.subprocess, 'run',
                           side_effect=publisher.subprocess.CalledProcessError(1, 'validate')),
              patch.object(publisher, 'GitHub') as api):
            self.assertEqual(publisher.main([TAG, 'unused.tar.gz']), 1)
            api.assert_not_called()
