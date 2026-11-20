"""Run the real workflow shell against local Git histories."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StagingCommitGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-b", "main")
        self.git(self.origin, "config", "user.name", "Regression Test")
        self.git(self.origin, "config", "user.email", "test@example.invalid")
        self.common = self.commit("common")
        self.git(self.origin, "branch", "staging")
        self.git(self.origin, "checkout", "-b", "feature/picking")
        self.feature = self.commit("picking")
        self.git(self.origin, "checkout", "main")
        self.documentation = self.commit("documentation")
        self.git(self.origin, "checkout", "feature/picking")
        self.git(self.origin, "merge", "--no-ff", "main", "-m", "Merge main")

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, name):
        (self.origin / name).write_text(name)
        self.git(self.origin, "add", name)
        self.git(self.origin, "commit", "-m", name)
        return self.git(self.origin, "rev-parse", "HEAD")

    def run_gate(self, base="main", compare="staging"):
        checkout = self.root / "checkout"
        self.git(self.root, "clone", str(self.origin), str(checkout))
        workflow = (ROOT / ".github/workflows/check-commits-in-staging.yml").read_text()
        script = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
        script = script.replace("${{ github.event_name }}", "pull_request")
        output = self.root / "output"
        output.touch()
        result = subprocess.run(
            ["bash", "-c", script], cwd=checkout, text=True, capture_output=True,
            env={**os.environ, "BASE": base, "COMPARE": compare,
                 "SOURCE": "feature/picking", "REPO": "example/inventory",
                 "GITHUB_OUTPUT": str(output)},
        )
        return result, output.read_text()

    def stage_feature(self, branch="staging"):
        self.git(self.origin, "branch", "-f", branch, self.feature)

    def test_main_history_missing_from_staging_does_not_block_staged_pr(self):
        self.stage_feature()
        result, output = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "")

    def test_unstaged_pr_commit_still_fails_without_reporting_main_history(self):
        result, output = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(self.feature, output)
        self.assertNotIn(self.documentation, output)
        self.assertNotIn(self.git(self.origin, "rev-parse", "HEAD"), output)

    def test_only_missing_pr_commit_is_reported_when_partially_staged(self):
        self.stage_feature()
        missing = self.commit("another-picking-change")
        result, output = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(output, f"pr_commits<<EOF\n{missing}\nEOF\n")

    def test_custom_compare_branch_and_master_base(self):
        self.git(self.origin, "branch", "master", "main")
        self.stage_feature("qa")
        result, _ = self.run_gate(base="master", compare="qa")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_base_ref_fails_closed(self):
        self.stage_feature()
        result, output = self.run_gate(base="Master")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
