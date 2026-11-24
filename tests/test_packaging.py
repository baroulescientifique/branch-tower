import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_both_catalogs_resolve_a_self_contained_shared_skill(self):
        codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        path = codex["plugins"][0]["source"]["path"]
        self.assertEqual(path, claude["plugins"][0]["source"])
        self.assertEqual(codex["name"], claude["name"])
        plugin = ROOT / path
        manifests = [json.loads((plugin / path).read_text())
                     for path in ("plugin.json", ".claude-plugin/plugin.json")]
        self.assertEqual(manifests[0]["name"], manifests[1]["name"])
        self.assertEqual(manifests[0]["version"], manifests[1]["version"])
        skill = plugin / "skills/sdlc"
        for relative in (
            "SKILL.md", "scripts/run.py", "scripts/workflow.yml", "scripts/adapters/claude.py",
            "scripts/adapters/codex.py", "assets/schemas/plan.json", "assets/schemas/worker.json",
            "assets/schemas/verification.json", "assets/templates/spec.md", "assets/templates/plan.md",
            "assets/templates/tasks.md", "references/planner.md", "references/worker.md",
            "references/verifier.md", "references/rules.md", "references/release.md",
        ):
            file = skill / relative
            self.assertTrue(file.is_file() and file.resolve().is_relative_to(skill), relative)
