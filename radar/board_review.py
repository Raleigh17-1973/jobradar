"""A full review of one company's board: every new posting, whatever its title or office (e.g. Anthropic, twice a day).

  ./jr board-review list NAME          new postings since the last review: number, title, team, location, posted date
  ./jr board-review show NAME N...     the full text of the postings you want to read
  ./jr board-review keep NAME FILE     add the ones that fit to Job Radar; FILE is [{"n": 3, "why": "..."}]
  ./jr board-review done NAME          mark everything listed as reviewed, so the next review shows only newer postings

NAME is a company in config/watchlist.yaml. The watchlist itself keeps only titles that pass your title rules,
in your home area or US-remote; this review is for the companies where an unusual title or an office role is still worth
a look. Kept postings skip the title and location rules and go straight to scoring.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from . import ats, config
from .parsers import ParsedJob
from .store import now, record_email, upsert_job
from .watchlist import SOURCE, _job_key, _parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_reviews (
  company     TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT,
  reviewed_at TEXT NOT NULL,
  kept        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (company, url)
);
"""
EMAIL_SOURCE = "board_review"


def company(name: str) -> dict:
    for c in config.load("watchlist").get("companies", []):
        if c["name"].lower() == name.lower():
            return c
    raise SystemExit(f"{name!r} isn't in config/watchlist.yaml.")


def _pending_path(c: dict):
    return config.project_path(f"data/board_review/{c['slug']}.json")


def _postings(c: dict) -> List[dict]:
    """Every posting with its team. Greenhouse's API names departments; the other systems go through ats.py."""
    if c["ats"] == "greenhouse":
        data = ats._get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true") or {}
        return [{"title": j["title"].strip(), "url": j["absolute_url"], "location": (j.get("location") or {}).get("name", ""),
                 "team": ", ".join(d["name"] for d in j.get("departments") or []),
                 "posted": j.get("first_published") or j.get("updated_at"), "description": ats._text(j.get("content", ""))}
                for j in data.get("jobs", [])]
    return [{"title": p.title.strip(), "url": p.url, "location": p.location, "team": "", "posted": p.posted_at,
             "description": p.description, "salary_min": p.salary_min, "salary_max": p.salary_max}
            for p in (ats.FETCHERS[c["ats"]](c["slug"]) or [])]


def list_new(conn: sqlite3.Connection, c: dict, max_age_days: int = 14) -> List[dict]:
    """Postings not yet reviewed and posted within the age limit, numbered; saved for show/keep/done."""
    conn.executescript(SCHEMA)
    seen = {r[0] for r in conn.execute("SELECT url FROM board_reviews WHERE company = ?", (c["name"],))}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    fresh = [p for p in _postings(c) if p["url"] not in seen and (_parse_time(p["posted"]) or cutoff) >= cutoff]
    fresh.sort(key=lambda p: p["posted"] or "", reverse=True)
    for n, p in enumerate(fresh, 1):
        p["n"] = n
    _pending_path(c).write_text(json.dumps(fresh, ensure_ascii=False))
    return fresh


def _load_pending(c: dict) -> Dict[int, dict]:
    path = _pending_path(c)
    if not path.exists():
        raise SystemExit(f"Run `./jr board-review list {c['name']!r}` first.")
    return {p["n"]: p for p in json.loads(path.read_text())}


def show(c: dict, numbers: List[int]) -> List[dict]:
    pending = _load_pending(c)
    return [pending[n] for n in numbers if n in pending]


def keep(conn: sqlite3.Connection, c: dict, picks: List[dict]) -> int:
    """Add the chosen postings as candidates. They skip the title and location rules (`kept_by_review`)."""
    conn.executescript(SCHEMA)
    pending = _load_pending(c)
    msgid = f"board_review:{c['slug']}:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}"
    record_email(conn, msgid, EMAIL_SOURCE, f"{c['name']} board review", c["name"], now(), len(picks))
    kept = 0
    for pick in picks:
        p = pending.get(int(pick["n"]))
        if not p:
            continue
        signals = {"kept_by_review": pick.get("why") or True, "watchlist": c.get("group") or True}
        if c.get("conflict"):
            signals["conflict_of_interest"] = config.conflict_note()
        job_id, _ = upsert_job(conn, ParsedJob(source=SOURCE, source_job_id=_job_key(p["url"]), title=p["title"],
                                               company=c["name"], location=p["location"] or None, url=p["url"],
                                               salary_min=p.get("salary_min"), salary_max=p.get("salary_max"),
                                               signals=signals), msgid)
        conn.execute("UPDATE jobs SET ats = ?, apply_url = COALESCE(apply_url, ?), posted_at = COALESCE(posted_at, ?), "
                     "ats_checked_at = COALESCE(ats_checked_at, ?) WHERE id = ?", (c["ats"], p["url"], p["posted"], now(), job_id))
        if len(p.get("description") or "") >= 500:
            conn.execute("UPDATE jobs SET description = ?, description_source = ? WHERE id = ? AND "
                         "length(COALESCE(description, '')) < ?", (p["description"][:15000], c["ats"], job_id, len(p["description"])))
        # Waiting jobs go straight to scoring; anything you've already decided on keeps its status.
        conn.execute("UPDATE jobs SET status = 'candidate', filter_reason = NULL WHERE id = ? AND status IN ('new', 'filtered')",
                     (job_id,))
        conn.execute("INSERT OR REPLACE INTO board_reviews VALUES (?, ?, ?, ?, 1)", (c["name"], p["url"], p["title"], now()))
        kept += 1
    conn.commit()
    return kept


def done(conn: sqlite3.Connection, c: dict) -> int:
    conn.executescript(SCHEMA)
    pending = _load_pending(c)
    for p in pending.values():
        conn.execute("INSERT OR IGNORE INTO board_reviews VALUES (?, ?, ?, ?, 0)", (c["name"], p["url"], p["title"], now()))
    conn.commit()
    _pending_path(c).unlink()
    return len(pending)
