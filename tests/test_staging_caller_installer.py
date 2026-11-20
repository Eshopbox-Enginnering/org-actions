"""Regression tests for single-repo, staging-first App caller rollout."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "install_caller", ROOT / "staging-deploy/install-caller.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_rollout_merges_staging_before_creating_independent_main_pr(self):
        order = []

        def phase(full, target, issue, operation):
            order.append((full, target, issue, operation))
            return "merged"

        def cmd(*args, **kwargs):
            return "staging" if args[-1] == ".name" else ""

        with mock.patch.object(installer, "phase", side_effect=phase), \
             mock.patch.object(installer, "gh_json", return_value={
                 "defaultBranchRef": {"name": "main"},
                 "isArchived": False, "isFork": False
             }), \
             mock.patch.object(installer, "cmd", side_effect=cmd), \
             mock.patch.dict(installer.os.environ, {"CONFIRMATION": "APPLY"}):
            installer.rollout("eshopbox-internal-apps", "APPLY", "7064")
        self.assertEqual([item[1] for item in order], ["staging", "main"])

    def test_staging_merge_failure_stops_before_main(self):
        calls = []
        def phase(full, target, issue, operation):
            calls.append(target)
            if target == "staging":
                raise RuntimeError("staging merge verification failed")
        with mock.patch.object(installer, "phase", side_effect=phase), \
             mock.patch.object(installer, "gh_json", return_value={
                 "defaultBranchRef": {"name": "main"},
                 "isArchived": False, "isFork": False
             }), \
             mock.patch.object(installer, "cmd", return_value="staging"), \
             mock.patch.dict(installer.os.environ, {"CONFIRMATION": "APPLY"}):
            with self.assertRaisesRegex(RuntimeError, "staging merge"):
                installer.rollout("eshopbox-internal-apps", "APPLY", "7064")
        self.assertEqual(calls, ["staging"])

    def test_bad_repository_name_rejected_before_any_remote_operation(self):
        with mock.patch.object(installer, "gh_json") as gh:
            for bad in ("foo/bar", "../repo", "some repo", ""):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    installer.rollout(bad, "DRY_RUN", "7064")
            gh.assert_not_called()

    def test_apply_requires_exact_confirmation(self):
        with mock.patch.dict(installer.os.environ, {"CONFIRMATION": "no"}), \
             mock.patch.object(installer, "gh_json") as gh:
            with self.assertRaisesRegex(ValueError, "confirmation"):
                installer.rollout("eshopbox-internal-apps", "APPLY", "7064")
            gh.assert_not_called()

    def test_canonical_caller_is_only_target_change(self):
        self.assertEqual(installer.DEST, ".github/workflows/auto-staging-deploy.yml")
        text = installer.CALLER.decode("utf-8")
        self.assertIn("org-actions/.github/workflows/reusable-staging-deploy.yml@main", text)
        self.assertIn("secrets: inherit", text)
        self.assertIn("/deploy-staging", text)

    def test_installer_preserves_independent_phase_cloning_and_admin_head_pin(self):
        source = (ROOT / "staging-deploy/install-caller.py").read_text()
        self.assertIn('def phase(full, target, issue, operation)', source)
        self.assertIn('"--admin", "--match-head-commit", head', source)
        self.assertIn('if changed != [DEST]', source)
        self.assertIn('parents[1:] != [base, head]', source)
        self.assertIn('merged != expected', source)

    def test_workflow_is_trusted_manual_dispatch_with_existing_app(self):
        source = (ROOT / ".github/workflows/install-central-staging-caller.yml").read_text()
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("environment: agents-org-production", source)
        self.assertIn("ORG_AGENTS_APP_ID", source)
        self.assertIn("ORG_AGENTS_APP_PRIVATE_KEY", source)
        self.assertIn("ref: main", source)
        self.assertNotIn("pull_request:", source)


if __name__ == "__main__":
    unittest.main()
