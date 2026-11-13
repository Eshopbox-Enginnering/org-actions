"""Regression tests for sequential, non-bypassing backend rollout (#7319)."""
import importlib.util
from pathlib import Path
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "rollout_staging", ROOT / "scripts/rollout-staging-promotion.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class RolloutSafetyTests(unittest.TestCase):
    def test_selected_repository_validation(self):
        self.assertEqual(
            mod.named_repositories("selected", "", "repo-one,repo-two", "org"),
            ["repo-one", "repo-two"])
        for value in ("", "repo-one,repo-one", "owner/repo", "../repo"):
            with self.assertRaises(ValueError):
                mod.named_repositories("selected", "", value, "org")

    def test_backend_filter_fail_closed(self):
        with mock.patch.object(mod, "gh_api", return_value=[{"name": "pom.xml"}]):
            self.assertTrue(mod.detect_backend("org", "java-api"))
        with mock.patch.object(mod, "gh_api", return_value=[
            {"name": "angular.json"}, {"name": "package.json"}]):
            self.assertFalse(mod.detect_backend("org", "frontend"))
        with mock.patch.object(mod, "gh_api", return_value=[{"name": "README.md"}]):
            self.assertFalse(mod.detect_backend("org", "unknown"))

    def test_payload_is_single_atomic_rollout(self):
        self.assertEqual(set(mod.SOURCE_FILES.values()), {
            ".github/scripts/auto-staging-deploy.cjs",
            ".github/scripts/auto-staging-pr.cjs",
            ".github/scripts/auto-staging-deploy.test.cjs",
            ".github/scripts/auto-staging-pr.test.cjs",
            ".github/workflows/auto-staging-deploy.yml",
        })
        self.assertFalse(hasattr(mod, "MANIFEST"))

    def test_confirmation_is_required_before_any_changes(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "", "ORG": "org",
               "TARGET": "single", "REPOSITORY": "api"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "cmd") as cmd:
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    mod.main()
                cmd.assert_not_called()

    def test_single_feature_commit_is_proposed_staging_first(self):
        import inspect
        source = inspect.getsource(mod.install)
        self.assertIn('for target in ("staging", "main"):', source)
        self.assertIn('merge_regular_pr(full, number, target, feature_sha,', source)
        self.assertNotIn("install_phase", source)
        self.assertIn("main PR exists before staging was merged", source)
        self.assertNotIn("--admin", source)

    def test_main_is_not_opened_after_failed_staging(self):
        import inspect
        source = inspect.getsource(mod.install)
        loop = source.index('for target in ("staging", "main"):')
        merge = source.index("merge_regular_pr(full, number, target, feature_sha,", loop)
        self.assertLess(loop, merge)
        # The merge throws if blocked, so the next iteration cannot run.
        self.assertNotIn("except", source[loop:merge])

    def test_diverged_main_staging_rejects_unrelated_staging_diff(self):
        import inspect
        source = inspect.getsource(mod.install)
        self.assertIn('origin/staging...HEAD', source)
        self.assertIn('refusing polluted staging PR', source)
        self.assertIn('pending_main - set(SOURCE_FILES.values())', source)
        self.assertLess(source.index('refusing polluted staging PR'),
                        source.index('cmd("git", "push", "-u", "origin", branch'))

    def test_full_history_is_fetched_before_staging_diff(self):
        import inspect
        source = inspect.getsource(mod.install)
        self.assertIn('cmd("git", "fetch", "--unshallow", "origin", cwd=root)', source)
        self.assertIn('cmd("git", "merge-base", "origin/staging", "HEAD"', source)
        self.assertIn("no verified merge base", source)
        self.assertLess(source.index('cmd("git", "fetch", "--unshallow"'),
                        source.index('"origin/staging...HEAD"'))
        self.assertLess(source.index("no verified merge base"),
                        source.index('"origin/staging...HEAD"'))

    def test_closed_pr_must_not_be_reopened(self):
        with self.assertRaisesRegex(RuntimeError, "closed without merge"):
            mod.verify_pr("org/api", {
                "baseRefName": "staging", "state": "CLOSED",
                "mergedAt": None, "url": "https://github.com/org/api/pull/1"
            }, "staging")

    def test_existing_pr_cannot_change_target(self):
        with self.assertRaisesRegex(RuntimeError, "targets main"):
            mod.verify_pr("org/api", {
                "baseRefName": "main", "state": "OPEN",
                "mergedAt": None, "url": "https://github.com/org/api/pull/2"
            }, "staging")

    def test_merge_uses_normal_method_only(self):
        calls = []
        views = [
            '{"headRefOid":"abc123","baseRefName":"staging","state":"OPEN","mergeStateStatus":"CLEAN","mergedAt":null}',
            '{"headRefOid":"abc123","baseRefName":"staging","state":"MERGED","mergeStateStatus":"UNKNOWN","mergedAt":"2026-11-13T12:00:00Z"}',
        ]
        def cmd(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("gh", "pr", "view"):
                return views.pop(0)
            return ""
        with mock.patch.object(mod, "gh_api", return_value={"allow_merge_commit": True}):
            with mock.patch.object(mod.time, "sleep"):
                with mock.patch.object(mod, "cmd", side_effect=cmd):
                    mod.merge_regular_pr("org/api", 1, "staging", "abc123")
        self.assertTrue(any(x[:3] == ("gh", "pr", "merge") for x in calls))
        self.assertFalse(any("--admin" in x or "--force" in x for x in calls))

    def test_dry_run_does_not_install(self):
        env = {"OPERATION": "DRY_RUN", "CONFIRMATION": "", "ORG": "org",
               "TARGET": "single", "REPOSITORY": "api"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=(
                "CANDIDATE main/staging/cicd.yml present", {})):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_frontend_is_skipped_even_when_explicitly_selected(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY", "ORG": "org",
               "TARGET": "single", "REPOSITORY": "frontend"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=(
                "SKIP frontend/unknown repository", None)):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_stale_rollout_pr_discovery(self):
        results = '[{"headRefName":"org-action/staging-promotion-7319-oldhash","url":"https://github.com/org/api/pull/1","baseRefName":"staging"}]'
        with mock.patch.object(mod, "cmd", return_value=results):
            prs = mod.managed_open_prs("org/api", "7319")
        self.assertEqual(len(prs), 1)

    def test_squash_only_repo_fails_closed(self):
        with mock.patch.object(mod, "gh_api", return_value={"allow_merge_commit": False}):
            with self.assertRaisesRegex(RuntimeError, "merge commits disabled"):
                mod.merge_regular_pr("org/api", 1, "staging", "abc")

    def test_merge_queue_polling(self):
        states = [
            {"headRefOid":"abc","baseRefName":"staging","state":"OPEN",
             "mergeStateStatus":"CLEAN","mergedAt":None},
            {"headRefOid":"abc","baseRefName":"staging","state":"OPEN",
             "mergeStateStatus":"BLOCKED","mergedAt":None},
            {"headRefOid":"abc","baseRefName":"staging","state":"MERGED",
             "mergeStateStatus":"UNKNOWN","mergedAt":"2026-11-13T12:00:00Z"},
        ]
        calls = []
        def fake_cmd(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("gh", "pr", "view"):
                return __import__("json").dumps(states.pop(0))
            return ""
        with mock.patch.object(mod, "gh_api", return_value={"allow_merge_commit": True}):
            with mock.patch.object(mod.time, "sleep"):
                with mock.patch.object(mod, "cmd", side_effect=fake_cmd):
                    self.assertEqual(mod.merge_regular_pr("org/api", 1, "staging", "abc"), "already merged")
        self.assertEqual(sum(args[:3] == ("gh", "pr", "merge") for args in calls), 1)


if __name__ == "__main__":
    unittest.main()
