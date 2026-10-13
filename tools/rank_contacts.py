#!/usr/bin/env python3
"""Rank LinkedIn contacts at a target company for /network.

Reads one JSON file holding the target and the raw `searchPeople` results
collected through api-anything, optionally merges the user's own LinkedIn
data export (Connections.csv, all first-degree), and prints the people ranked
by how useful they are for this application: already connected, recruiter or
likely hiring manager, same field, same location.

Input JSON:
    {
      "company": "Siemens Energy",
      "aliases": ["siemens-energy"],          # optional extra spellings
      "role_keywords": ["power electronics", "converter"],
      "location": "Munich",                    # optional
      "results": [
        {"tag": "recruiter", "data": [...]},   # tag: recruiter|manager|peer|alumni
        ...
      ]
    }

Each `data` is what `api-anything call linkedin searchPeople` returns: a list
of records, or `{columns, rows}` when it came over MCP.

No network access. Usage:
    python3 tools/rank_contacts.py <input.json> [--connections Connections.csv]
        [--format json|md] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

RECRUITER_TERMS = (
    "recruit", "talent acquisition", "talent partner", "sourcer", "sourcing",
    "hr business partner", "people partner", "human resources", "personalreferent",
    "personalmarketing", "personalgewinnung", "employer branding",
)
MANAGER_TERMS = (
    "head of", "lead", "manager", "director", "vp ", "vice president", "chief",
    "principal", "leiter", "leitung", "teamleiter", "gruppenleiter",
    "abteilungsleiter", "bereichsleiter", "chef",
)
DISTANCE_POINTS = {"DISTANCE_1": 30, "DISTANCE_2": 18, "DISTANCE_3": 5}
DISTANCE_LABEL = {
    "DISTANCE_1": "1st", "DISTANCE_2": "2nd", "DISTANCE_3": "3rd",
}


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def _contains(haystack: str, needle: str) -> bool:
    needle = _norm(needle)
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def _records(data) -> list[dict]:
    """Accept a list of records or the MCP {columns, rows} shape."""
    if isinstance(data, dict) and "columns" in data and "rows" in data:
        cols = data["columns"]
        return [dict(zip(cols, row)) for row in data["rows"]]
    if isinstance(data, dict) and "data" in data:
        return _records(data["data"])
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _slug(url: str | None) -> str | None:
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1).casefold() if m else None


def load_connections(path: Path) -> list[dict]:
    """Parse LinkedIn's Connections.csv export (skips its 'Notes:' preamble)."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("First Name,")), None)
    if start is None:
        raise ValueError(f"{path}: no 'First Name,...' header row; is this LinkedIn's Connections.csv?")
    people = []
    for row in csv.DictReader(lines[start:]):
        name = f"{row.get('First Name', '').strip()} {row.get('Last Name', '').strip()}".strip()
        if not name:
            continue
        company = (row.get("Company") or "").strip()
        position = (row.get("Position") or "").strip()
        url = (row.get("URL") or "").strip()
        people.append({
            "name": name,
            "headline": f"{position} at {company}" if company else position,
            "location": "",
            "url": url,
            "publicId": _slug(url),
            "distance": "DISTANCE_1",
            "_tags": {"connections-export"},
        })
    return people


def classify(headline: str) -> str:
    if any(t in headline for t in RECRUITER_TERMS):
        return "recruiter"
    if any(_contains(headline, t.strip()) for t in MANAGER_TERMS):
        return "manager"
    return "peer"


