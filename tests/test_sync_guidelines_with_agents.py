"""Regression tests for combined guideline + AGENTS.md rollout entrypoint."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sync_guidelines_with_agents", ROOT / "scripts/sync-guidelines-with-agents.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

BASE = [
    "run", "--operation", "DRY_RUN", "--target", "selected",
    "--repositories", "a-service,b-service", "--phase", "staging",
    "--source-scope", "backend", "--issue", "7218",
    "--org", "Eshopbox-Enginnering",
]


class CombinedRolloutTests(unittest.TestCase):
    def test_selected_names_are_validated_and_unique(self):
        self.assertEqual(module.selected_repositories("a-service, b-service\nc-service"),
                         ["a-service", "b-service", "c-service"])
        for bad in ("", "a,b,a", "owner/repo", "repo; echo hacked",
                    ",".join("repo" + str(i) for i in range(26))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.selected_repositories(bad)

    def test_selected_dry_run_orders_guidelines_before_agents_per_repo(self):
        calls = []
        with patch.object(sys, "argv", BASE), patch.object(
                module, "run_checked", side_effect=lambda command, env: calls.append((command, dict(env)))):
            module.main()
        self.assertEqual(len(calls), 4)
        for index, repo in enumerate(("a-service", "b-service")):
            guideline, agent = calls[2 * index:2 * index + 2]
            self.assertIn("sync-guidelines-auto-merge.py", guideline[0][1])
            self.assertEqual(guideline[0][guideline[0].index("--repository") + 1], repo)
            self.assertEqual(agent[0], ["bash", "scripts/update-agents.sh"])
            self.assertEqual(agent[1]["TARGET"], "single")
            self.assertEqual(agent[1]["TARGET_REPO"], repo)
            self.assertEqual(agent[1]["OPERATION"], "DRY_RUN")

    def test_disabled_agents_keeps_guidelines_only(self):
        argv = BASE + ["--update-agents", "false"]
        calls = []
        with patch.object(sys, "argv", argv), patch.object(
                module, "run_checked", side_effect=lambda command, env: calls.append(command)):
            module.main()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("sync-guidelines-auto-merge.py" in cmd[1] for cmd in calls))

    def test_production_phase_cannot_implicitly_update_agents(self):
        with patch.object(sys, "argv", BASE + ["--update-agents", "true",
                                             "--phase", "production"]):
            with self.assertRaises(SystemExit):
                module.main()

    def test_failure_does_not_continue_to_agents_or_next_repo(self):
        calls = []
        def failing(command, env):
            calls.append(command)
            raise RuntimeError("guideline failed")
        with patch.object(sys, "argv", BASE), patch.object(
                module, "run_checked", side_effect=failing):
            with self.assertRaises(RuntimeError):
                module.main()
        self.assertEqual(len(calls), 1)

    def test_apply_uses_one_combined_creator_per_selected_repo(self):
        argv = [value if value != "DRY_RUN" else "APPLY" for value in BASE]
        calls = []
        with patch.dict(os.environ, {"CONFIRMATION": "APPLY"}), patch.object(
                sys, "argv", argv), patch.object(
                module, "run_checked",
                side_effect=lambda command, env: calls.append(command)):
            module.main()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(
            command[1] == "scripts/sync-guidelines-and-agents.py"
            for command in calls))
        self.assertEqual(
            [command[command.index("--repository") + 1] for command in calls],
            ["a-service", "b-service"])

    def test_apply_without_agents_keeps_guideline_only_flow(self):
        argv = [value if value != "DRY_RUN" else "APPLY" for value in BASE]
        argv += ["--update-agents", "false"]
        calls = []
        with patch.dict(os.environ, {"CONFIRMATION": "APPLY"}), patch.object(
                sys, "argv", argv), patch.object(
                module, "run_checked",
                side_effect=lambda command, env: calls.append(command)):
            module.main()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(
            command[1] == "scripts/sync-guidelines-auto-merge.py"
            for command in calls))

    def test_existing_single_target_is_supported(self):
        argv = [
            "run", "--operation", "DRY_RUN", "--target", "single",
            "--repository", "one-service", "--phase", "staging",
            "--source-scope", "backend", "--issue", "7218",
            "--org", "Eshopbox-Enginnering",
        ]
        calls = []
        with patch.object(sys, "argv", argv), patch.object(
                module, "run_checked", side_effect=lambda command, env: calls.append(command)):
            module.main()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("--target") + 1], "single")

    def test_empty_or_wrongly_scoped_repositories_rejected(self):
        argv = [value for i, value in enumerate(BASE) if i not in (4, 5)]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            module.main()


if __name__ == "__main__":
    unittest.main()
