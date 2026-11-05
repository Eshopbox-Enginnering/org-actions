import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "auto_guideline_rollout", ROOT / "scripts/sync-guidelines-auto-merge.py")
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)


class GuidelineAutoMergeTests(unittest.TestCase):
    def setUp(self):
        self.repo = {"name": "eshopbox-wms-api",
                     "defaultBranchRef": {"name": "main"}}
        self.entry = {"id": "cache", "source_url": "cache",
                      "destination": "docs/engineering-guidelines/cache.md"}
        self.selected = {"backend": [self.entry]}
        self.contents = {"cache": b"canonical"}

    def test_guideline_tree_rejects_symlink_and_gitlink(self):
        destination = self.entry["destination"]
        for mode in ("120000 blob ", "160000 commit ", "100755 blob "):
            with self.subTest(mode=mode), patch.object(
                    auto.sync, "cmd",
                    return_value=mode + "a" * 40 + "\\t" + destination):
                with self.assertRaisesRegex(ValueError, "regular non-executable"):
                    auto.require_regular_guideline_file(
                        Path("/tmp"), "HEAD", destination)

    def test_guideline_tree_accepts_plain_markdown(self):
        destination = self.entry["destination"]
        with patch.object(auto.sync, "cmd",
                          return_value="100644 blob " + "a" * 40 + "\\t" + destination):
            auto.require_regular_guideline_file(Path("/tmp"), "HEAD", destination)

    def test_premerged_pr_is_rejected_before_git_validation(self):
        pr = {"baseRefName": "staging", "headRefName":
              "org-action/guidelines-backend-sample",
              "state": "MERGED", "mergedAt": "2026-11-05T00:00:00Z"}
        with patch.object(auto.sync, "gh_json", return_value=pr), \
             patch.object(auto.sync, "cmd") as cmd:
            with self.assertRaisesRegex(ValueError, "merged before validation"):
                auto.merge_guideline_pr("org/repo", 3, "staging",
                                        [self.entry], self.contents)
        cmd.assert_not_called()

    def test_target_tip_change_rejected_before_admin_merge(self):
        from types import SimpleNamespace
        pr = {"baseRefName": "staging", "headRefName":
              "org-action/guidelines-backend-sample",
              "state": "OPEN", "mergedAt": None}
        def command(*args, **kwargs):
            if args[:2] == ("git", "rev-parse"):
                return "a" * 40 if "org-action/" in args[2] else "b" * 40
            if args[:3] == ("git", "ls-remote", "origin"):
                return "c" * 40 + "\\trefs/heads/staging"
            if args[:3] == ("git", "diff", "--name-only"):
                return self.entry["destination"]
            return ""
        def git_run(args, **kwargs):
            if args[1] == "show":
                missing = args[2].startswith("origin/staging:")
                return SimpleNamespace(returncode=1 if missing else 0,
                                       stdout=b"" if missing else b"canonical")
            return SimpleNamespace(returncode=0)
        with patch.object(auto.sync, "gh_json", return_value=pr), \
             patch.object(auto.sync, "fetch_remote_branch"), \
             patch.object(auto.sync, "cmd", side_effect=command) as cmd, \
             patch.object(auto.subprocess, "run", side_effect=git_run):
            with self.assertRaisesRegex(ValueError, "advanced during validation"):
                auto.merge_guideline_pr("org/repo", 3, "staging",
                                        [self.entry], self.contents)
        self.assertFalse(any(call.args[:3] == ("gh", "pr", "merge")
                             for call in cmd.call_args_list))

    def test_post_merge_verification_rejects_changed_parent(self):
        with patch.object(auto.sync, "cmd", side_effect=[
                "", "merge base-moved expected-head"
        ]):
            with self.assertRaisesRegex(ValueError, "parents differ"):
                auto.verify_merged_document_commit(
                    Path("/tmp"), "merge", "expected-base",
                    "expected-head", {self.entry["destination"]},
                    [self.entry], self.contents)

    def test_merges_staging_before_opening_production(self):
        events = []
        def process(*args, **kwargs):
            phase = args[4]
            events.append("process_" + phase)
            kwargs["stage_pr_holder"]["kind"] = "backend"
            kwargs["stage_pr_holder"]["number"] = (
                12 if phase == "staging" else 13)
            return "created"
        def merge(*args):
            events.append("merge_" + args[2])
            return "merged"
        def pr(*args):
            return {"baseRefName": "staging" if "12" in args else "main"}
        with patch.object(auto.sync, "process", side_effect=process), \
             patch.object(auto.sync, "gh_json", side_effect=pr), \
             patch.object(auto, "merge_guideline_pr", side_effect=merge):
            result = auto.rollout(self.repo, "Eshopbox-Enginnering",
                                  self.selected, self.contents, "7218")
        self.assertEqual(result, "merged")
        self.assertEqual(events, [
            "process_staging", "merge_staging",
            "process_production", "merge_main"])

    def test_bulk_target_skips_other_repository_types(self):
        from unittest.mock import patch
        with patch.object(auto.sync, "process", return_value="skipped") as process, \
             patch.object(auto, "merge_guideline_pr") as merge:
            outcome = auto.rollout(self.repo, "Eshopbox-Enginnering",
                                   self.selected, self.contents, "7218",
                                   single=False)
        self.assertEqual(outcome, "skipped")
        self.assertFalse(process.call_args.kwargs["require_production"])
        merge.assert_not_called()

    def test_single_target_requires_matching_guidelines(self):
        with patch.object(auto.sync, "process", return_value="blocked") as process:
            outcome = auto.rollout(self.repo, "Eshopbox-Enginnering",
                                   self.selected, self.contents, "7218",
                                   single=True)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(process.call_args.kwargs["require_production"])

    def test_staging_failure_does_not_touch_production(self):
        with patch.object(auto.sync, "process", return_value="blocked") as process:
            result = auto.rollout(self.repo, "Eshopbox-Enginnering",
                                  self.selected, self.contents, "7218")
        self.assertEqual(result, "blocked")
        self.assertEqual(process.call_count, 1)

    def test_both_branches_already_current(self):
        def process(*args, **kwargs):
            if args[4] == "staging":
                kwargs["stage_pr_holder"].update(
                    {"kind": "backend", "staging_current": True})
                return "already_synced"
            return "already_synced"
        with patch.object(auto.sync, "process", side_effect=process), \
             patch.object(auto, "merge_guideline_pr") as merge:
            result = auto.rollout(self.repo, "Eshopbox-Enginnering",
                                  self.selected, self.contents, "7218")
        self.assertEqual(result, "synced")
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