def score(person: dict, target: dict) -> dict:
    headline = _norm(person.get("headline"))
    names = [target["company"], *target.get("aliases", [])]
    at_company = any(_contains(headline, n) for n in names)
    distance = person.get("distance") or ""
    kind = classify(headline)
    hits = [k for k in target.get("role_keywords", []) if _contains(headline, k)]
    loc = _norm(target.get("location"))
    same_place = bool(loc) and loc in _norm(person.get("location"))

    points, reasons = 0, []
    if at_company:
        points += 35
        reasons.append("headline names the company")
    else:
        reasons.append("company NOT in headline - may have left or never worked there")
    if distance in DISTANCE_POINTS:
        points += DISTANCE_POINTS[distance]
        reasons.append(f"{DISTANCE_LABEL[distance]}-degree")
    if kind == "recruiter":
        points += 20
        reasons.append("recruiting/HR")
    elif kind == "manager":
        points += 15 if hits else 6
        reasons.append("lead/manager" + (" in your field" if hits else ", field unclear"))
    else:
        points += 8 if hits else 0
    if hits:
        points += min(18, 6 * len(hits))
        reasons.append("matches: " + ", ".join(hits))
    if same_place:
        points += 8
        reasons.append("same location")
    if "alumni" in person["_tags"] and at_company:
        points += 7
        reasons.append("matched school search - confirm alumni on profile")

    if distance == "DISTANCE_1":
        approach = "Ask directly for a referral or an honest view of the team"
    elif distance == "DISTANCE_2":
        approach = "Ask a mutual connection for an intro (see 'mutual connections' on the profile)"
    elif kind == "recruiter":
        approach = "Short connection note naming the exact role"
    else:
        approach = "Connection note with one specific question about the team; no referral ask yet"

    return {
        "name": person.get("name"),
        "headline": person.get("headline"),
        "location": person.get("location") or "",
        "degree": DISTANCE_LABEL.get(distance, "out of network" if distance else "unknown"),
        "type": kind,
        "score": points,
        "at_company": at_company,
        "reasons": reasons,
        "approach": approach,
        "url": (person.get("url") or "").split("?")[0],
        "publicId": person.get("publicId"),
        "found_by": sorted(person["_tags"]),
    }


def rank(target: dict, extra: list[dict] | None = None) -> list[dict]:
    people: dict[str, dict] = {}

    def add(p: dict) -> None:
        key = (p.get("publicId") or _slug(p.get("url")) or _norm(p.get("name"))).casefold()
        if not key:
            return
        if key in people:
            seen = people[key]
            seen["_tags"] |= p["_tags"]
            # A connections-export row proves 1st degree; never downgrade it.
            if p.get("distance") == "DISTANCE_1":
                seen["distance"] = "DISTANCE_1"
            for field in ("headline", "location"):
                if not seen.get(field) and p.get(field):
                    seen[field] = p[field]
        else:
            people[key] = p

    for block in target.get("results", []):
        tag = block.get("tag", "search")
        for rec in _records(block.get("data")):
            add({**rec, "_tags": {tag}})
    for p in extra or []:
        add(p)

    names = [target["company"], *target.get("aliases", [])]
    ranked = []
    for p in people.values():
        if "connections-export" in p["_tags"] and p["_tags"] == {"connections-export"}:
            # Only keep export rows at the target company; the rest of the
            # user's network is irrelevant here.
            if not any(_contains(_norm(p["headline"]), n) for n in names):
                continue
        ranked.append(score(p, target))
    ranked.sort(key=lambda r: (not r["at_company"], -r["score"], r["name"] or ""))
    return ranked


def to_markdown(rows: list[dict]) -> str:
    out = ["| # | Name | Degree | Type | Score | Headline | Why | Approach |",
           "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        cells = [str(i), f"[{r['name']}]({r['url']})" if r["url"] else r["name"], r["degree"],
                 r["type"], str(r["score"]), r["headline"] or "", "; ".join(r["reasons"]), r["approach"]]
        out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("--connections", type=Path, help="LinkedIn export Connections.csv")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args(argv)

    try:
        target = json.loads(args.input.read_text(encoding="utf-8"))
        if not target.get("company"):
            raise ValueError("input JSON needs a non-empty 'company'")
        extra = load_connections(args.connections) if args.connections else None
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    rows = rank(target, extra)[: max(args.limit, 0)]
    print(to_markdown(rows) if args.format == "md" else json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
