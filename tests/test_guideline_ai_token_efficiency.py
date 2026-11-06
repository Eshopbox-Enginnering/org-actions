"""Shared AI token-efficiency instructions must be locally distributed to both repo types."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ID = "ai-context"
DEST = "docs/engineering-guidelines/ai-agent-context-token-efficiency.md"
SRC = "engineering-guidelines/ai-agent-context-token-efficiency.md"


class TokenEfficiencyCatalogTests(unittest.TestCase):
    def test_guideline_is_identical_for_backend_and_frontend(self):
        catalog = json.loads((ROOT / "engineering-guidelines/catalog.json").read_text())
        entries = []
        for scope in ("backend", "frontend"):
            matches = [item for item in catalog[scope] if item["id"] == ID]
            self.assertEqual(len(matches), 1, scope)
            entries.append(matches[0])
        self.assertEqual(entries[0], entries[1])
        self.assertEqual(entries[0]["source_url"], SRC)
        self.assertEqual(entries[0]["destination"], DEST)
        self.assertTrue((ROOT / SRC).is_file())

    def test_agent_payload_points_to_local_guideline_conditionally(self):
        payload = (ROOT / "payload/agents-update.md").read_text()
        self.assertIn(DEST, payload)
        self.assertIn("when present", payload)
        self.assertNotIn("Read the entire central repository", payload)

    def test_guideline_does_not_override_safety_or_verification(self):
        guide = (ROOT / SRC).read_text().lower()
        for phrase in ("correctness", "security", "repository-specific",
                       "do not skip", "never print"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)


if __name__ == "__main__":
    unittest.main()
