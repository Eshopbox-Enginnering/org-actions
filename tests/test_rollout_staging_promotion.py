"""Safety checks for PR-only staging promotion rollout."""
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
        self.assertEqual(mod.named_repositories("selected", "", "repo-one,repo-two", "org"),
                         ["repo-one", "repo-two"])
        for value in ("", "repo-one,repo-one", "owner/repo", "../repo"):
            with self.assertRaises(ValueError):
                mod.named_repositories("selected", "", value, "org")
        with self.assertRaises(ValueError):
            mod.named_repositories("selected", "", ",".join(f"repo{i}" for i in range(26)), "org")

    def test_backend_selection_and_classification(self):
        with mock.patch.object(mod, "cmd", return_value='[{"name":"backend-a","isArchived":false},{"name":"frontend-a","isArchived":false}]'):
            self.assertEqual(mod.named_repositories("backend", "", "", "org"),
                             ["backend-a", "frontend-a"])
        def api(path):
            if path.endswith("/contents"):
                return [{"name": "pom.xml"}]
            raise AssertionError(path)
        with mock.patch.object(mod, "gh_api", side_effect=api):
            self.assertTrue(mod.detect_backend("org", "backend-a"))
        with mock.patch.object(mod, "gh_api", return_value=[{"name": "angular.json"}, {"name": "package.json"}]):
            self.assertFalse(mod.detect_backend("org", "frontend-a"))
        with mock.patch.object(mod, "gh_api", return_value=[{"name": "README.md"}]):
            self.assertFalse(mod.detect_backend("org", "unknown-a"))

    def test_node_backend_detection_and_frontend_exclusion(self):
        with mock.patch.object(mod, "gh_api", return_value=[{"name": "package.json"}]):
            with mock.patch.object(mod, "cmd", return_value='{"dependencies":{"express":"^4"}}'):
                self.assertTrue(mod.detect_backend("org", "api"))
            with mock.patch.object(mod, "cmd", return_value='{"dependencies":{"react":"^18"}}'):
                self.assertFalse(mod.detect_backend("org", "frontend"))

    def test_single_frontend_repo_is_skipped_for_apply(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "esb-product-engine-jobs"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=("SKIP frontend/unknown repository", None)):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_source_payload_targets_are_tightly_scoped(self):
        self.assertEqual(set(mod.SOURCE_FILES.values()), {
            ".github/scripts/auto-staging-deploy.cjs",
            ".github/scripts/auto-staging-pr.cjs",
            ".github/scripts/auto-staging-deploy.test.cjs",
            ".github/scripts/auto-staging-pr.test.cjs",
            ".github/workflows/auto-staging-deploy.yml",
        })
        self.assertNotIn(".github/workflows/cicd.yml", mod.SOURCE_FILES)

    def test_apply_refuses_missing_confirmation_without_any_git_calls(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "esb-product-engine-jobs"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "cmd") as git:
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    mod.main()
                git.assert_not_called()

    def test_non_allowlisted_repo_never_installed(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "unreviewed-example"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=("CANDIDATE", {})):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_archived_pilot_is_skipped_without_install(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "esb-product-engine-jobs"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=("SKIP archived/disabled", None)):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_orphan_branch_is_counted_as_apply_failure(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "esb-product-engine-jobs"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=("CANDIDATE main/staging/cicd.yml present", {})):
                with mock.patch.object(mod, "install", side_effect=RuntimeError("orphaned rollout branch")):
                    with self.assertRaises(SystemExit):
                        mod.main()

    def test_closed_rollout_pr_is_blocker_not_success(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp:
            clone = Path(temp)
            files = {src: (ROOT / src).read_bytes() for src in mod.SOURCE_FILES}
            def command(*args, cwd=None, check=True):
                if args[:3] == ("gh", "repo", "clone"):
                    for src, dest in mod.SOURCE_FILES.items():
                        target = clone / dest
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"old data")
                    return ""
                if args[:3] == ("git", "rev-parse", "HEAD"):
                    return "a" * 40
                if args[:3] == ("gh", "pr", "list"):
                    return mock.Mock(returncode=0, stdout='[{"state":"CLOSED","url":"https://github.com/org/repo/pull/1"}]')
                raise AssertionError(f"Unexpected command: {args}")
            # temp workspace chosen by the rollout function remains under a
            # dedicated clone directory. Simulate only the pre-commit sequence.
            with mock.patch.object(mod.tempfile, "TemporaryDirectory") as temp_dir:
                temp_dir.return_value.__enter__.return_value = str(clone)
                with mock.patch.object(mod, "cmd", side_effect=command):
                    with self.assertRaisesRegex(RuntimeError, "closed"):
                        mod.install("org/repo", "repo", "main", "7319")

    def test_dry_run_never_installs_even_approved_repo(self):
        env = {"OPERATION": "DRY_RUN", "CONFIRMATION": "", "ORG": "Eshopbox-Enginnering",
               "TARGET": "single", "REPOSITORY": "esb-product-engine-jobs"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=("CANDIDATE", {})):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
