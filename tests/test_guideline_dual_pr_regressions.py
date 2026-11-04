import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, call

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dual_guidelines", ROOT / "scripts/sync-guidelines-dual-pr.py")
dual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dual)


class DualGuidelineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.repo = {"name": "inventory-engine", "isArchived": False, "isFork": False,
                     "defaultBranchRef": {"name": "main"}}

    def run_dual(self, staging_result, holder_data, production_result):
        args = ["script", "--operation", "APPLY", "--phase", "staging",
                "--target", "single", "--repository", "inventory-engine",
                "--file-id", "all", "--source-scope", "backend",
                "--org", "Eshopbox-Enginnering", "--issue", "7218"]
        original_catalog = Path.read_text
        with patch("sys.argv", args), patch.dict("os.environ", {"CONFIRMATION": "APPLY"}), \
             patch.object(dual.sync, "gh_json", return_value=self.repo), \
             patch.object(dual.sync, "select_files", return_value={
                 "backend": [{"id": "cache", "source_url": "x", "destination": "docs/engineering-guidelines/cache.md"}]
             }), patch.object(dual.sync, "source_bytes", return_value=b"example"), \
             patch.object(Path, "read_text", return_value='{"backend":[],"frontend":[]}'), \
             patch.object(dual.sync, "process") as process:
            def simulate(*args, **kwargs):
                if args[4] == 'staging':
                    kwargs["stage_pr_holder"].update(holder_data)
                    return staging_result
                return production_result
            process.side_effect = simulate
            dual.main()
            return process.call_args_list

    def test_open_staging_pr_triggers_production_pr(self):
        calls = self.run_dual("created", {"number": 33}, "created")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1].kwargs["promotion_pr"], 33)
        self.assertTrue(calls[-1].kwargs["allow_open_staging"])

    def test_no_staging_changes_still_reconciles_production(self):
        calls = self.run_dual("skipped", {"staging_current": True}, "created")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[-1].kwargs["allow_synced_staging"])

    def test_no_staging_changes_and_main_current_is_noop(self):
        calls = self.run_dual("skipped", {"staging_current": True}, "already_synced")
        self.assertEqual(len(calls), 2)

    def test_blocked_production_is_not_silently_reported_success(self):
        with self.assertRaises(SystemExit) as error:
            self.run_dual("skipped", {"staging_current": True}, "blocked")
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
