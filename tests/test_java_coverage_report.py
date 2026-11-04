"""Execute the workflow's real report script against isolated runner outputs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/java-test-coverage.yml'


def report_script():
    step = WORKFLOW.read_text().split('      - name: Build coverage and test report\n', 1)[1]
    body = step.split("          python3 <<'PYTHON'\n", 1)[1]
    return textwrap.dedent(body.split('\n          PYTHON', 1)[0])


class CoverageReportTests(unittest.TestCase):
    def run_report(self, *, base_failed=False, base_status=0, base_tests=1,
                   base_covered=8, pr_covered=7):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for revision, covered in [('base', base_covered), ('pr', pr_covered)]:
                target = root / revision / 'target'
                jacoco = target / 'site/jacoco'
                jacoco.mkdir(parents=True)
                (jacoco / 'jacoco.xml').write_text(
                    f'<report><counter type="LINE" covered="{covered}" '
                    f'missed="{10-covered}"/></report>')
                surefire = target / 'surefire-reports'
                surefire.mkdir()
                failed = revision == 'base' and base_failed
                count = base_tests if revision == 'base' else 1
                case = ('<testcase classname="example.ContextTest" name="contextLoads">'
                        + ('<error message="setup failed"/>' if failed else '')
                        + '</testcase>') if count else ''
                (surefire / 'TEST-example.ContextTest.xml').write_text(
                    f'<testsuite tests="{count}" errors="{int(failed)}" failures="0">'
                    + case + '</testsuite>')
                status = base_status if revision == 'base' else 0
                (root / f'{revision}.status').write_text(str(status))
            (root / 'changed-files.txt').write_bytes(b'src/main/java/example/Example.java\0')
            result = subprocess.run(
                [sys.executable, '-c', report_script()], cwd=root,
                env={**os.environ, 'GITHUB_STEP_SUMMARY': str(root / 'summary.md')},
                capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return (json.loads((root / 'gate.json').read_text()),
                    (root / 'summary.md').read_text())

    def test_failed_base_blocks_comparison_instead_of_reporting_regression(self):
        gate, summary = self.run_report(base_failed=True)
        self.assertFalse(gate['passed'])
        self.assertTrue(any('base: Test failed:' in e for e in gate['errors']))
        self.assertNotIn('PR line coverage is lower than base line coverage.', gate['errors'])
        self.assertIn('Not compared', summary)

    def test_zero_tests_still_fails_and_blocks_comparison(self):
        gate, summary = self.run_report(base_tests=0)
        self.assertFalse(gate['passed'])
        self.assertIn('base: No test results found.', gate['errors'])
        self.assertNotIn('PR line coverage is lower than base line coverage.', gate['errors'])
        self.assertIn('Not compared', summary)

    def test_build_failure_blocks_comparison_even_with_test_xml(self):
        gate, summary = self.run_report(base_status=1)
        self.assertFalse(gate['passed'])
        self.assertTrue(any('base: Maven failed' in e for e in gate['errors']))
        self.assertNotIn('PR line coverage is lower than base line coverage.', gate['errors'])
        self.assertIn('Not compared', summary)

    def test_valid_coverage_decrease_still_fails(self):
        gate, summary = self.run_report()
        self.assertFalse(gate['passed'])
        self.assertEqual(['PR line coverage is lower than base line coverage.'], gate['errors'])
        self.assertIn('-10.0000 percentage points', summary)

    def test_valid_equal_or_increased_coverage_passes(self):
        for covered in (8, 9):
            with self.subTest(covered=covered):
                gate, _ = self.run_report(pr_covered=covered)
                self.assertTrue(gate['passed'])
                self.assertEqual([], gate['errors'])


if __name__ == '__main__':
    unittest.main()
