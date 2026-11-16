"""Regression tests for staging workflow distribution through shared Guidelines lifecycle."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import inspect
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "rollout_staging", ROOT / "scripts/rollout-staging-promotion.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shared_spec = importlib.util.spec_from_file_location(
    "shared_guidelines", ROOT / "scripts/sync-guidelines-auto-merge.py")
shared = importlib.util.module_from_spec(shared_spec)
shared_spec.loader.exec_module(shared)


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

    def test_payload_is_exactly_one_centrally_managed_workflow(self):
        self.assertEqual(mod.SOURCE_FILES, {
            "staging-deploy/auto-staging-caller.yml":
                ".github/workflows/auto-staging-deploy.yml",
        })
        source = inspect.getsource(mod.install)
        self.assertIn('rollout_family="staging-deployment"', source)
        self.assertNotIn('gh pr merge', source)
        self.assertNotIn('git checkout', source)

    def test_existing_guideline_destination_restrictions_remain(self):
        self.assertEqual(shared.sync.validate_destination(
            ".github/workflows/auto-staging-deploy.yml"),
            ".github/workflows/auto-staging-deploy.yml")
        with self.assertRaises(ValueError):
            shared.sync.validate_destination(".github/workflows/unsafe.yml")
        with self.assertRaises(ValueError):
            shared.sync.validate_destination("../auto-staging-deploy.yml")

    def test_family_rejects_unexpected_rollout_file(self):
        with self.assertRaisesRegex(ValueError, "exact workflow caller"):
            shared.sync.process(
                {"name": "api", "defaultBranchRef": {"name": "main"}}, "org",
                {"backend": [{"source_url": "file", "destination": "other.yml"}]},
                {"file": b"data"}, "staging", "APPLY", "7319",
                rollout_family="staging-deployment")

    def test_shared_rollout_is_staging_first_with_production_separate(self):
        source = inspect.getsource(shared.rollout)
        self.assertIn('"staging", "APPLY"', source)
        self.assertIn('"production", "APPLY"', source)
        self.assertLess(source.index('"staging", "APPLY"'),
                        source.index('"production", "APPLY"'))
        self.assertIn("merge_guideline_pr", source)
        self.assertIn("rollout_family=rollout_family", source)

    def test_shared_merge_uses_authorized_app_and_sha_guards(self):
        source = inspect.getsource(shared.merge_guideline_pr)
        self.assertIn('"--admin", "--match-head-commit", verified_head', source)
        self.assertIn("verify_merged_document_commit(", source)
        self.assertIn("verified_base", source)
        self.assertIn("expected_prefix", source)

    def test_confirmation_required_before_any_mutation(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "",
               "ORG": "org", "TARGET": "single", "REPOSITORY": "api"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "cmd") as cmd:
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    mod.main()
                cmd.assert_not_called()

    def test_dry_run_does_not_install(self):
        env = {"OPERATION": "DRY_RUN", "CONFIRMATION": "",
               "ORG": "org", "TARGET": "single", "REPOSITORY": "api"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=(
                    "CANDIDATE main/staging/cicd.yml present", {})):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_apply_skips_frontend(self):
        env = {"OPERATION": "APPLY", "CONFIRMATION": "APPLY",
               "ORG": "org", "TARGET": "single", "REPOSITORY": "frontend"}
        with mock.patch.dict(mod.os.environ, env):
            with mock.patch.object(mod, "check_repo", return_value=(
                    "SKIP frontend/unknown repository", None)):
                with mock.patch.object(mod, "install") as install:
                    mod.main()
                    install.assert_not_called()

    def test_shared_rollout_failure_propagates(self):
        fake = SimpleNamespace(rollout=mock.Mock(return_value="blocked"))
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=mock.Mock()))
        with mock.patch("importlib.util.spec_from_file_location",
                        return_value=fake_spec):
            with mock.patch("importlib.util.module_from_spec", return_value=fake):
                with self.assertRaisesRegex(RuntimeError, "returned blocked"):
                    mod.install("org/api", "api", "7319")
        fake.rollout.assert_called_once()
        args, kwargs = fake.rollout.call_args
        self.assertEqual(args[0]["name"], "api")
        self.assertEqual(args[2]["backend"][0]["destination"],
                         ".github/workflows/auto-staging-deploy.yml")
        self.assertEqual(kwargs["rollout_family"], "staging-deployment")


if __name__ == "__main__":
    unittest.main()
