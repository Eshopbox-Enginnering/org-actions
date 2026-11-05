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
