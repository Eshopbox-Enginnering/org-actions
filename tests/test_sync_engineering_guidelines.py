import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guideline_sync', ROOT / 'scripts/sync-engineering-guidelines.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class EngineeringGuidelineSyncTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / 'engineering-guidelines/catalog.json').read_text())

    def test_five_backend_guidelines_and_frontend_catalog(self):
        self.assertEqual(set(x['id'] for x in self.catalog['backend']), {'401', 'cache', 'logging', '429', 'status'})
        self.assertEqual(self.catalog['frontend'], [])
        for entry in self.catalog['backend']:
            sync.validate_source(entry['source_url'])
            sync.validate_destination(entry['destination'])

    def test_one_off_frontend_guideline(self):
        result = sync.select_files(
            self.catalog, 'all', 'frontend',
            'https://github.com/user-attachments/files/1234/frontend.md',
            'docs/engineering-guidelines/frontend.md')
        self.assertEqual(list(result), ['frontend'])
        self.assertEqual(len(result['frontend']), 1)

    def test_one_off_requires_source_and_destination(self):
        with self.assertRaises(ValueError):
            sync.select_files(self.catalog, 'all', 'backend',
                              'https://github.com/user-attachments/files/1234/a.md', '')

    def test_reject_unsafe_destination(self):
        for path in ('AGENTS.md', '../foo.md', 'docs/engineering-guidelines/../../AGENTS.md',
                     'docs/engineering-guidelines/nested/a.md'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sync.validate_destination(path)

    def test_reject_untrusted_sources(self):
        for source in ('http://github.com/user-attachments/files/123/a.md',
                       'https://example.com/user-attachments/files/123/a.md',
                       'https://github.com/user-attachments/files/123/a.txt',
                       'https://github.com/user-attachments/files/123/a.md?query=true'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                sync.validate_source(source)

    def test_create_replace_skip_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = 'engineering-guidelines/guide.md'
            entry = [{'source_url': source, 'destination': 'docs/engineering-guidelines/guide.md'}]
            content = {source: b'new\n'}
            self.assertEqual(sync.diff_plan(root, entry, content)[0][1], 'CREATE')
            target = root / entry[0]['destination']
            target.parent.mkdir(parents=True)
            target.write_bytes(b'old\n')
            self.assertEqual(sync.diff_plan(root, entry, content)[0][1], 'REPLACE')
            target.write_bytes(b'new\n')
            self.assertEqual(sync.diff_plan(root, entry, content)[0][1], 'SKIP')

    def test_node_backend_wins_over_react_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text('{"dependencies":{"express":"1","react":"2"}}')
            self.assertEqual(sync.detect_type(root), 'backend')
            (root / 'package.json').write_text('{"dependencies":{"react-dom":"2"}}')
            self.assertEqual(sync.detect_type(root), 'frontend')
            (root / 'package.json').write_text('{"dependencies":{"nestjs":"2"}}')
            self.assertEqual(sync.detect_type(root), 'backend')

    def test_preview_shows_line_ending_and_final_newline_changes(self):
        for before, after in ((b'abc\\r\\n', b'abc\\n'), (b'abc', b'abc\\n')):
            with self.subTest(before=before):
                diff = ''.join(sync.preview(before, after, 'guide.md'))
                self.assertTrue(diff)

    def test_fetch_remote_branch_uses_explicit_tracking_refspec(self):
        from unittest.mock import patch
        with patch.object(sync, 'cmd') as mock_cmd:
            sync.fetch_remote_branch(Path('/tmp/repo'), 'staging')
        self.assertEqual(
            mock_cmd.call_args.args,
            ('git', 'fetch', '--depth=100', 'origin',
             '+refs/heads/staging:refs/remotes/origin/staging'))

    def test_fetch_remote_branch_rejects_invalid_ref(self):
        with self.assertRaises(ValueError):
            sync.fetch_remote_branch(Path('/tmp/repo'), '../staging')

    def test_exact_promotion_fetches_pr_by_number_without_list_limit(self):
        from unittest.mock import patch
        pr = {'number': 145, 'headRefName': 'org-action/guidelines-backend-abcd12345678-a1',
              'baseRefName': 'staging', 'mergedAt': '2026-11-04T00:00:00Z'}
        with patch.object(sync, 'gh_json', return_value=pr) as api:
            result = sync.find_merged_staging_candidates(
                'Eshopbox-Enginnering/inventory-engine', 'staging',
                'org-action/guidelines-backend-abcd12345678-', 145)
        self.assertEqual(result, [pr])
        self.assertEqual(api.call_args.args[:3], ('pr', 'view', '145'))

    def test_exact_promotion_rejects_wrong_base_or_open_pr(self):
        from unittest.mock import patch
        for base, merged in (('main', '2026-11-04T00:00:00Z'),
                             ('staging', None)):
            with self.subTest(base=base, merged=merged):
                pr = {'headRefName': 'org-action/guidelines-backend-abcd12345678-a1',
                      'baseRefName': base, 'mergedAt': merged}
                with patch.object(sync, 'gh_json', return_value=pr):
                    self.assertEqual(sync.find_merged_staging_candidates(
                        'Eshopbox-Enginnering/inventory-engine', 'staging',
                        'org-action/guidelines-backend-abcd12345678-', 145), [])

    def test_reject_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs').mkdir()
            (root / 'docs/engineering-guidelines').symlink_to(root)
            entry = [{'source_url':'engineering-guidelines/guide.md',
                      'destination':'docs/engineering-guidelines/guide.md'}]
            with self.assertRaises(ValueError):
                sync.diff_plan(root, entry, {'engineering-guidelines/guide.md':b'new'})


if __name__ == '__main__':
    unittest.main()
