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

    def test_payload_is_single_reusable_workflow_caller(self):
        self.assertEqual(mod.SOURCE_FILES, {
            "staging-deploy/auto-staging-caller.yml": ".github/workflows/auto-staging-deploy.yml",
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

    def test_existing_rollout_cleans_only_managed_paths_before_checkout(self):
        import inspect
        source = inspect.getsource(mod.install)
        reset = source.index('cmd("git", "reset", "--hard", "HEAD", cwd=root)')
        clean = source.index('cmd("git", "clean", "-f", "--", *changed, cwd=root)')
        checkout = source.index('cmd("git", "checkout", "-B", branch, "FETCH_HEAD", cwd=root)')
        self.assertLess(reset, clean)
        self.assertLess(clean, checkout)
        self.assertNotIn('cmd("git", "clean", "-fd"', source)
        self.assertNotIn('cmd("git", "clean", "-fdx"', source)

    def test_existing_pr_cannot_change_target(self):
        with self.assertRaisesRegex(RuntimeError, "targets main"):
            mod.verify_pr("org/api", {
                "baseRefName": "main", "state": "OPEN",
                "mergedAt": None, "url": "https://github.com/org/api/pull/2"
            }, "staging")

    def test_app_merge_accepts_blocked_ui_state_without_polling(self):
        pr = {"head": {"sha": "abc123"}, "base": {"ref": "staging"},
              "state": "open", "draft": False, "mergeable": True,
              "mergeable_state": "blocked", "merged": False}
        with mock.patch.object(mod, "gh_api", side_effect=[
                {"allow_merge_commit": True}, pr]):
            with mock.patch.object(mod, "gh_api_merge", return_value={"merged": True}) as merge:
                with mock.patch.object(mod.time, "sleep") as sleep:
                    result = mod.merge_regular_pr("org/api", 1, "staging", "abc123")
        self.assertEqual(result, "merged")
        merge.assert_called_once_with("org/api", 1, "abc123")
        sleep.assert_not_called()

    def test_mergeability_null_retries_only_until_computed(self):
        base = {"head": {"sha": "abc123"}, "base": {"ref": "staging"},
                "state": "open", "draft": False, "merged": False}
        unknown = {**base, "mergeable": None}
        ready = {**base, "mergeable": True, "mergeable_state": "blocked"}
        with mock.patch.object(mod, "gh_api", side_effect=[
                {"allow_merge_commit": True}, unknown, unknown, ready]) as api:
            with mock.patch.object(mod, "gh_api_merge", return_value={"merged": True}) as merge:
                with mock.patch.object(mod.time, "sleep") as sleep:
                    self.assertEqual(mod.merge_regular_pr("org/api", 1, "staging", "abc123"), "merged")
        self.assertEqual(api.call_count, 4)
        self.assertEqual(sleep.call_count, 2)
        merge.assert_called_once()

    def test_mergeability_never_resolving_fails_without_merging(self):
        unknown = {"head": {"sha": "abc123"}, "base": {"ref": "staging"},
                   "state": "open", "draft": False, "mergeable": None, "merged": False}
        with mock.patch.object(mod, "gh_api", side_effect=[
                {"allow_merge_commit": True}] + [unknown] * 6):
            with mock.patch.object(mod, "gh_api_merge") as merge:
                with mock.patch.object(mod.time, "sleep") as sleep:
                    with self.assertRaisesRegex(RuntimeError, "still unknown"):
                        mod.merge_regular_pr("org/api", 1, "staging", "abc123")
        self.assertEqual(sleep.call_count, 5)
        merge.assert_not_called()

    def test_staging_ancestry_required_before_main_merge(self):
        import inspect
        source = inspect.getsource(mod.install)
        self.assertIn('if target == "main":\n                verify_staging_ancestry(root, feature_sha)', source)
        self.assertIn('if target == "main":\n                    verify_staging_ancestry(root, feature_sha)', source)
        commands = []
        def fake_cmd(*args, **kwargs):
            commands.append(args)
            if args[:3] == ("git", "merge-base", "--is-ancestor"):
                return mock.Mock(returncode=1)
            return ""
        with mock.patch.object(mod, "cmd", side_effect=fake_cmd):
            with self.assertRaisesRegex(RuntimeError, "not in remote staging"):
                mod.verify_staging_ancestry(Path("/tmp/rollout-test"), "abc123")
        self.assertEqual(commands[1][:4], ("git", "merge-base", "--is-ancestor", "abc123"))
        self.assertIn("refs/remotes/origin/staging", commands[1])

    def test_staging_ancestry_accepts_exact_commit(self):
        with mock.patch.object(mod, "cmd", side_effect=[
                "", mock.Mock(returncode=0)]) as cmd:
            mod.verify_staging_ancestry(Path("/tmp/rollout-test"), "abc123")
        self.assertEqual(cmd.call_count, 2)

    def test_app_merge_uses_expected_sha_without_admin_override(self):
        with mock.patch.object(mod, "cmd", return_value='{"merged":true}') as cmd:
            self.assertTrue(mod.gh_api_merge("org/api", 8, "abc123")["merged"])
        cmd.assert_called_once_with("gh", "api", "--method", "PUT",
                                    "repos/org/api/pulls/8/merge",
                                    "-f", "merge_method=merge", "-f", "sha=abc123")
        self.assertNotIn("--admin", cmd.call_args.args)

    def test_merge_refuses_mismatched_head_base_draft_and_conflicts(self):
        original = {"head": {"sha": "abc123"}, "base": {"ref": "staging"},
                    "state": "open", "draft": False, "mergeable": True,
                    "merged": False}
        variations = [({"head": {"sha": "wrong"}}, "changed"),
                      ({"base": {"ref": "main"}}, "changed"),
                      ({"draft": True}, "draft"),
                      ({"mergeable": False}, "conflicts")]
        for patch, reason in variations:
            with self.subTest(reason=reason):
                pr = {**original, **patch}
                with mock.patch.object(mod, "gh_api", side_effect=[
                        {"allow_merge_commit": True}, pr]):
                    with mock.patch.object(mod, "gh_api_merge") as merge:
                        with self.assertRaisesRegex(RuntimeError, reason):
                            mod.merge_regular_pr("org/api", 1, "staging", "abc123")
                        merge.assert_not_called()

    def test_merge_denied_by_github_fails_without_retry(self):
        pr = {"head": {"sha": "abc123"}, "base": {"ref": "staging"},
              "state": "open", "draft": False, "mergeable": True, "merged": False}
        with mock.patch.object(mod, "gh_api", side_effect=[
                {"allow_merge_commit": True}, pr]):
            with mock.patch.object(mod, "gh_api_merge", side_effect=RuntimeError("403 forbidden")) as merge:
                with self.assertRaisesRegex(RuntimeError, "403 forbidden"):
                    mod.merge_regular_pr("org/api", 1, "staging", "abc123")
                merge.assert_called_once()

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



if __name__ == "__main__":
    unittest.main()
