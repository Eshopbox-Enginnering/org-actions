"""Regression coverage for safe legacy AGENTS.md block migration."""
import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/update-agents.sh").read_text()
MIGRATOR = SCRIPT.split("apply_agents_content() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
TRUSTED = (ROOT / "payload/agents-legacy-v1.md").read_bytes()
CURRENT = (ROOT / "payload/agents-update.md").read_bytes()
LEGACY_BEGIN = b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE -->\n"
LEGACY_END = b"<!-- END ESHOPBOX-AGENTS-UPDATE -->\n"


class LegacyAgentsMigrationTests(unittest.TestCase):
    def run_migrator(self, directory, mode="apply"):
        payload = ROOT / "payload/agents-update.md"
        old = ROOT / "payload/agents-legacy-v1.md"
        digest = hashlib.sha256(CURRENT).hexdigest()[:12]
        return subprocess.run(
            [sys.executable, "-", str(payload), digest, mode, str(old)],
            input=MIGRATOR, text=True, capture_output=True, cwd=directory,
            check=False,
        )

    def make_repo(self, directory, body=TRUSTED, before=b"", after=b""):
        data = before + LEGACY_BEGIN + body + LEGACY_END + after
        (directory / "AGENTS.md").write_bytes(data)
        return data

    def test_trusted_legacy_migrates_without_touching_repo_owned_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            original = self.make_repo(root, before=b"# Repo instructions\n\n",
                                      after=b"\n## Keep my custom section\n")
            check = self.run_migrator(root, "check")
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual((root / "AGENTS.md").read_bytes(), original)
            migrated = self.run_migrator(root)
            self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
            result = (root / "AGENTS.md").read_bytes()
            digest = hashlib.sha256(CURRENT).hexdigest()[:12].encode("ascii")
            self.assertTrue(result.startswith(b"# Repo instructions\n\n"))
            self.assertTrue(result.endswith(b"\n## Keep my custom section\n"))
            self.assertIn(b"<!-- BEGIN ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->", result)
            self.assertIn(b"<!-- END ESHOPBOX-AGENTS-UPDATE:" + digest + b" -->", result)
            self.assertIn(CURRENT, result)
            self.assertNotIn(LEGACY_BEGIN, result)
            repeat = self.run_migrator(root)
            self.assertEqual(repeat.returncode, 2, repeat.stdout + repeat.stderr)
            self.assertEqual(result, (root / "AGENTS.md").read_bytes())

    def test_modified_legacy_content_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            original = self.make_repo(root, body=TRUSTED.replace(b"## Git", b"## Altered Git", 1))
            outcome = self.run_migrator(root)
            self.assertEqual(outcome.returncode, 1, outcome.stdout + outcome.stderr)
            self.assertIn("differs from trusted historical payload", outcome.stdout)
            self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_mismatched_and_duplicated_markers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            originals = [
                LEGACY_BEGIN + TRUSTED + b"<!-- END ESHOPBOX-AGENTS-UPDATE:123456789abc -->\n",
                LEGACY_BEGIN + TRUSTED + LEGACY_END + LEGACY_BEGIN + TRUSTED + LEGACY_END,
            ]
            for original in originals:
                (root / "AGENTS.md").write_bytes(original)
                outcome = self.run_migrator(root)
                self.assertEqual(outcome.returncode, 1, outcome.stdout + outcome.stderr)
                self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_mixed_frontend_php_backend_uses_backend_precedence(self):
        php_check = SCRIPT.index('if [ -f "$dir/composer.json" ]; then')
        frontend_check = SCRIPT.index('if [ -f "$dir/angular.json" ]')
        self.assertLess(php_check, frontend_check)


if __name__ == "__main__":
    unittest.main()
