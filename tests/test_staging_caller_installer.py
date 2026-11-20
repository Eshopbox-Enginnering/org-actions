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


    def test_planner_rejects_bad_selection_and_duplicate_repositories(self):
        import importlib.util
        from argparse import Namespace
        spec = importlib.util.spec_from_file_location("rollout_plan", ROOT / "staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        with self.assertRaises(ValueError):
            planner.names(Namespace(target="selected",repository="",repositories="one,one"))
        with self.assertRaises(ValueError):
            planner.names(Namespace(target="single",repository="repo",repositories="another"))
        self.assertEqual(planner.names(Namespace(target="selected",repository="",repositories="one,two")),["one","two"])

    def test_planner_skips_existing_and_unsupported_targets(self):
        import importlib.util
        from argparse import Namespace
        import json
        from tempfile import TemporaryDirectory
        spec = importlib.util.spec_from_file_location("rollout_plan2", ROOT / "staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            args = Namespace(target="selected",repository="",repositories="new,current,unsupported",
                             output=str(out/"matrix.json"),report=str(out/"report.json"))
            outcomes = {"new":("eligible","ready"),"current":("current","installed"),"unsupported":("skipped","no staging")}
            with mock.patch.object(planner, "inspect", side_effect=lambda repo, target: outcomes[repo]):
                with self.assertRaisesRegex(RuntimeError,"ineligible"):
                    planner.plan(args)
            matrix = json.loads((out/"matrix.json").read_text())
            self.assertEqual(matrix["include"],[{"repository":"new"}])
            self.assertEqual(len(json.loads((out/"report.json").read_text())),3)

    def test_planner_fails_closed_on_ambiguous_workflows(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rollout_plan3", ROOT / "staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        info = {"archived":False,"fork":False,"disabled":False,"default_branch":"main"}
        with mock.patch.object(planner, "api_json", return_value=info), \
             mock.patch.object(planner, "file_at", return_value=b"on:\\n  workflow_dispatch:\\n"):
            state, reason = planner.inspect("testrepo","all")
            self.assertEqual(state,"skipped")
            self.assertIn("expected one",reason)

    def test_bulk_workflow_limits_concurrency_and_preserves_workflow_permission(self):
        source = (ROOT / ".github/workflows/install-central-staging-caller.yml").read_text()
        for target in ("single","selected","backend","all"):
            self.assertIn(target,source)
        self.assertIn("max-parallel: 3",source)
        self.assertIn("fail-fast: false",source)
        self.assertIn("permission-workflows: write",source)
        self.assertIn("rollout-report.json",source)
        self.assertIn("operation == 'APPLY'",source)
        self.assertIn("fromJSON(needs.plan.outputs.matrix)",source)


if __name__ == "__main__":
    unittest.main()
