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

    def test_staging_merge_precedes_main_creation(self):
        calls = []
        def phase(full, name, target, issue):
            calls.append(target)
            return "merged"
        with mock.patch.object(mod, "install_phase", side_effect=phase):
            mod.install("org/api", "api", "7319")
        self.assertEqual(calls, ["staging", "main"])

    def test_failure_during_staging_never_opens_main(self):
        calls = []
        def phase(full, name, target, issue):
            calls.append(target)
            raise RuntimeError("required check not green")
        with mock.patch.object(mod, "install_phase", side_effect=phase):
            with self.assertRaisesRegex(RuntimeError, "required check"):
                mod.install("org/api", "api", "7319")
        self.assertEqual(calls, ["staging"])

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
        def cmd(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("gh", "pr", "view"):
                if "headRefOid,baseRefName,state,mergeStateStatus,mergedAt" in args:
                    return '{"headRefOid":"abc123","baseRefName":"staging","state":"OPEN","mergeStateStatus":"CLEAN","mergedAt":null}'
                return '{"mergedAt":"2026-11-13T12:00:00Z","mergeCommit":{"oid":"abc"},"baseRefName":"staging"}'
            return ""
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


if __name__ == "__main__":
    unittest.main()
