"""Verify differential coverage against real temporary Git repositories."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/ci/coverage_diff.py'
SPEC = importlib.util.spec_from_file_location('coverage_diff', SCRIPT)
diff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diff)


class CoverageDiffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git('init', '-q', '-b', 'base')
        self.git('config', 'user.name', 'Coverage Test')
        self.git('config', 'user.email', 'coverage@example.invalid')
        self.write('netbox_ops/sample.py', 'a = 1\nb = 2\n')
        self.base = self.commit()

    def git(self, *args):
        return diff.git(self.root, *args).decode().strip()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def coverage(self, name='netbox_ops/sample.py', covered=None, missed=None):
        return {'files': {name: {
            'executed_lines': [1] if covered is None else covered,
            'missing_lines': [2] if missed is None else missed,
        }}}

    def test_changed_covered_and_missed(self):
        self.write('netbox_ops/sample.py', 'a = 10\nb = 20\n')
        self.commit()
        result = diff.report(self.root, self.base, self.coverage())
        self.assertEqual(result['percent'], 50)
        self.assertEqual(result['files']['netbox_ops/sample.py']['missing'], [2])

    def test_comment_only_and_deleted_only_are_not_applicable(self):
        self.write('netbox_ops/sample.py', 'a = 1\nb = 2\n# comment\n')
        self.commit()
        self.assertIsNone(diff.report(self.root, self.base, self.coverage())['percent'])
        (self.root / 'netbox_ops/sample.py').unlink()
        self.commit()
        result = diff.report(self.root, self.base, {'files': {}})
        self.assertIsNone(result['percent'])
        self.assertTrue(result['review_required']['netbox_ops/sample.py']['deleted_file'])

    def test_missing_report_entry_fails_even_for_comment(self):
        self.write('netbox_ops/sample.py', 'a = 1\nb = 2\n# comment\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'Missing coverage'):
            diff.report(self.root, self.base, {'files': {}})

    def test_new_module_and_rename_are_counted_conservatively(self):
        self.git('mv', 'netbox_ops/sample.py', 'netbox_ops/renamed.py')
        self.write('netbox_ops/new.py', 'c = 3\n')
        self.commit()
        data = self.coverage('netbox_ops/renamed.py')
        data['files']['netbox_ops/new.py'] = {'executed_lines': [], 'missing_lines': [1]}
        result = diff.report(self.root, self.base, data)
        self.assertEqual((result['covered'], result['missing'], result['total']), (1, 2, 3))

    def test_unrelated_changes_are_not_applicable(self):
        self.write('README.md', '# documentation\n')
        self.commit()
        self.assertIsNone(diff.report(self.root, self.base, {'files': {}})['percent'])

    def test_invalid_base_and_dirty_source_fail(self):
        with self.assertRaises(ValueError):
            diff.report(self.root, 'HEAD', self.coverage())
        self.write('netbox_ops/sample.py', 'a = 9\n')
        with self.assertRaisesRegex(ValueError, 'not clean'):
            diff.report(self.root, self.base, self.coverage())

    def test_pr_base_matches_merge_parents(self):
        self.git('checkout', '-qb', 'feature')
        self.write('netbox_ops/sample.py', 'a = 10\nb = 2\n')
        head = self.commit()
        self.git('checkout', '-q', 'base')
        self.write('README.md', '# base update\n')
        base = self.commit()
        self.git('merge', '--no-ff', '--no-edit', 'feature')
        event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
        self.assertEqual(diff.pr_base(self.root, event), base)
        result = diff.report(self.root, base, self.coverage())
        self.assertEqual((result['covered'], result['total']), (1, 1))
        event['pull_request']['base']['sha'] = self.base
        with self.assertRaisesRegex(ValueError, 'does not match'):
            diff.pr_base(self.root, event)

    def test_non_ancestor_is_rejected(self):
        self.git('checkout', '-qb', 'other')
        self.write('README.md', 'other branch\n')
        other = self.commit()
        self.git('checkout', '-q', 'base')
        with self.assertRaises(subprocess.CalledProcessError):
            diff.report(self.root, other, self.coverage())

    def test_continuation_line_requires_review(self):
        self.write('netbox_ops/sample.py', 'a = (\n    1\n)\n')
        base = self.commit()
        self.write('netbox_ops/sample.py', 'a = (\n    2\n)\n')
        self.commit()
        result = diff.report(self.root, base, self.coverage(covered=[1], missed=[]))
        self.assertIsNone(result['percent'])
        self.assertEqual(result['review_required']['netbox_ops/sample.py'],
                         {'unmapped_source_lines': [2], 'deletion_only_hunks': 0})

    def test_partial_deletion_requires_review(self):
        self.write('netbox_ops/sample.py', 'a = (\n    1 +\n    2\n)\n')
        base = self.commit()
        self.write('netbox_ops/sample.py', 'a = (\n    2\n)\n')
        self.commit()
        result = diff.report(self.root, base, self.coverage(covered=[1], missed=[]))
        self.assertIsNone(result['percent'])
        self.assertEqual(result['review_required']['netbox_ops/sample.py']['deletion_only_hunks'], 1)

    @unittest.skipUnless(importlib.util.find_spec('coverage'), 'coverage.py required')
    def test_real_coverage_json_with_multiline_change(self):
        self.write('netbox_ops/__init__.py', '')
        self.write('netbox_ops/sample.py', 'a = 1\ndef missed():\n    return (\n        2\n    )\n')
        base = self.commit()
        self.write('netbox_ops/sample.py', 'a = 10\ndef missed():\n    return (\n        20\n    )\n')
        self.commit()
        datafile = str(self.root / '.fixture-coverage')
        reportfile = self.root / 'fixture.json'
        for args in (
            ['run', '--branch', '--source=netbox_ops', f'--data-file={datafile}',
             '-m', 'netbox_ops.sample'],
            ['json', f'--data-file={datafile}', '-o', str(reportfile)],
        ):
            subprocess.run([sys.executable, '-m', 'coverage', *args], cwd=self.root,
                           check=True, capture_output=True,
                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        report = json.loads(reportfile.read_text(encoding='utf-8'))
        result = diff.report(self.root, base, report)
        self.assertEqual(result['files']['netbox_ops/sample.py']['covered'], [1])
        self.assertEqual(result['review_required']['netbox_ops/sample.py']['unmapped_source_lines'], [4])
