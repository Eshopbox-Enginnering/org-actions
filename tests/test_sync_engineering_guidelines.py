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
