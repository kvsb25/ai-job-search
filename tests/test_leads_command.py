"""Guards for the /leads command spec and its wiring.

The command is a markdown spec, so these pin the invariants that would break
silently: the header lint_skills.py enforces, the rules that keep outreach
honest (drafts only, no LinkedIn fetching, no invented contacts), the 200
character connection-note limit, and the privacy/permission wiring CI checks.
"""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMAND = REPO / ".claude" / "commands" / "leads.md"
SKILL_DIR = REPO / ".claude" / "skills" / "job-application-assistant"
OUTREACH = SKILL_DIR / "10-outreach.md"


class LeadsCommandSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = COMMAND.read_text(encoding="utf-8")
        cls.rules = OUTREACH.read_text(encoding="utf-8")

    def test_header_matches_lint_format(self):
        self.assertRegex(self.spec.splitlines()[0], r"^# /leads - .+")

    def test_ends_with_important_rules(self):
        self.assertIn("\n## Important Rules\n", self.spec)

    def test_steps_appear_in_order(self):
        steps = re.findall(r"^## Step (\d+)", self.spec, re.M)
        self.assertEqual(steps, [str(i) for i in range(len(steps))])

    def test_drafts_only_and_never_fetches_linkedin(self):
        rules = self.spec.split("## Important Rules", 1)[1]
        self.assertIn("Never send", rules)
        self.assertIn("No LinkedIn fetching", rules)
        self.assertIn("Rule 7", rules)

    def test_connection_note_limit_is_200_everywhere(self):
        for text in (self.spec, self.rules):
            self.assertIn("200 characters", text)
            self.assertNotIn("300 characters", text)
            self.assertNotIn("300-character", text)

    def test_uses_state_tool_and_reads_outreach_rules(self):
        self.assertIn("tools/leads_state.py", self.spec)
        self.assertIn("10-outreach.md", self.spec)
        self.assertIn("Never read `seen_jobs.json` directly", self.spec)

    def test_no_invented_contacts_and_source_required(self):
        self.assertIn("No source, no lead", self.spec)
        self.assertIn("Never invent", self.spec)
        self.assertIn("inferred", self.spec)

    def test_names_claude_code_for_ai_tooling(self):
        self.assertIn("Claude Code", self.spec)
        self.assertIn("Claude Code", self.rules)

    def test_rules_file_has_framework_version_and_is_listed(self):
        self.assertTrue(self.rules.startswith("---\nframework_version:"))
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`10-outreach.md`", skill)

    def test_scrape_rule_7_is_unchanged(self):
        scraper = (REPO / ".claude" / "skills" / "job-scraper" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("No automated people lookups", scraper)


class LeadsWiring(unittest.TestCase):
    def test_lead_data_is_gitignored_and_pinned(self):
        self.assertIn("documents/leads/**", (REPO / ".gitignore").read_text(encoding="utf-8").splitlines())
        guards = (REPO / "tools" / "security_guards.py").read_text(encoding="utf-8")
        self.assertIn('"documents/leads/**"', guards)

    def test_tool_permissions_are_allowlisted_in_both_places(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        guards = (REPO / "tools" / "security_guards.py").read_text(encoding="utf-8")
        for entry in ("Bash(python tools/leads_state.py:*)", "Bash(python3 tools/leads_state.py:*)"):
            self.assertIn(entry, settings["permissions"]["allow"])
            self.assertIn(f'"{entry}"', guards)

    def test_reset_wipes_leads(self):
        reset = (REPO / ".claude" / "commands" / "reset.md").read_text(encoding="utf-8")
        self.assertIn("rm -rf documents/leads/*", reset)


if __name__ == "__main__":
    unittest.main()
