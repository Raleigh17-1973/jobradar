"""People you know: your LinkedIn connections, matched to the companies in the radar.

  ./jr network-import FILE      load Connections.csv (or the LinkedIn export .zip) into `connections`
  ./jr network-match            attach the connections at each job's company to signals.network

The file comes from LinkedIn's data export (Settings > Data privacy > Download my data, larger archive).
Each import is a fresh snapshot, so it replaces the previous one. Company names are matched with the same
keys as your application history, so "Northwind AI" finds connections who list "Northwind".
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Dict, List

from .history import company_key, loose_company_key
from .store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
  url          TEXT PRIMARY KEY,
  first_name   TEXT,
  last_name    TEXT,
  email        TEXT,
  company      TEXT,
  company_key  TEXT,
  position     TEXT,
  connected_on TEXT,
  imported_at  TEXT
);
"""

# People who can route you to a hiring manager, listed first.
RECRUITING = re.compile(r"recruit|talent|sourc|people partner|human resources|\bhr\b|hiring", re.I)
PEOPLE_SHOWN = 6
# Placeholders that would wrongly pair everyone who is "self-employed" with a "Confidential" posting.
NOT_A_COMPANY = {"", "confidential", "stealth", "stealth startup", "stealth mode startup", "self employed",
                 "freelance", "independent", "retired", "unemployed", "none", "n a"}


def connect_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _read_csv_text(path: Path) -> str:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("connections.csv")), None)
            if not name:
                raise SystemExit(f"No Connections.csv inside {path.name}")
            return z.read(name).decode("utf-8-sig")
    return path.read_text(encoding="utf-8-sig")


def parse(text: str) -> List[dict]:
    """LinkedIn puts a few "Notes:" lines above the header row; skip to the row that starts with First Name."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("First Name")), None)
    if start is None:
        raise SystemExit("No 'First Name,...' header row found; is this LinkedIn's Connections.csv?")
    return [r for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))) if r.get("URL")]


def import_file(conn: sqlite3.Connection, path: Path) -> int:
    connect_schema(conn)
    rows = parse(_read_csv_text(path))
    conn.execute("DELETE FROM connections")
    stamp = now()
    for r in rows:
        company = (r.get("Company") or "").strip()
        conn.execute("INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (r["URL"].strip(), r.get("First Name", "").strip(), r.get("Last Name", "").strip(),
                      (r.get("Email Address") or "").strip() or None, company or None,
                      company_key(company) if company else None, (r.get("Position") or "").strip() or None,
                      (r.get("Connected On") or "").strip() or None, stamp))
    conn.commit()
    return len(rows)


def _person(c: sqlite3.Row) -> dict:
    return {"name": f"{c['first_name']} {c['last_name']}".strip(), "position": c["position"], "url": c["url"],
            "recruiting": bool(RECRUITING.search(c["position"] or ""))}


def personal_contacts() -> Dict[str, List[dict]]:
    """config/contacts.yaml: people you named yourself, keyed by company."""
    from . import config
    try:
        entries = (config.load("contacts") or {}).get("contacts") or []
    except FileNotFoundError:
        return {}
    out: Dict[str, List[dict]] = {}
    for c in entries:
        for company in c.get("companies") or []:
            out.setdefault(company_key(company), []).append(
                {"name": c["name"], "position": c.get("relationship"), "url": c.get("url"), "recruiting": False,
                 "personal": True, "relationship": c.get("relationship")})
    return out


def match_jobs(conn: sqlite3.Connection) -> Dict[str, int]:
    """Attach the people you know at each job's company: your own contacts, then recruiters, then by name."""
    connect_schema(conn)
    personal = personal_contacts()
    by_key: Dict[str, list] = {}
    by_loose: Dict[str, list] = {}
    for c in conn.execute("SELECT * FROM connections WHERE company_key IS NOT NULL AND company_key != '' "
                          "ORDER BY first_name, last_name"):
        by_key.setdefault(c["company_key"], []).append(c)
        by_loose.setdefault(loose_company_key(c["company_key"]), []).append(c)
    counts = {"jobs": 0, "companies": 0}
    seen_companies = set()
    for job in conn.execute("SELECT id, company, signals FROM jobs").fetchall():
        signals = json.loads(job["signals"] or "{}")
        had = signals.pop("network", None)
        key = company_key(job["company"])
        people = (by_key.get(key) or by_loose.get(loose_company_key(key), [])) if key not in NOT_A_COMPANY else []
        mine = personal.get(key) or personal.get(loose_company_key(key)) or []
        if people or mine:
            ranked = mine + sorted((_person(c) for c in people if f"{c['first_name']} {c['last_name']}".strip()
                                    not in {m["name"] for m in mine}), key=lambda p: not p["recruiting"])
            signals["network"] = {"count": len(ranked), "people": ranked[:PEOPLE_SHOWN]}
            counts["jobs"] += 1
            seen_companies.add(key)
        if people or had:
            conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job["id"]))
    conn.commit()
    counts["companies"] = len(seen_companies)
    return counts
