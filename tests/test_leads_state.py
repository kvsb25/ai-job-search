"""Tests for tools/leads_state.py, the state helper behind /leads."""
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "leads_state.py"
sys.path.insert(0, str(REPO / "tools"))

import leads_state  # noqa: E402

TODAY = "2026-09-26"


def ranked(company, title, verdict="Strong Fit", score=80, **extra):
    entry = {
        "title": title, "company": company, "url": "https://example.com/j/1",
        "status": "ranked", "rank_score": score, "rank_verdict": verdict,
        "location_verdict": "PASS", "language_gate": "PASS",
        "strengths": ["python"], "gaps": [],
    }
    entry.update(extra)
    return entry


class LeadsStateTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.state = self.tmp / "seen_jobs.json"
        self.tracker = self.tmp / "tracker.csv"
        self.leads = self.tmp / "leads" / "leads.json"

    def write_state(self, seen):
        self.state.write_text(json.dumps({"seen": seen}, ensure_ascii=False), encoding="utf-8")

    def run_tool(self, *argv, expect=0):
        proc = subprocess.run(
            [sys.executable, str(TOOL), *map(str, argv), "--leads", str(self.leads), "--today", TODAY],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, expect, proc.stderr + proc.stdout)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def candidates(self, *extra):
        return self.run_tool("candidates", "--state", self.state, "--tracker", self.tracker, *extra)

    def person(self, **over):
        person = {"name": "Asha Rao", "title": "Engineering Manager", "source_url": "https://acme.example/team",
                  "why": "leads the platform team", "email": "asha@acme.example", "email_confidence": "published"}
        person.update(over)
        return person

    def save(self, records, expect=0):
        path = self.tmp / "in.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return self.run_tool("save", "--input", path, expect=expect)

    # ------------------------------------------------------------ candidates

    def test_selects_only_strong_and_good_ranked_entries(self):
        self.write_state({
            "acme_eng": ranked("Acme", "Engineer"),
            "beta_eng": ranked("Beta", "Engineer", verdict="Good Fit", score=65),
            "gamma_eng": ranked("Gamma", "Engineer", verdict="Moderate Fit", score=50),
            "delta_eng": {**ranked("Delta", "Engineer"), "status": "new"},
        })
        out = self.candidates()
        self.assertEqual([c["company"] for c in out["selected"]], ["Acme", "Beta"])

    def test_verdicts_flag_widens_selection(self):
        self.write_state({"g": ranked("Gamma", "Engineer", verdict="Moderate Fit", score=50)})
        out = self.candidates("--verdicts", "Strong Fit,Good Fit,Moderate Fit")
        self.assertEqual(out["eligible"], 1)

    def test_vetoed_entries_are_skipped(self):
        self.write_state({
            "a": ranked("Acme", "Engineer", location_verdict="FAIL"),
            "b": ranked("Beta", "Engineer", language_gate="FAIL"),
        })
        self.assertEqual(self.candidates()["eligible"], 0)

    def test_company_already_applied_to_is_skipped_but_other_roles_stay(self):
        self.write_state({
            "a1": ranked("Acme", "Engineer"),
            "a2": ranked("Acme", "Data Scientist", score=70),
            "b1": ranked("Beta", "Engineer"),
        })
        self.tracker.write_text("date,company,role,status\n2026-09-01,Beta,Engineer,applied\n", encoding="utf-8")
        out = self.candidates()
        self.assertEqual([c["company"] for c in out["selected"]], ["Acme"])
        self.assertEqual(len(out["selected"][0]["roles"]), 2)

    def test_tracker_with_bom_is_read(self):
        self.write_state({"b1": ranked("Beta", "Engineer")})
        self.tracker.write_bytes("﻿date,company,role,status\n2026-09-01,Beta,Engineer,applied\n".encode("utf-8"))
        self.assertEqual(self.candidates()["eligible"], 0)

    def test_companies_are_grouped_and_sorted_by_best_score(self):
        self.write_state({
            "a": ranked("Acme", "Engineer", score=70, rank_verdict="Good Fit"),
            "b": ranked("BETA", "Engineer", score=90),
            "b2": ranked("Beta", "Analyst", score=60, rank_verdict="Good Fit"),
        })
        out = self.candidates()
        self.assertEqual([c["company"] for c in out["selected"]], ["BETA", "Acme"])
        self.assertEqual(len(out["selected"][0]["roles"]), 2)

    def test_limit_defers_the_rest(self):
        self.write_state({f"c{i}": ranked(f"Co{i}", "Engineer", score=90 - i) for i in range(4)})
        out = self.candidates("--limit", "2")
        self.assertEqual((len(out["selected"]), out["deferred"]), (2, 2))

    def test_researched_company_is_skipped_unless_refresh(self):
        self.write_state({"a": ranked("Acme", "Engineer")})
        self.save([{"company": "Acme", "people": [self.person()]}])
        out = self.candidates()
        self.assertEqual((out["eligible"], out["already_researched"]), (0, 1))
        refreshed = self.candidates("--refresh")
        self.assertEqual(refreshed["eligible"], 1)
        self.assertEqual(refreshed["selected"][0]["existing_people"][0]["name"], "Asha Rao")

    def test_named_company_bypasses_rank_and_unknown_name_is_still_researchable(self):
        self.write_state({"g": ranked("Gamma", "Engineer", verdict="Weak Fit", score=35)})
        out = self.candidates("--company", "gamma", "--company", "Nowhere Inc")
        self.assertEqual(sorted(c["company"] for c in out["selected"]), ["Gamma", "Nowhere Inc"])
        self.assertEqual(out["unmatched_names"], ["Nowhere Inc"])

    def test_people_search_urls_are_links_only(self):
        self.write_state({"a": ranked("Acme", "AI Engineer")})
        urls = self.candidates()["selected"][0]["linkedin_people_search"]
        self.assertTrue(all(u["url"].startswith("https://www.linkedin.com/search/results/people/?keywords=") for u in urls))
        self.assertEqual(urls[0]["label"], "recruiter")

    def test_non_latin_company_keeps_distinct_identity(self):
        self.write_state({"a": ranked("टाटा", "Engineer"), "b": ranked("इन्फोसिस", "Engineer")})
        selected = self.candidates()["selected"]
        slugs = {c["slug"] for c in selected}
        self.assertEqual(len(slugs), 2)
        self.assertTrue(all(s.startswith("company-") for s in slugs))

    def test_missing_state_file_is_a_clear_error(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL), "candidates", "--state", str(self.state), "--tracker", str(self.tracker)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run /scrape first", proc.stderr)

    # ------------------------------------------------------------------ save

    def test_save_round_trip_writes_json_and_outreach_markdown(self):
        record = {"company": "Acme", "people": [self.person(draft={
            "email_subject": "Platform work", "email_body": "Hello Asha.",
            "connection_note": "Hi Asha, I follow your platform team's work.", "dm": "Thanks for connecting.",
        })]}
        out = self.save([record])
        self.assertEqual(out["saved"][0]["slug"], "acme")
        doc = json.loads(self.leads.read_text(encoding="utf-8"))
        self.assertEqual(doc["companies"]["acme"]["people"][0]["name"], "Asha Rao")
        md = (self.leads.parent / "acme" / "outreach.md").read_text(encoding="utf-8")
        self.assertIn("Drafts only", md)
        self.assertIn("**Subject:** Platform work", md)
        self.assertIn("(published)", md)

    def test_connection_note_limit_is_200_characters(self):
        ok = self.person(draft={"connection_note": "x" * 200})
        self.assertEqual(self.save([{"company": "Acme", "people": [ok]}])["errors"], [])
        too_long = self.person(name="Bo Lee", draft={"connection_note": "x" * 201})
        out = self.save([{"company": "Beta", "people": [too_long]}], expect=1)
        self.assertIn("201 characters", out["errors"][0]["error"])
        self.assertFalse((self.leads.parent / "beta").exists())

    def test_connection_note_counts_code_points_not_bytes(self):
        note = "नमस्ते" * 33  # 198 code points, far more than 200 bytes
        out = self.save([{"company": "Acme", "people": [self.person(draft={"connection_note": note})]}])
        self.assertEqual(out["errors"], [])

    def test_person_without_public_source_is_rejected(self):
        out = self.save([{"company": "Acme", "people": [self.person(source_url="")]}], expect=1)
        self.assertIn("source_url", out["errors"][0]["error"])
        self.assertFalse(self.leads.exists())

    def test_email_confidence_rules(self):
        bad = self.person(email_confidence="guess")
        self.assertIn("email_confidence", self.save([{"company": "A", "people": [bad]}], expect=1)["errors"][0]["error"])
        missing = self.person(email="", email_confidence="inferred")
        self.assertIn("needs an email", self.save([{"company": "B", "people": [missing]}], expect=1)["errors"][0]["error"])
        none = self.person(email="ignored@x.example", email_confidence="none")
        self.save([{"company": "C", "people": [none]}])
        stored = json.loads(self.leads.read_text(encoding="utf-8"))["companies"]["c"]["people"][0]
        self.assertEqual(stored["email"], "")

    def test_inferred_email_is_labelled_unverified_in_markdown(self):
        person = self.person(email_confidence="inferred", draft={"email_body": "Hi"})
        self.save([{"company": "Acme", "people": [person]}])
        md = (self.leads.parent / "acme" / "outreach.md").read_text(encoding="utf-8")
        self.assertIn("inferred - verify before sending", md)

    def test_valid_records_are_saved_even_when_another_is_rejected(self):
        out = self.save([
            {"company": "Acme", "people": [self.person()]},
            {"company": "Beta", "people": [self.person(source_url="")]},
        ], expect=1)
        self.assertEqual([s["company"] for s in out["saved"]], ["Acme"])
        self.assertIn("acme", json.loads(self.leads.read_text(encoding="utf-8"))["companies"])

    def test_resave_keeps_contact_history_and_earlier_people(self):
        self.save([{"company": "Acme", "people": [self.person(), self.person(name="Ben Cho")]}])
        self.run_tool("contacted", "--company", "acme", "--person", "asha rao", "--date", "2026-09-20")
        self.save([{"company": "Acme", "people": [self.person(title="Head of Platform")]}])
        people = {p["name"]: p for p in json.loads(self.leads.read_text(encoding="utf-8"))["companies"]["acme"]["people"]}
        self.assertEqual(people["Asha Rao"]["contacted"], "2026-09-20")
        self.assertEqual(people["Asha Rao"]["title"], "Head of Platform")
        self.assertIn("Ben Cho", people)

    def test_dry_run_writes_nothing(self):
        path = self.tmp / "in.json"
        path.write_text(json.dumps([{"company": "Acme", "people": [self.person()]}]), encoding="utf-8")
        out = self.run_tool("save", "--input", path, "--dry-run")
        self.assertFalse(out["written"])
        self.assertFalse(self.leads.exists())

    def test_company_with_no_people_is_not_saved(self):
        out = self.save([{"company": "Acme", "people": []}], expect=1)
        self.assertEqual(out["saved"], [])

    # ------------------------------------------------------ contacted / list

    def test_contacted_sets_follow_up_six_days_out_and_list_due_uses_it(self):
        self.save([{"company": "Acme", "people": [self.person()]}])
        out = self.run_tool("contacted", "--company", "Acme", "--person", "Asha Rao", "--date", "2026-09-20")
        self.assertEqual(out["follow_up_due"], "2026-09-26")
        self.assertEqual(self.run_tool("list", "--due")["count"], 1)
        early = subprocess.run(
            [sys.executable, str(TOOL), "list", "--due", "--leads", str(self.leads), "--today", "2026-09-25"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(early.stdout)["count"], 0)

    def test_contacted_unknown_person_is_an_error(self):
        self.save([{"company": "Acme", "people": [self.person()]}])
        proc = subprocess.run(
            [sys.executable, str(TOOL), "contacted", "--company", "Acme", "--person", "Nobody", "--leads", str(self.leads)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)

    def test_list_without_leads_file_is_empty(self):
        self.assertEqual(self.run_tool("list")["count"], 0)

    # ---------------------------------------------------------------- slugs

    def test_company_slug_matches_job_key_company_part(self):
        from job_key import make_key

        for name in ("Acme Corp", "Żabka", "腾讯", "Яндекс", ""):
            self.assertEqual(leads_state.company_slug(name), make_key(name, "Engineer").split("_")[0])

    def test_atomic_write_leaves_no_temp_files(self):
        self.save([{"company": "Acme", "people": [self.person()]}])
        self.assertEqual([p.name for p in self.leads.parent.glob(".leads.*")], [])


if __name__ == "__main__":
    unittest.main()
