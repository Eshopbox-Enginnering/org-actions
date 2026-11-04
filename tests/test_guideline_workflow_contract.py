"""Regression checks for guideline rollout and lightweight org checks."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuidelineWorkflowContractTests(unittest.TestCase):
    def contents(self, name):
        return (ROOT / name).read_text()

    def test_sync_workflow_runs_dual_pr_creator(self):
        workflow = self.contents(".github/workflows/sync-engineering-guidelines.yml")
        self.assertIn("python3 scripts/sync-guidelines-dual-pr.py", workflow)
        self.assertNotIn("python3 scripts/sync-engineering-guidelines.py", workflow)

    def test_lightweight_checks_do_not_use_checks_runner(self):
        for name in (
            "pr-test-validation.yml",
            "pr-ticket-validation.yml",
            "check-sync-with-main.yml",
            "check-commits-in-staging.yml",
        ):
            with self.subTest(name=name):
                content = self.contents(".github/workflows/" + name)
                self.assertIn("runs-on: ubuntu-", content)
                self.assertNotIn("group: checks-runners", content.replace(
                    "#  group: checks-runners", ""))

    def test_java_coverage_reserves_checks_runner_for_heavy_job(self):
        workflow = self.contents(".github/workflows/java-test-coverage.yml")
        detect = workflow.split("\n  detect-java:", 1)[1].split("\n  not-applicable:", 1)[0]
        not_applicable = workflow.split("\n  not-applicable:", 1)[1].split("\n  coverage:", 1)[0]
        heavy = workflow.split("\n  coverage:", 1)[1]
        self.assertIn("runs-on: ubuntu-latest", detect)
        self.assertIn("runs-on: ubuntu-latest", not_applicable)
        self.assertIn("group: checks-runners", heavy)

    def test_commit_gate_requires_exact_staging_document_bytes(self):
        workflow = self.contents(".github/workflows/check-commits-in-staging.yml")
        self.assertIn("org-action/guidelines-prod-*", workflow)
        self.assertIn("git cat-file -e", workflow)
        self.assertIn('git diff --quiet "origin/$COMPARE" "origin/$SOURCE"', workflow)
        self.assertIn("enforcing normal commit check", workflow)

    def test_guidelines_have_org_compliant_pr_titles(self):
        source = self.contents("scripts/sync-engineering-guidelines.py")
        self.assertIn("f'feature to", source)
        self.assertNotIn("f'docs to", source)


if __name__ == "__main__":
    unittest.main()
