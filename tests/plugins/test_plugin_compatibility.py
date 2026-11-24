from __future__ import annotations

import io
import json
import subprocess
import unittest
from pathlib import Path

from netbox_ops.plugin_compatibility import (
    CompatibilityDeclaration, CompatibilityError, DependencyPreflight,
    _check_specifiers, assess_compatibility, assess_upgrade_stages,
    discover_stable_releases, fetch_pypi_json, isolated_dependency_preflight,
)


META = {'info': {'name': 'netbox-plugin-demo', 'version': '2.0.0',
                 'requires_python': '>=3.12,<3.15',
                 'requires_dist': ['netbox (>=4.5,<4.7)', 'requests>=2']}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class CompatibilityTests(unittest.TestCase):
    def assess(self, **overrides):
        args = dict(package='netbox-plugin-demo', version='2.0.0', netbox_version='4.5.1',
                    python_version='3.12.2', metadata=META)
        args.update(overrides)
        return assess_compatibility(**args)

    def test_comparison_supports_ranges_and_wildcards(self):
        self.assertTrue(_check_specifiers('>=4.5,<4.7', '4.6.2'))
        self.assertFalse(_check_specifiers('>=4.5,<4.7', '4.7.0'))
        self.assertTrue(_check_specifiers('~=4.5', '4.6.0'))
        self.assertFalse(_check_specifiers('~=4.5', '5.0.0'))
        self.assertFalse(_check_specifiers('!=4.5.*', '4.5.2'))
        self.assertTrue(_check_specifiers('==4.5.*', '4.5.2'))
        self.assertIsNone(_check_specifiers('>=4.5; extra == "dev"', '4.6.0'))
        self.assertIsNone(_check_specifiers('===abc', '4.6.0'))

    def test_pypi_latest_excludes_yanked_prereleases_and_empty(self):
        project = {'releases': {
            '2.0.0': [{'packagetype': 'bdist_wheel', 'requires_python': '>=3.12'}],
            '2.1.0rc1': [{'packagetype': 'bdist_wheel'}],
            '2.0.1': [{'packagetype': 'sdist', 'requires_python': '>=3.13'}],
            '3.0.0': [{'packagetype': 'sdist', 'yanked': True}],
            '9.0.0': [],
        }}
        versions = discover_stable_releases('netbox-plugin-demo', project, '3.12.3')
        self.assertEqual(versions.latest, '2.0.1')
        self.assertEqual([v.status for v in versions.stable], ['blocked', 'ok'])

    def test_pypi_url_fixed_and_bounded(self):
        called = []

        def opener(request, timeout):
            called.append((request.full_url, timeout))
            return FakeResponse(json.dumps(META).encode())

        self.assertEqual(fetch_pypi_json('netbox-plugin-demo', '2.0.0', opener=opener), META)
        self.assertEqual(called, [('https://pypi.org/pypi/netbox-plugin-demo/2.0.0/json', 8)])
        with self.assertRaises(CompatibilityError):
            fetch_pypi_json('https://evil.example/path', opener=opener)
        with self.assertRaises(CompatibilityError):
            fetch_pypi_json('plugin', '../2.0', opener=opener)
        with self.assertRaisesRegex(CompatibilityError, 'size limit'):
            fetch_pypi_json('plugin', opener=lambda *_args, **_kwargs: FakeResponse(b'x' * (3 * 1024 * 1024 + 1)))

    def test_metadata_direct_conflict_blocks(self):
        result = self.assess(netbox_version='4.7.0')
        self.assertEqual(result.status, 'blocked')
        self.assertFalse(result.can_continue)

    def test_requires_python_conflict_blocks(self):
        result = self.assess(python_version='3.15.0')
        self.assertEqual(result.status, 'blocked')

    def test_missing_metadata_is_uncertain(self):
        result = self.assess(metadata=None)
        self.assertEqual(result.status, 'uncertain')
        self.assertTrue(result.requires_strong_confirmation)
        self.assertTrue(result.can_continue)

    def test_missing_netbox_declaration_is_uncertain(self):
        metadata = {'info': {**META['info'], 'requires_dist': ['requests>=2']}}
        self.assertEqual(self.assess(metadata=metadata).status, 'uncertain')

    def test_catalog_mismatch_is_warning_not_block(self):
        catalog = CompatibilityDeclaration(source='https://netboxlabs.com/plugins/demo',
                                           min_netbox='4.1.0', max_netbox='4.4.99', advisory=True)
        result = self.assess(catalog=catalog)
        self.assertEqual(result.status, 'uncertain')
        self.assertTrue(any(record.status == 'warns' for record in result.evidence))

    def test_release_docs_mismatch_is_not_hard_block(self):
        docs = CompatibilityDeclaration(source='https://github.com/author/plugin/blob/v2.0.0/COMPATIBILITY.md',
                                        min_netbox='4.0.0', max_netbox='4.4.99')
        result = self.assess(declaration=docs)
        self.assertNotEqual(result.status, 'blocked')

    def test_verified_pluginconfig_runtime_gate_blocks(self):
        gate = CompatibilityDeclaration(source='https://github.com/author/plugin/blob/v2.0.0/config.py',
                                        max_netbox='4.4.99', enforced=True)
        self.assertEqual(self.assess(declaration=gate).status, 'blocked')

    def test_conditional_requirement_cannot_block(self):
        metadata = {'info': {**META['info'], 'requires_dist': ['netbox (<4.0); extra == "foo"']}}
        result = self.assess(metadata=metadata)
        self.assertEqual(result.status, 'uncertain')

    def test_verified_preflight_and_metadata_can_confirm(self):
        full = DependencyPreflight('passed', 'All core pins resolved', checked_core=True)
        result = self.assess(preflight=full, alternatives=('1.9.0', '2.0.0', '2.1.0rc1'))
        self.assertEqual(result.status, 'compatible')
        self.assertFalse(result.requires_strong_confirmation)
        self.assertEqual(result.alternatives, ('1.9.0',))

    def test_preflight_without_core_cannot_confirm(self):
        partial = DependencyPreflight('passed', 'Only plugin pins resolved', checked_core=False)
        self.assertEqual(self.assess(preflight=partial).status, 'uncertain')

    def test_resolver_conflict_blocks_when_verified(self):
        result = self.assess(preflight=DependencyPreflight('blocked', 'Dependency conflict', checked_core=True))
        self.assertEqual(result.status, 'blocked')

    def test_malformed_identity_never_blocks(self):
        metadata = {'info': {**META['info'], 'version': '2.0.1', 'requires_python': '<3.0'}}
        result = self.assess(metadata=metadata)
        self.assertEqual(result.status, 'uncertain')

    def test_all_intermediate_netbox_steps_rechecked(self):
        stages = assess_upgrade_stages('netbox-plugin-demo', '2.0.0', ['4.5.0', '4.6.0', '4.7.0'],
                                       ['3.12.1'] * 3, META)
        self.assertEqual([s.status for s in stages], ['uncertain', 'uncertain', 'blocked'])
        with self.assertRaises(CompatibilityError):
            assess_upgrade_stages('netbox-plugin-demo', '2.0.0', ['4.5.0'], [], META)

    def test_pip_isolation_no_install_and_no_builds(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            report = Path(command[command.index('--report') + 1])
            report.write_text(json.dumps({'install': []}))
            return subprocess.CompletedProcess(command, 0, '', '')

        result = isolated_dependency_preflight(['netbox-plugin-demo==2.0.0'],
                                               ['django==5.2.1', 'psycopg[c,pool]==3.2.10'], '3.12.3', runner=run)
        self.assertEqual(result.status, 'passed')
        command, kwargs = calls[0]
        self.assertIn('psycopg[c,pool]==3.2.10', command)
        self.assertEqual(command[command.index('--python-version') + 1], '3.12.3')
        self.assertIn('--dry-run', command)
        self.assertIn('--ignore-installed', command)
        self.assertIn('--only-binary=:all:', command)
        self.assertIn('--isolated', command)
        self.assertEqual(kwargs['timeout'], 120)
        self.assertNotIn('PIP_INDEX_URL', kwargs['env'])

    def test_resolver_without_core_is_unknown(self):
        def run(command, **kwargs):
            Path(command[command.index('--report') + 1]).write_text('{"install": []}')
            return subprocess.CompletedProcess(command, 0, '', '')
        result = isolated_dependency_preflight(['netbox-plugin-demo==2.0.0'], [], '3.12', runner=run)
        self.assertEqual(result.status, 'unknown')

    def test_resolver_nonconflict_failure_is_unknown(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, '', 'No matching distribution')
        result = isolated_dependency_preflight(['netbox-plugin-demo==2.0.0'], ['django==5.2'],
                                               '3.12', runner=runner)
        self.assertEqual(result.status, 'unknown')

    def test_resolver_proven_conflict_blocks(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, '', 'ERROR: ResolutionImpossible')
        result = isolated_dependency_preflight(['netbox-plugin-demo==2.0.0'], ['django==5.2'],
                                               '3.12', runner=runner)
        self.assertEqual(result.status, 'blocked')

    def test_unsafe_package_arguments_rejected(self):
        with self.assertRaises(CompatibilityError):
            isolated_dependency_preflight(['--extra-index-url=https://evil.test'], ['django==5.2'], '3.12')
        with self.assertRaises(CompatibilityError):
            isolated_dependency_preflight(['demo==1.0\n--allow-external'], ['django==5.2'], '3.12')

    def test_preflight_timeout_is_unknown(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired('pip', 120)
        result = isolated_dependency_preflight(['netbox-plugin-demo==2.0.0'], ['django==5.2'],
                                               '3.12', runner=run)
        self.assertEqual(result.status, 'unknown')


if __name__ == '__main__':
    unittest.main()


class OrchestrationTests(unittest.TestCase):
    @staticmethod
    def metadata():
        return {'info': {'name': 'netbox-plugin-demo', 'version': '2.0.0',
                         'requires_python': '>=3.12,<3.15',
                         'requires_dist': ['netbox (>=4.5,<4.7)'],
                         'project_urls': {'Source': 'https://github.com/example/netbox-plugin-demo'}}}

    def test_parse_release_document_with_wildcard(self):
        from netbox_ops.plugin_compatibility import parse_compatibility_document
        markdown = '''| Plugin version | Minimum NetBox | Maximum NetBox |
| --- | --- | --- |
| 1.9.0 | 4.3.0 | 4.4.x |
| 2.0.0 | 4.5.0 | 4.6.x |
'''
        declaration = parse_compatibility_document(markdown, '2.0.0', 'https://github.com/example/repo')
        self.assertEqual(declaration.min_netbox, '4.5.0')
        self.assertEqual(declaration.max_netbox, '4.6.x')
        assessment = assess_compatibility('netbox-plugin-demo', '2.0.0', '4.7.0', '3.12',
                                           self.metadata(), declaration=declaration)
        self.assertEqual(assessment.status, 'blocked')  # PyPI NetBox constraint
        self.assertTrue(any(e.status == 'warns' for e in assessment.evidence))

    def test_rejects_ambiguous_document_rows(self):
        from netbox_ops.plugin_compatibility import parse_compatibility_document
        doc = '| Plugin version | Min NetBox | Max NetBox |\n|2.0.0|4.5.0|4.6.0|\n|2.0.0|4.5.0|4.6.0|'
        self.assertIsNone(parse_compatibility_document(doc, '2.0.0', 'https://example.com'))

    def test_release_docs_pinned_to_exact_git_tag(self):
        from netbox_ops.plugin_compatibility import fetch_release_declaration
        requested = []

        def opener(request, timeout):
            requested.append(request.full_url)
            return FakeResponse(b'| Plugin version | Min NetBox | Max NetBox |\n|2.0.0|4.5.0|4.6.x|')

        declaration = fetch_release_declaration(self.metadata(), '2.0.0', opener=opener)
        self.assertEqual(declaration.max_netbox, '4.6.x')
        self.assertEqual(requested, ['https://raw.githubusercontent.com/example/netbox-plugin-demo/v2.0.0/COMPATIBILITY.md'])

    def test_no_untrusted_project_url_fetch(self):
        from netbox_ops.plugin_compatibility import fetch_release_declaration
        metadata = self.metadata()
        metadata['info']['project_urls']['Source'] = 'https://github.com.attacker.example/example/repo'
        fetches = []
        def opener(*args, **kwargs):
            fetches.append(args)
            return FakeResponse(b'')
        self.assertIsNone(fetch_release_declaration(metadata, '2.0.0', opener=opener))
        self.assertEqual(fetches, [])

    def test_review_latest_and_every_stage(self):
        from netbox_ops.plugin_compatibility import review_plugin_upgrade
        calls = []
        project = {'releases': {
            '2.0.0': [{'packagetype': 'bdist_wheel', 'requires_python': '>=3.12'}],
            '1.9.0': [{'packagetype': 'bdist_wheel', 'requires_python': '>=3.10'}],
            '2.1.0rc1': [{'packagetype': 'bdist_wheel'}],
        }}
        def fetcher(*args):
            calls.append(args)
            return project if len(args) == 1 else self.metadata()
        review = review_plugin_upgrade('netbox-plugin-demo', ['4.5.0', '4.7.0'],
                                       ['3.12', '3.12'], fetcher=fetcher,
                                       docs_fetcher=lambda *_: None)
        self.assertEqual(review.selected_version, '2.0.0')
        self.assertEqual([s.status for s in review.stages], ['uncertain', 'blocked'])
        self.assertEqual(review.stages[0].alternatives, ('1.9.0',))
        self.assertEqual(calls, [('netbox-plugin-demo',), ('netbox-plugin-demo', '2.0.0')])

    def test_pypi_error_never_proves_incompatibility(self):
        from urllib.error import URLError
        from netbox_ops.plugin_compatibility import review_plugin_upgrade
        def fetcher(*_):
            raise URLError('Connection refused')
        review = review_plugin_upgrade('netbox-plugin-demo', ['4.5.0'], ['3.12.1'],
                                       version='2.0.0', fetcher=fetcher)
        self.assertEqual(review.stages[0].status, 'uncertain')
        self.assertTrue(review.warnings)

    def test_file_level_python_conflict_proven(self):
        result = assess_compatibility('netbox-plugin-demo', '2.0.0', '4.5.0', '3.12.0',
                                      self.metadata(), published_files=[{'requires_python': '>=3.13'}])
        self.assertEqual(result.status, 'blocked')
