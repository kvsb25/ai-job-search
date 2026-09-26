#!/usr/bin/env python3
"""State helper for /leads: pick companies to research and store the leads found.

/leads turns the ranked shortlist into named contacts and outreach drafts (cold
email plus a LinkedIn connection note and DM). The model does the research and
the writing; this keeps the state files out of the conversation and enforces the
few rules that must not depend on the model counting correctly.

Subcommands:

  candidates   ranked companies worth reaching out to, with the roles that made
               them qualify, ready-made LinkedIn people-search URLs (for the
               USER to open - nothing here fetches LinkedIn), and any leads
               already stored
  save         validate lead records from the research agents, write them to
               documents/leads/leads.json and render one outreach.md per company
  contacted    record that a message went out, which starts the follow-up clock
  list         stored leads; --due shows only follow-ups that are due

Selection follows /rank's outcome: entries with status `ranked`, a verdict in
`--verdicts`, no location/language veto, and at least one role not already in
the tracker. Company matching uses rank_state.norm so non-Latin names keep their
identity; folder names reuse job_key's slug and hash fallback.

`save` rejects (never truncates) a LinkedIn connection note over 200 characters,
counted in Unicode code points, and any person without a source URL: a contact
nobody can trace to a public page is not a lead.

Usage:
  python3 tools/leads_state.py candidates [--verdicts "Strong Fit,Good Fit"]
                                          [--company NAME ...] [--refresh] [--limit N]
  python3 tools/leads_state.py save --input leads.json [--dry-run]
  python3 tools/leads_state.py contacted --company NAME --person NAME [--date YYYY-MM-DD]
  python3 tools/leads_state.py list [--due]

All subcommands print JSON on stdout. Exit 0 on success, 1 on a usage or state
error, or on `save` when any record was rejected.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))

from job_key import COMPANY_MAX, HASH_LEN, _cap, slugify  # noqa: E402
from rank_state import STATE, TRACKER, load_state, norm, tracker_pairs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEADS_DIR = ROOT / "documents" / "leads"
LEADS_FILE = LEADS_DIR / "leads.json"

DEFAULT_LIMIT = 5
DEFAULT_VERDICTS = ("Strong Fit", "Good Fit")
NOTE_MAX = 200  # LinkedIn connection note, code points
FOLLOW_UP_DAYS = 6  # 10-outreach.md: one nudge, 5-7 days after the first message
EMAIL_CONFIDENCE = ("published", "inferred", "none")


def company_slug(company: str) -> str:
    """Folder-safe company identity, the same rule job_key.make_key applies."""
    slug = _cap(slugify(company), COMPANY_MAX)
    if slug:
        return slug
    name = norm(company)
    if not name:
        return "unknown-company"
    return "company-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:HASH_LEN]


def people_search_urls(company: str, roles: list[str]) -> list[dict]:
    """LinkedIn people-search links for the user to open. Never fetched here."""
    base = "https://www.linkedin.com/search/results/people/?keywords="
    urls = [{"label": "recruiter", "url": base + quote_plus(f'"{company}" recruiter')}]
    for title in roles[:2]:
        urls.append({"label": title, "url": base + quote_plus(f'"{company}" {title}')})
    return urls


def load_leads(path: Path) -> dict:
    if not path.is_file():
        return {"companies": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("companies"), dict):
        sys.exit(f"{path}: expected an object with a 'companies' map")
    return doc


def atomic_write(path: Path, text: str) -> None:
    """Atomic replace: a half-written leads.json loses every stored contact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".leads.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_leads(path: Path, doc: dict) -> None:
    atomic_write(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def dump(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- candidates


def cmd_candidates(args) -> int:
    _, seen = load_state(args.state)
    stored = load_leads(args.leads)["companies"]
    applied = tracker_pairs(args.tracker)
    verdicts = {v.strip() for v in args.verdicts.split(",") if v.strip()}
    wanted = {norm(c) for c in args.company or [] if norm(c)}

    groups: dict[str, dict] = {}
    for key, entry in seen.items():
        cnorm = norm(entry.get("company"))
        if not cnorm:
            continue
        if wanted:
            if cnorm not in wanted:
                continue
        else:
            if entry.get("status") != "ranked" or entry.get("rank_verdict") not in verdicts:
                continue
            if entry.get("location_verdict") == "FAIL" or entry.get("language_gate") == "FAIL":
                continue
        if (cnorm, norm(entry.get("title"))) in applied:
            continue
        group = groups.setdefault(cnorm, {"company": entry.get("company"), "roles": []})
        group["roles"].append(
            {
                "key": key,
                "title": entry.get("title"),
                "url": entry.get("url"),
                "score": entry.get("rank_score"),
                "verdict": entry.get("rank_verdict"),
                "strengths": entry.get("strengths", []),
                "gaps": entry.get("gaps", []),
            }
        )

    unmatched = sorted(c for c in args.company or [] if norm(c) not in groups) if wanted else []
    for name in unmatched:  # named by the user but absent from the scrape: still researchable
        groups[norm(name)] = {"company": name, "roles": []}

    rows, already = [], 0
    for group in groups.values():
        group["roles"].sort(key=lambda r: r["score"] if isinstance(r["score"], (int, float)) else -1, reverse=True)
        slug = company_slug(group["company"])
        existing = stored.get(slug)
        if existing and not args.refresh:
            already += 1
            continue
        titles = [r["title"] for r in group["roles"] if r.get("title")]
        best = group["roles"][0]["score"] if group["roles"] else None
        rows.append(
            {
                "company": group["company"],
                "slug": slug,
                "best_score": best,
                "roles": group["roles"],
                "linkedin_people_search": people_search_urls(group["company"], titles),
                "existing_people": (existing or {}).get("people", []),
            }
        )

    rows.sort(key=lambda r: r["best_score"] if isinstance(r["best_score"], (int, float)) else -1, reverse=True)
    eligible = len(rows)
    if args.limit > 0:
        rows = rows[: args.limit]
    dump(
        {
            "eligible": eligible,
            "selected": rows,
            "deferred": max(0, eligible - len(rows)),
            "already_researched": already,
            "unmatched_names": unmatched,
            "total_entries": len(seen),
        }
    )
    return 0


# ---------------------------------------------------------------------- save


def validate_person(person) -> tuple[dict | None, str | None]:
    if not isinstance(person, dict):
        return None, "person must be an object"
    name = str(person.get("name") or "").strip()
    title = str(person.get("title") or "").strip()
    source = str(person.get("source_url") or "").strip()
    if not name:
        return None, "person without a name"
    if not title:
        return None, f"{name}: missing title"
    if not source.lower().startswith(("http://", "https://")):
        return None, f"{name}: source_url must be an http(s) public page"
    confidence = person.get("email_confidence") or "none"
    if confidence not in EMAIL_CONFIDENCE:
        return None, f"{name}: email_confidence must be one of {', '.join(EMAIL_CONFIDENCE)}"
    email = str(person.get("email") or "").strip()
    if confidence != "none" and "@" not in email:
        return None, f"{name}: email_confidence '{confidence}' needs an email address"
    if confidence == "none":
        email = ""

    cleaned = {
        "name": name,
        "title": title,
        "source_url": source,
        "why": str(person.get("why") or "").strip(),
        "email": email,
        "email_confidence": confidence,
        "contacted": None,
        "follow_up_due": None,
    }

    draft = person.get("draft")
    if draft is not None:
        if not isinstance(draft, dict):
            return None, f"{name}: draft must be an object"
        note = str(draft.get("connection_note") or "")
        if len(note) > NOTE_MAX:
            return None, f"{name}: connection note is {len(note)} characters, limit is {NOTE_MAX}"
        cleaned["draft"] = {
            "email_subject": str(draft.get("email_subject") or "").strip(),
            "email_body": str(draft.get("email_body") or "").strip(),
            "connection_note": note.strip(),
            "dm": str(draft.get("dm") or "").strip(),
        }
    return cleaned, None


def render_outreach(record: dict) -> str:
    lines = [f"# Outreach: {record['company']}", "", f"_Updated {record['updated']}. Drafts only - nothing here has been sent._", ""]
    if record.get("roles"):
        lines += ["## Roles tracked", ""]
        lines += [f"- {r.get('title')} ({r.get('verdict')}, score {r.get('score')})" for r in record["roles"]]
        lines.append("")
    for person in record["people"]:
        lines += [f"## {person['name']} - {person['title']}", ""]
        lines.append(f"- Source: {person['source_url']}")
        if person.get("why"):
            lines.append(f"- Why this person: {person['why']}")
        if person["email"]:
            lines.append(f"- Email: {person['email']} ({person['email_confidence']}"
                         + (" - verify before sending)" if person["email_confidence"] == "inferred" else ")"))
        else:
            lines.append("- Email: none found")
        if person.get("contacted"):
            lines.append(f"- Contacted: {person['contacted']} (follow-up due {person['follow_up_due']})")
        lines.append("")
        draft = person.get("draft")
        if draft:
            if draft["email_body"]:
                lines += ["### Cold email", "", f"**Subject:** {draft['email_subject']}", "", draft["email_body"], ""]
            if draft["connection_note"]:
                lines += [f"### LinkedIn connection note ({len(draft['connection_note'])}/{NOTE_MAX})", "", draft["connection_note"], ""]
            if draft["dm"]:
                lines += ["### LinkedIn follow-up DM", "", draft["dm"], ""]
    if record.get("linkedin_people_search"):
        lines += ["## LinkedIn people search (open manually)", ""]
        lines += [f"- {u['label']}: {u['url']}" for u in record["linkedin_people_search"]]
        lines.append("")
    return "\n".join(lines)


def cmd_save(args) -> int:
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"cannot read input file {args.input}: {exc}")
    if isinstance(payload, dict):
        payload = payload.get("companies", [])
    if not isinstance(payload, list):
        sys.exit("input must be a JSON array of company objects")

    doc = load_leads(args.leads)
    companies = doc["companies"]
    saved, errors = [], []
    for item in payload:
        company = str((item or {}).get("company") or "").strip() if isinstance(item, dict) else ""
        if not company:
            errors.append({"company": None, "error": "record without a company"})
            continue
        slug = company_slug(company)
        previous = companies.get(slug, {})
        people, problems = [], []
        for person in item.get("people") or []:
            cleaned, problem = validate_person(person)
            if problem:
                problems.append(problem)
                continue
            people.append(cleaned)
        for problem in problems:
            errors.append({"company": company, "error": problem})
        if not people:
            if not problems:
                errors.append({"company": company, "error": "no people given - nobody credible found is a valid finding, but nothing to save"})
            continue

        # Merge with what is stored: same person keeps contact history.
        old_by_name = {norm(p.get("name")): p for p in previous.get("people", [])}
        for person in people:
            old = old_by_name.pop(norm(person["name"]), None)
            if old:
                person["contacted"] = old.get("contacted")
                person["follow_up_due"] = old.get("follow_up_due")
                if "draft" not in person and old.get("draft"):
                    person["draft"] = old["draft"]
        people += list(old_by_name.values())  # earlier people not re-found stay

        record = {
            "company": company,
            "updated": args.today.isoformat(),
            "roles": item.get("roles") or previous.get("roles", []),
            "linkedin_people_search": item.get("linkedin_people_search") or previous.get("linkedin_people_search", []),
            "people": people,
        }
        companies[slug] = record
        out = args.leads.parent / slug / "outreach.md"
        saved.append({"company": company, "slug": slug, "people": len(record["people"]), "path": str(out)})
        if not args.dry_run:
            atomic_write(out, render_outreach(record))

    if not args.dry_run and saved:
        save_leads(args.leads, doc)
    dump({"saved": saved, "errors": errors, "written": bool(saved and not args.dry_run)})
    return 1 if errors else 0


# ----------------------------------------------------------------- contacted


def find_company(companies: dict, name: str) -> tuple[str, dict] | None:
    target = norm(name)
    for slug, record in companies.items():
        if slug == name or norm(record.get("company")) == target:
            return slug, record
    return None


def cmd_contacted(args) -> int:
    doc = load_leads(args.leads)
    found = find_company(doc["companies"], args.company)
    if not found:
        sys.exit(f"no stored leads for '{args.company}' - run /leads first")
    slug, record = found
    target = norm(args.person)
    person = next((p for p in record["people"] if norm(p.get("name")) == target), None)
    if not person:
        sys.exit(f"no stored person '{args.person}' at {record['company']}")
    person["contacted"] = args.date.isoformat()
    person["follow_up_due"] = (args.date + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    save_leads(args.leads, doc)
    atomic_write(args.leads.parent / slug / "outreach.md", render_outreach(record))
    dump({"company": record["company"], "person": person["name"],
          "contacted": person["contacted"], "follow_up_due": person["follow_up_due"]})
    return 0


# ---------------------------------------------------------------------- list


def cmd_list(args) -> int:
    companies = load_leads(args.leads)["companies"]
    rows = []
    for slug, record in companies.items():
        for person in record.get("people", []):
            due = person.get("follow_up_due")
            is_due = bool(due and due <= args.today.isoformat())
            if args.due and not is_due:
                continue
            rows.append(
                {
                    "company": record["company"],
                    "slug": slug,
                    "name": person["name"],
                    "title": person["title"],
                    "email_confidence": person.get("email_confidence"),
                    "contacted": person.get("contacted"),
                    "follow_up_due": due,
                    "follow_up_overdue": is_due,
                }
            )
    dump({"count": len(rows), "people": rows})
    return 0


def _force_utf8_output() -> None:
    """Write UTF-8 whatever the host's default encoding is (see rank_state.py)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # absent on a StringIO under test
        if reconfigure:
            reconfigure(encoding="utf-8")


def main() -> int:
    _force_utf8_output()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--leads", type=Path, default=LEADS_FILE)
    common.add_argument("--today", type=date.fromisoformat, default=date.today())

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    cand = sub.add_parser("candidates", parents=[common], help="companies to research")
    cand.add_argument("--state", type=Path, default=STATE)
    cand.add_argument("--tracker", type=Path, default=TRACKER)
    cand.add_argument("--verdicts", default=",".join(DEFAULT_VERDICTS), help="comma-separated rank_verdict values")
    cand.add_argument("--company", action="append", help="research this company regardless of rank (repeatable)")
    cand.add_argument("--refresh", action="store_true", help="include companies already researched")
    cand.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="0 for no cap")
    cand.set_defaults(func=cmd_candidates)

    save = sub.add_parser("save", parents=[common], help="validate and store lead records")
    save.add_argument("--input", required=True, help="JSON array from the research agents")
    save.add_argument("--dry-run", action="store_true")
    save.set_defaults(func=cmd_save)

    mark = sub.add_parser("contacted", parents=[common], help="record a sent message, start the follow-up clock")
    mark.add_argument("--company", required=True)
    mark.add_argument("--person", required=True)
    mark.add_argument("--date", type=date.fromisoformat, default=date.today())
    mark.set_defaults(func=cmd_contacted)

    lst = sub.add_parser("list", parents=[common], help="stored leads")
    lst.add_argument("--due", action="store_true", help="only follow-ups due today or earlier")
    lst.set_defaults(func=cmd_list)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
