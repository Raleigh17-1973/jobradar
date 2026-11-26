"""Your company watchlist: startups and other targets read straight from their own job boards.

  ./jr watch                 read every board in config/watchlist.yaml (also part of `./jr run` once enabled)
  ./jr watch-find NAME...    find a company's board on Greenhouse, Lever, Ashby, SmartRecruiters, Gem or Workable

Startups usually post on their own Ashby, Greenhouse or Lever board first, and many of those jobs never reach
LinkedIn alerts. Only titles that pass your title rules are stored, so a 300-job engineering board adds nothing
but its program, PMO and Chief of Staff roles. Postings come with the full description, pay and post date, so
they go straight to scoring.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import ats, config, filters
from .parsers import ParsedJob
from .store import _norm, now, record_email, upsert_job

SOURCE = "board"            # sightings.source for jobs found on a watched board
EMAIL_SOURCE = "watchlist"  # emails.source for the pseudo-email each run records
# Where a posting can be: your home area (preferences.yaml `home_area.places`), or remote in the US. Anything else
# waits for the remote check. A bare country ("United States", "USA") counts as remote; "Salt Lake City, Utah,
# United States" doesn't.
IN_AREA = config.area_pattern(list(config.home_area().get("places") or []) + ["remote", "anywhere"])
WHOLE_COUNTRY = re.compile(r"^\s*(united states( of america)?|u\.?s\.?a?\.?|north america|americas|amer)\s*$", re.I)
ABROAD = re.compile(r"canada|toronto|vancouver|united kingdom|\bUK\b|london|europe|emea|germany|berlin|france|paris|"
                    r"ireland|dublin|netherlands|amsterdam|spain|india|bangalore|bengaluru|singapore|japan|tokyo|"
                    r"australia|sydney|brazil|mexico|israel|tel aviv|poland|apac|latam", re.I)


def _job_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def location_ok(location: str) -> bool:
    """Your home area or US remote. 'Remote - Canada' and 'London; Remote' don't count."""
    loc = location or ""
    if not loc.strip():
        return True        # unlisted: let the remote check read the posting
    parts = [p for p in re.split(r"[;|/]| or ", loc) if p.strip()]
    return any((IN_AREA.search(p) or WHOLE_COUNTRY.match(p)) and not ABROAD.search(p) for p in parts)


def due(conn: sqlite3.Connection, every_hours: float) -> bool:
    """Read the boards at most every few hours; a quarter-hour of slack keeps hourly runs from slipping an hour."""
    row = conn.execute("SELECT MAX(received_at) FROM emails WHERE source = ?", (EMAIL_SOURCE,)).fetchone()
    if not row or not row[0]:
        return True
    hours = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
    return hours >= every_hours - 0.25


def _title_passes(rules: filters.Rules, title: str, company: str) -> bool:
    reason = rules.reason_to_drop({"title": title, "company": company, "signals": "{}", "salary_max": None})
    return reason is None


def run(conn: sqlite3.Connection, cfg: dict, prefs: dict, max_age_days: int = 14) -> Dict[str, int]:
    """Read every watched board; store fresh postings whose titles fit, then run the free filters."""
    companies = [c for c in cfg.get("companies", []) if c.get("ats") in ats.FETCHERS and c.get("slug")]
    rules = filters.Rules(prefs)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    def fetch(c):
        # Big boards (OpenAI lists 800+ jobs) sometimes time out once; one retry is enough.
        return c, ats.FETCHERS[c["ats"]](c["slug"]) or ats.FETCHERS[c["ats"]](c["slug"])

    with ThreadPoolExecutor(max_workers=16) as pool:
        boards = list(pool.map(fetch, companies))
    msgid = f"watchlist:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}"
    counts = {"boards": len(companies), "unreachable": 0, "postings": 0, "fit": 0, "too_old": 0, "new": 0}
    unreachable: List[str] = []
    found = []
    for c, postings in boards:
        if postings is None:
            counts["unreachable"] += 1
            unreachable.append(c["name"])
            continue
        counts["postings"] += len(postings)
        for p in postings:
            if not _title_passes(rules, p.title, c["name"]):
                continue
            posted = _parse_time(p.posted_at)
            if posted and posted < cutoff:
                counts["too_old"] += 1
                continue
            counts["fit"] += 1
            found.append((c, p))
    record_email(conn, msgid, EMAIL_SOURCE, "Company watchlist", f"{len(companies)} boards", now(), len(found))
    for c, p in found:
        if p.ats == "smartrecruiters":
            p = ats._complete(p)
        signals = {"watchlist": c.get("group") or True}
        if c.get("conflict"):
            signals["conflict_of_interest"] = config.conflict_note()
        if p.location and not location_ok(p.location):
            signals["outside_search_area"] = [p.location]
        job = ParsedJob(source=SOURCE, source_job_id=_job_key(p.url), title=p.title.strip(), company=c["name"],
                        location=p.location or None, url=p.url, salary_min=p.salary_min, salary_max=p.salary_max,
                        signals=signals)
        job_id, created = upsert_job(conn, job, msgid)
        counts["new"] += created
        # The board is the employer's own posting: its link, text and date beat anything from an alert.
        row = conn.execute("SELECT apply_url, description, posted_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.execute("UPDATE jobs SET ats = ?, apply_url = COALESCE(apply_url, ?), ats_checked_at = COALESCE(ats_checked_at, ?) "
                     "WHERE id = ?", (p.ats, p.url, now(), job_id))
        if len(p.description or "") > len(row["description"] or ""):
            conn.execute("UPDATE jobs SET description = ?, description_source = ? WHERE id = ?",
                         (p.description[:15000], p.ats, job_id))
        if p.posted_at and not row["posted_at"]:
            conn.execute("UPDATE jobs SET posted_at = ? WHERE id = ?", (p.posted_at, job_id))
    conn.commit()
    filters.run(conn, prefs)
    counts["unreachable_names"] = unreachable
    return counts


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def slug_candidates(name: str) -> List[str]:
    """Board names companies commonly use: 'Scale AI' -> scaleai, scale-ai, scale; 'Kin' -> kin, kinai, kinhq..."""
    base = ats.slug_guesses(name)
    words = _norm(name).split()
    stem = "".join(words)
    extra = [stem + s for s in ("ai", "hq", "inc", "labs")] if stem else []
    return list(dict.fromkeys(base + extra))


def find_boards(names: List[str], hints: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[Tuple[str, str, int, List[str]]]]:
    """For each company name: every (ats, slug, posting count, sample titles) that answers."""
    hints = hints or {}

    def names_for(n: str, system: str) -> List[str]:
        slugs = hints.get(n, []) + slug_candidates(n)
        return slugs[:3] if system == "workable" else slugs     # Workable throttles (see ats.py): fewer guesses

    tries = [(n, a, s) for n in names for a in ats.FETCHERS for s in names_for(n, a)]

    def fetch(t):
        n, a, s = t
        return n, a, s, ats.FETCHERS[a](s)

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(fetch, tries))
    out: Dict[str, list] = {n: [] for n in names}
    for n, a, s, postings in results:
        if postings:
            out[n].append((a, s, len(postings), [p.title for p in postings[:3]]))
    return out
