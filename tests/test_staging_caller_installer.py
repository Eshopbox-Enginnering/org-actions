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
             mock.patch.object(installer, "assert_eligible"), \
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
             mock.patch.object(installer, "assert_eligible"), \
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
        def api(path):
            if "/actions/workflows/" in path:
                return {"state": "active"}
            return info
        with mock.patch.object(planner, "api_json", side_effect=api), \
             mock.patch.object(planner, "file_at", return_value=b"on:\n  workflow_dispatch:\n"):
            state, reason = planner.inspect("testrepo","all")
            self.assertEqual(state,"skipped")
            self.assertIn("exactly one active",reason)

    def test_bulk_workflow_limits_concurrency_and_preserves_workflow_permission(self):
        source = (ROOT / ".github/workflows/install-central-staging-caller.yml").read_text()
        for target in ("single","selected","backend","all"):
            self.assertIn(target,source)
        self.assertEqual(source.count("environment: agents-org-production"), 1)
        self.assertIn("install-batch.py",source)
        self.assertIn("permission-workflows: write",source)
        self.assertIn("rollout-report.json",source)
        self.assertIn("operation == 'APPLY'",source)
        self.assertIn("steps.discover.outputs.repositories",source)
        self.assertEqual(source.count("permission-actions: read"), 2)


    def test_names_reject_case_insensitive_duplicates(self):
        from argparse import Namespace
        import importlib.util
        spec = importlib.util.spec_from_file_location("plan_case",ROOT/"staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            planner.names(Namespace(target="selected",repository="",repositories="Service,service"))

    def test_bulk_listing_includes_archived_and_forked_for_reporting(self):
        from argparse import Namespace
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location("plan_all",ROOT/"staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        records = [
            {"name":"forked","isArchived":False,"isFork":True},
            {"name":"archived","isArchived":True,"isFork":False},
            {"name":"active","isArchived":False,"isFork":False},
        ]
        with mock.patch.object(planner,"gh",return_value=json.dumps(records)):
            self.assertEqual(planner.names(Namespace(target="all",repository="",repositories="")),
                             ["active","archived","forked"])

    def test_planner_requires_one_active_and_dispatchable_workflow(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("plan_active",ROOT/"staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        info = {"archived":False,"fork":False,"disabled":False,"default_branch":"main"}
        def api(path):
            if "/actions/workflows/" in path:
                return {"state":"active" if path.endswith("ci-cd.yml") else "disabled_manually"}
            return info
        def file_at(repo,path,ref):
            if path.endswith("ci-cd.yml"):
                return b"on:\n  workflow_dispatch:\n"
            return None
        with mock.patch.object(planner,"api_json",side_effect=api), \
             mock.patch.object(planner,"file_at",side_effect=file_at):
            self.assertEqual(planner.inspect("myrepo","all")[0],"eligible")
        with mock.patch.object(planner,"api_json",side_effect=api), \
             mock.patch.object(planner,"file_at",return_value=b"on: push"):
            state,reason = planner.inspect("myrepo","all")
            self.assertEqual(state,"skipped")
            self.assertIn("lacks workflow_dispatch",reason)

    def test_revalidates_before_both_apply_phases_and_blocks_stale_plan(self):
        checks=[]
        phases=[]
        def check(repo):
            checks.append(repo)
            if len(checks)==2:
                raise ValueError("Repository no longer eligible")
        with mock.patch.object(installer,"gh_json",return_value={
                 "defaultBranchRef":{"name":"main"},"isArchived":False,"isFork":False
             }), \
             mock.patch.object(installer,"cmd",return_value="staging"), \
             mock.patch.object(installer,"assert_eligible",side_effect=check), \
             mock.patch.object(installer,"phase",side_effect=lambda full,target,issue,op: phases.append(target)), \
             mock.patch.dict(installer.os.environ,{"CONFIRMATION":"APPLY"}):
            with self.assertRaisesRegex(ValueError,"no longer eligible"):
                installer.rollout("service","APPLY","7064")
        self.assertEqual(phases,["staging"])
        self.assertEqual(len(checks),2)

    def test_discovery_token_is_read_only(self):
        source=(ROOT/".github/workflows/install-central-staging-caller.yml").read_text()
        discovery=source.split("name: Mint organization App discovery token",1)[1].split(
            "name: Discover and check repository eligibility",1)[0]
        self.assertIn("permission-contents: read",discovery)
        self.assertIn("permission-actions: read",discovery)
        self.assertNotIn("permission-workflows: write",discovery)

    def test_yaml_workflow_names_supported(self):
        spec = importlib.util.spec_from_file_location("plan_yaml", ROOT / "staging-deploy/plan-rollout.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        for filename in ("ci-cd.yaml", "cicd.yaml"):
            with self.subTest(filename=filename):
                def api(path):
                    if "/actions/workflows/" in path:
                        return {"state": "active" if path.endswith(filename) else "disabled_manually"}
                    return {"default_branch": "main"}
                with mock.patch.object(planner, "api_json", side_effect=api), mock.patch.object(planner, "file_at", side_effect=lambda repo,path,ref: b"on:\\n  workflow_dispatch:\\n" if path.endswith(filename) else None):
                    self.assertEqual(planner.inspect("service", "all")[0], "eligible")

if __name__ == "__main__":
    unittest.main()
