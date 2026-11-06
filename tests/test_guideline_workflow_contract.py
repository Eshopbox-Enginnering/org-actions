"""Regression checks for guideline rollout and lightweight org checks."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuidelineWorkflowContractTests(unittest.TestCase):
    def contents(self, name):
        return (ROOT / name).read_text()

    def test_sync_workflow_runs_dual_pr_creator(self):
        workflow = self.contents(".github/workflows/sync-engineering-guidelines.yml")
        self.assertIn("python3 scripts/sync-guidelines-with-agents.py", workflow)
        entrypoint = self.contents("scripts/sync-guidelines-with-agents.py")
        self.assertIn("scripts/sync-guidelines-auto-merge.py", entrypoint)
        self.assertIn("scripts/update-agents.sh", entrypoint)
        self.assertNotIn("python3 scripts/sync-guidelines-dual-pr.py", workflow)

    def test_combined_apply_verifies_both_file_families(self):
        source = self.contents("scripts/sync-guidelines-and-agents.py")
        merge = self.contents("scripts/sync-guidelines-auto-merge.py")
        self.assertIn("expected_agents(root)", source)
        self.assertIn("sync.diff_plan(root, entries, contents)", source)
        self.assertIn('changed.add("AGENTS.md")', source)
        self.assertIn("staged != changed", source)
        self.assertIn("agents_expected=agents_expected", source)
        self.assertIn('expected.add("AGENTS.md")', merge)
        self.assertIn('noncanonical AGENTS.md', merge)
        self.assertIn('require_regular_guideline_file(root, merge_sha, "AGENTS.md")', merge)
        self.assertIn("verified_base", merge)
        self.assertIn("--match-head-commit", merge)

    def test_combined_rollout_reuses_validated_legacy_migrator(self):
        source = self.contents("scripts/sync-guidelines-and-agents.py")
        self.assertIn("apply_agents_content() {", source)
        self.assertIn("TRUSTED_LEGACY", source)
        self.assertIn('result.returncode not in (0, 2)', source)

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

    def test_ruleset_workflows_retain_pull_request_triggers(self):
        for name in (
            "check-commits-in-staging.yml",
            "pr-test-validation.yml",
            "pr-ticket-validation.yml",
            "check-sync-with-main.yml",
            "java-test-coverage.yml",
        ):
            with self.subTest(name=name):
                content = self.contents(".github/workflows/" + name)
                trigger = content.split("\non:\n", 1)[1].split("\njobs:", 1)[0]
                self.assertIn("  pull_request:", trigger)
                self.assertNotIn("  workflow_dispatch:", trigger)

    def test_guideline_exemption_requires_current_production_base(self):
        source = self.contents(".github/workflows/check-commits-in-staging.yml")
        self.assertIn('git merge-base --is-ancestor "origin/$BASE" "origin/$SOURCE"', source)
        self.assertIn('docs_only=false', source)
        self.assertIn('git diff --quiet "origin/$COMPARE" "origin/$SOURCE"', source)

    def test_supported_branch_names_match_pr_title_validation(self):
        workflow = self.contents(".github/workflows/pr-ticket-validation.yml")
        script = self.contents("scripts/sync-engineering-guidelines.py")
        for name in ("staging", "main", "master",
                     "aws-velocis-main", "aws-velocis-staging"):
            with self.subTest(name=name):
                self.assertIn(name, workflow)
        self.assertIn("f'feature to {target_base}:", script)

    def test_runbook_describes_immediate_dual_pr_creation(self):
        readme = self.contents("engineering-guidelines/README.md")
        self.assertIn("One manual APPLY run creates and merges the staging and production PRs", readme)
        self.assertNotIn("scheduled promotion workflow then creates", readme)
        self.assertIn("staging first, then production", readme)

    def test_main_pr_never_uses_staging_derived_head(self):
        creator = self.contents("scripts/sync-guidelines-and-agents.py")
        merger = self.contents("scripts/sync-guidelines-auto-merge.py")
        self.assertIn('phase(repo, args, selected, contents, production)', creator)
        self.assertIn('len(parents) != 2 or parents[1] != verified_base', merger)
        self.assertIn('never merge staging into main', merger)
        self.assertIn('rollout branch must be one commit', merger)

    def test_automatic_merges_pin_the_inspected_head(self):
        source = self.contents("scripts/sync-guidelines-auto-merge.py")
        self.assertIn('"--match-head-commit", verified_head', source)
        self.assertIn('"--merge", "--admin", "--match-head-commit"', source)
        self.assertIn('require_production=single', source)

    def test_repeat_production_rollouts_use_current_main_base(self):
        source = self.contents("scripts/sync-engineering-guidelines.py")
        self.assertIn("-synced-{base_sha}", source)
        self.assertIn("synced(?:-[a-f0-9]{{10}})?", source)

    def test_promotion_and_sync_share_exclusive_lock(self):
        for path in (".github/workflows/sync-engineering-guidelines.yml",
                     ".github/workflows/promote-engineering-guidelines.yml"):
            self.assertIn("engineering-guidelines-rollout",
                          self.contents(path))
            self.assertNotIn("queue: max", self.contents(path))

    def test_auto_merge_rejects_nonregular_markdown_and_stale_pr_heads(self):
        auto_source = self.contents("scripts/sync-guidelines-auto-merge.py")
        sync_source = self.contents("scripts/sync-engineering-guidelines.py")
        self.assertIn('require_regular_guideline_file(root, f"origin/{head}", dest)', auto_source)
        self.assertIn("require_regular_guideline_file(root, merge_sha, destination)", auto_source)
        self.assertIn("git', 'merge-base', '--is-ancestor'", sync_source)
        self.assertIn("creating a new base-scoped production PR", sync_source)

    def test_guidelines_have_org_compliant_pr_titles(self):
        source = self.contents("scripts/sync-engineering-guidelines.py")
        self.assertIn("f'feature to", source)
        self.assertNotIn("f'docs to", source)


if __name__ == "__main__":
    unittest.main()
