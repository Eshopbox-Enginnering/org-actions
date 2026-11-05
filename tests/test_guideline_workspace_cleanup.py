"""Regression tests for ephemeral Git checkout cleanup.

Git can start background maintenance while a temporary repository is being
deleted; fail closed after bounded retries rather than raising on one transient
ENOTEMPTY. Disabling git maintenance prevents the common cause of the race.
"""
import errno
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "guideline_sync", ROOT / "scripts/sync-engineering-guidelines.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class WorkspaceCleanupTests(unittest.TestCase):
    def test_workspace_is_removed_when_body_succeeds(self):
        with sync.rollout_workspace() as tmp:
            path = Path(tmp)
            (path / "nested").mkdir()
            (path / "nested" / "item.txt").write_text("hello")
        self.assertFalse(path.exists())

    def test_workspace_is_removed_when_body_fails(self):
        with self.assertRaisesRegex(RuntimeError, "body error"):
            with sync.rollout_workspace() as tmp:
                path = Path(tmp)
                raise RuntimeError("body error")
        self.assertFalse(path.exists())

    def test_transient_directory_not_empty_is_retried(self):
        with tempfile.TemporaryDirectory() as container:
            target = Path(container) / "workspace"
            target.mkdir()
            real_rmtree = sync.shutil.rmtree
            attempts = []
            def flaky_remove(path):
                attempts.append(path)
                if len(attempts) == 1:
                    raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
                return real_rmtree(path)
            with patch.object(sync.tempfile, "mkdtemp", return_value=str(target)):
                with patch.object(sync.shutil, "rmtree", side_effect=flaky_remove):
                    with patch.object(sync.time, "sleep") as sleep:
                        with sync.rollout_workspace():
                            pass
            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once()
            self.assertFalse(target.exists())

    def test_permanent_cleanup_failure_is_not_suppressed(self):
        with patch.object(sync.tempfile, "mkdtemp", return_value="/tmp/example-guidelines"):
            with patch.object(sync.shutil, "rmtree",
                              side_effect=OSError(errno.ENOTEMPTY, "Directory not empty")) as remove:
                with patch.object(sync.time, "sleep") as sleep:
                    with self.assertRaises(OSError):
                        with sync.rollout_workspace():
                            pass
        self.assertEqual(remove.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_git_disables_autogc_and_background_maintenance(self):
        completed = subprocess.CompletedProcess(["git", "status"], 0, stdout="ok", stderr="")
        with patch.object(sync.subprocess, "run", return_value=completed) as run:
            self.assertEqual(sync.cmd("git", "status"), "ok")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "gc.auto")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "0")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "maintenance.auto")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")


if __name__ == "__main__":
    unittest.main()
