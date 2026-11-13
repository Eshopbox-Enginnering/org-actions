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
