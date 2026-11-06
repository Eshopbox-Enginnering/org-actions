"""Regression coverage for safe legacy AGENTS.md block migration."""
import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/update-agents.sh").read_text()
MIGRATOR = SCRIPT.split("apply_agents_content() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
TRUSTED = (ROOT / "payload/agents-legacy-v1.md").read_bytes()
CURRENT = (ROOT / "payload/agents-update.md").read_bytes()
LEGACY_BEGIN = b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE -->\n"
LEGACY_END = b"<!-- END ESHOPBOX-AGENTS-UPDATE -->\n"


class LegacyAgentsMigrationTests(unittest.TestCase):
    def run_migrator(self, directory, mode="apply"):
        payload = ROOT / "payload/agents-update.md"
        old = ROOT / "payload/agents-legacy-v1.md"
        digest = hashlib.sha256(CURRENT).hexdigest()[:12]
        return subprocess.run(
            [sys.executable, "-", str(payload), digest, mode, str(old)],
            input=MIGRATOR, text=True, capture_output=True, cwd=directory,
            check=False,
        )

    def make_repo(self, directory, body=TRUSTED, before=b"", after=b""):
        data = before + LEGACY_BEGIN + body + LEGACY_END + after
        (directory / "AGENTS.md").write_bytes(data)
        return data

    def test_trusted_legacy_stays_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = self.make_repo(root, before=b"# Repo rules\n\n")
            result = self.run_migrator(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            updated = (root / "AGENTS.md").read_bytes()
            self.assertTrue(updated.startswith(original))
            self.assertEqual(updated.count(b"## Git\n"), 1)
            self.assertIn(b"## AI context and token efficiency", updated)
            self.assertEqual(self.run_migrator(root).returncode, 2)

    def test_ai_only_managed_block_gets_missing_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            start = CURRENT.index(b"## AI context and token efficiency")
            end = CURRENT.index(b"## Engineering standards", start)
            ai = CURRENT[start:end].strip(b"\n") + b"\n"
            digest = hashlib.sha256(ai).hexdigest()[:12].encode("ascii")
            original = (b"# Existing custom rules\n\n"
                        + b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->\n"
                        + ai
                        + b"<!-- END ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->\n")
            (root / "AGENTS.md").write_bytes(original)
            result = self.run_migrator(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            updated = (root / "AGENTS.md").read_bytes()
            self.assertTrue(updated.startswith(original))
            for section in (b"## Git", b"## Starting a task",
                            b"## Reading the issue", b"## Review comments",
                            b"## Engineering standards", b"## Learned rules"):
                self.assertIn(section, updated)
            self.assertEqual(updated.count(b"## AI context and token efficiency"), 1)
            self.assertEqual(self.run_migrator(root).returncode, 2)

    def test_modified_legacy_content_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            original = self.make_repo(root, body=TRUSTED.replace(b"## Git", b"## Altered Git", 1))
            outcome = self.run_migrator(root)
            self.assertEqual(outcome.returncode, 1, outcome.stdout + outcome.stderr)
            self.assertIn("differs from trusted historical payload", outcome.stdout)
            self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_legacy_spacing_variant_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            padded = TRUSTED.replace(b"## Git\n", b"## Git\n\n", 1)
            original = self.make_repo(root, body=padded)
            self.assertEqual(self.run_migrator(root, "check").returncode, 0)
            self.assertEqual(self.run_migrator(root).returncode, 0)
            updated = (root / "AGENTS.md").read_bytes()
            self.assertTrue(updated.startswith(original))
            self.assertIn(b"## AI context and token efficiency", updated)
            self.assertEqual(self.run_migrator(root).returncode, 2)

    def test_legacy_nonblank_changes_remain_blocked_even_with_blank_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            original = self.make_repo(
                root, body=TRUSTED.replace(b"## Git\n", b"## Git\n\n", 1)
                .replace(b"Never merge or rebase", b"Always merge or rebase", 1))
            for mode in ("check", "apply"):
                result = self.run_migrator(root, mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("differs from trusted historical payload", result.stdout)
                self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_legacy_line_spacing_changes_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            original = self.make_repo(root, body=TRUSTED.replace(
                b"## Git\n", b"## Git\n  \n", 1))
            result = self.run_migrator(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_mismatched_and_duplicated_markers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            originals = [
                LEGACY_BEGIN + TRUSTED + b"<!-- END ESHOPBOX-AGENTS-UPDATE:123456789abc -->\n",
                LEGACY_BEGIN + TRUSTED + LEGACY_END + LEGACY_BEGIN + TRUSTED + LEGACY_END,
            ]
            for original in originals:
                (root / "AGENTS.md").write_bytes(original)
                outcome = self.run_migrator(root)
                self.assertEqual(outcome.returncode, 1, outcome.stdout + outcome.stderr)
                self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_matching_versioned_markers_are_accepted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            digest = hashlib.sha256(CURRENT).hexdigest()[:12].encode("ascii")
            begin = b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->\n"
            end = b"<!-- END ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->\n"
            existing = begin + CURRENT + end
            (root / "AGENTS.md").write_bytes(existing)
            result = self.run_migrator(root)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(existing, (root / "AGENTS.md").read_bytes())

    def test_unmatched_versioned_marker_hashes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            begin = b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:aaaaaaaaaaaa -->\n"
            end = b"<!-- END ESHOPBOX-AGENTS-UPDATE:bbbbbbbbbbbb -->\n"
            existing = begin + TRUSTED + end
            (root / "AGENTS.md").write_bytes(existing)
            result = self.run_migrator(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Mismatched organization-managed marker hashes", result.stdout)
            self.assertEqual(existing, (root / "AGENTS.md").read_bytes())

    def test_single_target_uses_staging_before_production(self):
        # Single and bulk must use one staging -> main implementation.
        self.assertNotIn('Manual review/merge required.', SCRIPT)
        self.assertNotIn('SUCCESS: Single repository PR created.', SCRIPT)
        self.assertIn('staging_branch=$(find_staging_branch "$production_branch")', SCRIPT)
        self.assertNotIn('if [ "$TARGET" != "single" ]; then', SCRIPT)

        existing = SCRIPT.split('# EXISTING ROLLOUT BRANCH', 1)[1].split(
            '# NEW ROLLOUT', 1)[0]
        self.assertLess(existing.index('Existing staging PR'),
                        existing.index('Production PR'))
        self.assertIn('production_pr=$(find_open_pr_by_base', existing)
        self.assertIn('gh pr merge "$production_pr"', existing)

        new = SCRIPT.split('# NEW ROLLOUT', 1)[1].split(
            '\n# Header', 1)[0]
        self.assertLess(new.index('Creating staging PR'),
                        new.index('Creating production PR'))
        self.assertIn('gh pr merge "$staging_pr"', new)
        self.assertIn('gh pr merge "$production_pr"', new)

    def test_single_target_reports_two_merges_and_keeps_dry_run_safe(self):
        single = SCRIPT.rsplit('# Single', 1)[1].split('# Bulk', 1)[0]
        self.assertIn('process_repo "$TARGET_REPO"', single)
        self.assertIn('10)', single)
        self.assertIn('12)', single)
        self.assertIn('staging_merged=$((staging_merged + 1))', single)
        self.assertIn('production_merged=$((production_merged + 1))', single)
        self.assertNotIn('single_pr_created', SCRIPT)
        self.assertIn('NO merge performed.', SCRIPT)
        self.assertIn('is_supported_production_branch "$production_branch"', SCRIPT)

    def test_existing_open_main_pr_is_reused_after_staging_merge(self):
        existing = SCRIPT.split('# EXISTING ROLLOUT BRANCH', 1)[1].split(
            '# NEW ROLLOUT', 1)[0]
        self.assertLess(existing.index('gh pr merge "$staging_pr"'),
                        existing.index('production_pr=$(find_open_pr_by_base'))
        self.assertLess(existing.index('production_pr=$(find_open_pr_by_base'),
                        existing.index('gh pr merge "$production_pr"'))
        self.assertIn('if [ -z "$production_pr" ]; then', existing)
        self.assertIn('gh pr create', existing)

    def test_retries_skip_completed_staging_and_pin_original_commit(self):
        existing = SCRIPT.split('# EXISTING ROLLOUT BRANCH', 1)[1].split(
            '# NEW ROLLOUT', 1)[0]
        self.assertIn('git merge-base --is-ancestor "$verified_sha"', existing)
        self.assertIn('already merged into staging; continuing to main', existing)
        self.assertIn('--match-head-commit "$verified_sha"', existing)
        new = SCRIPT.split('# NEW ROLLOUT', 1)[1].split(
            '# Header', 1)[0]
        self.assertIn('--match-head-commit "$working_commit"', new)
        self.assertIn('git checkout -b "$branch"', new)

    def test_single_apply_reports_skipped_target_as_failure(self):
        single = SCRIPT.rsplit('# Single', 1)[1].split('# Bulk', 1)[0]
        self.assertIn('if [ "$OPERATION" = "APPLY" ]; then', single)
        self.assertIn('failed=$((failed + 1))', single)

    def test_mixed_frontend_php_backend_uses_backend_precedence(self):
        php_check = SCRIPT.index('if [ -f "$dir/composer.json" ]; then')
        frontend_check = SCRIPT.index('if [ -f "$dir/angular.json" ]')
        self.assertLess(php_check, frontend_check)


if __name__ == "__main__":
    unittest.main()
