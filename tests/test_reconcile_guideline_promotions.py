import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reconcile_guidelines", ROOT / "scripts/reconcile-guideline-promotions.py")
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)


class GuidelinePromotionTests(unittest.TestCase):
    def setUp(self):
        self.meta = {"file_id": "all", "source_url": "", "destination": "",
                     "source_scope": "backend", "issue": "7218", "kind": "backend",
                     "digest": "123456789abc"}
        self.repo = "Eshopbox-Enginnering/inventory-engine"

    def make_pr(self, **changes):
        result = {
            "merged_at": "2026-11-04T00:00:00Z",
            "base": {"ref": "staging"},
            "head": {"ref": "org-action/guidelines-backend-123456789abc-abc123",
                     "repo": {"full_name": self.repo}},
            "body": "Central Backlog issue #7218\n\n<!-- eshopbox-guideline-sync:v1 " +
                    json.dumps(self.meta) + " -->",
        }
        result.update(changes)
        return result

    def test_accept_merged_matching_staging_pr(self):
        self.assertEqual(reconcile.eligible(self.make_pr(), self.repo), self.meta)

    def test_never_promote_unmerged_or_unrelated_prs(self):
        with self.subTest("open"):
            self.assertIsNone(reconcile.eligible(self.make_pr(merged_at=None), self.repo))
        with self.subTest("wrong branch"):
            self.assertIsNone(reconcile.eligible(self.make_pr(base={"ref": "main"}), self.repo))
        with self.subTest("wrong source"):
            self.assertIsNone(reconcile.eligible(self.make_pr(head={
                "ref": "feature/something", "repo": {"full_name": self.repo}
            }), self.repo))

    def test_allow_deleted_source_branch_after_merge(self):
        pr = self.make_pr()
        pr["head"]["repo"] = None
        self.assertEqual(reconcile.eligible(pr, self.repo), self.meta)

    def test_ignore_invalid_or_absent_metadata(self):
        self.assertIsNone(reconcile.decode_metadata("regular markdown body"))
        bad = dict(self.meta, issue="../7218")
        self.assertIsNone(reconcile.decode_metadata(
            "<!-- eshopbox-guideline-sync:v1 " + json.dumps(bad) + " -->"))
        bad = dict(self.meta, kind="unknown")
        self.assertIsNone(reconcile.decode_metadata(
            "<!-- eshopbox-guideline-sync:v1 " + json.dumps(bad) + " -->"))

    def test_promote_invokes_existing_script_with_explicit_merged_pr(self):
        with patch.object(reconcile.subprocess, "run") as call:
            call.return_value.returncode = 0
            call.return_value.stdout = "created"
            call.return_value.stderr = ""
            self.assertEqual(reconcile.promote_one(self.repo, 42, self.meta), 0)
        args = call.call_args.args[0]
        self.assertIn("--promotion-pr", args)
        self.assertEqual(args[args.index("--promotion-pr") + 1], "42")
        self.assertIn("--phase", args)
        self.assertEqual(args[args.index("--phase") + 1], "production")
        self.assertEqual(call.call_args.kwargs["env"]["CONFIRMATION"], "APPLY")


if __name__ == "__main__":
    unittest.main()
