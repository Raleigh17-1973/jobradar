"""Fetches each job's full description, so scoring reads what the job actually requires.

Sources, best first:
  1. the employer's own job board (stored by `resolve`: Greenhouse, Lever, Ashby, SmartRecruiters, Gem)
  2. the employer posting URL (apply_url): Workday's public data endpoint, else the schema.org JobPosting
     data most career sites embed, else the page's visible text
  3. LinkedIn's public job page (slow: LinkedIn rate-limits it; shared with the closed-job check in liveness.py)
Anything still missing is found by Claude (the Indeed connector, or a web search for the employer posting)
and saved with `./jr save-postings`, after which `./jr describe` fetches the new URLs.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from bs4 import BeautifulSoup

from . import liveness
from .store import now, rescore_when_described

USER_AGENT = liveness.USER_AGENT
MIN_DESCRIPTION = 500          # shorter than this isn't a real description
MAX_DESCRIPTION = 15000
# Every job that isn't applied, skipped by you, closed or expired needs a description, whatever its
# verdict: a Skip given before the description arrived is only a title-based guess.
OPEN = "status IN ('candidate', 'scored', 'queued', 'maybe')"
_WORKDAY = re.compile(r"https?://([^./]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/(?:job|details)/([^?#]+)")


_REQUIREMENTS = re.compile(r"\n[^\n]{0,40}(requirements|qualifications|what you.ll need|what you bring|what we.re looking for|"
                           r"must have|basic|minimum|required|you have|about you|skills and experience)[^\n]{0,40}\n", re.I)


def excerpt(text: str, limit: int) -> str:
    """Trim a long description for scoring without losing the requirements section."""
    if len(text) <= limit:
        return text
    m = _REQUIREMENTS.search(text, limit // 4)
    if not m or m.start() < limit // 2:
        return text[:limit]
    head = limit // 3
    return text[:head] + "\n[…]\n" + text[m.start():m.start() + limit - head]


def _text(markup: str) -> str:
    soup = BeautifulSoup(htmllib.unescape(markup or ""), "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    return re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n")).strip()


def _get(url: str, accept: str = "text/html") -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _days_from_text(text: str) -> Optional[int]:
    if re.search(r"today|just posted", text or "", re.I):
        return 0
    if re.search(r"yesterday", text or "", re.I):
        return 1
    return liveness._relative_days((text or "").replace("Posted ", "") + ("" if "ago" in (text or "") else " ago"))


def from_workday(url: str) -> Optional[dict]:
    m = _WORKDAY.match(url)
    if not m:
        return None
    tenant, pod, site, path = m.groups()
    body = _get(f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}", "application/json")
    if body is None:
        return {"open": False, "note": "Workday posting not found (likely closed)"}
    try:
        info = json.loads(body).get("jobPostingInfo") or {}
    except ValueError:
        return None
    days = _days_from_text(info.get("postedOn", ""))
    return {"text": _text(info.get("jobDescription", "")), "open": info.get("canApply", True),
            "posted_days": days, "source": "workday"}


def from_jsonld(page: str) -> Optional[dict]:
    """schema.org JobPosting, which most career sites embed for search engines."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and "JobPosting" in str(item.get("@type", "")):
                posted = item.get("datePosted")
                out = {"text": _text(item.get("description", "")), "source": "employer page"}
                if posted:
                    try:
                        d = datetime.fromisoformat(str(posted)[:10]).replace(tzinfo=timezone.utc)
                        out["posted_days"] = (datetime.now(timezone.utc) - d).days
                    except ValueError:
                        pass
                valid = item.get("validThrough")
                if valid and str(valid)[:10] < datetime.now(timezone.utc).date().isoformat():
                    out["open"] = False
                    out["note"] = f"posting expired {str(valid)[:10]}"
                return out
    return None


def from_page(url: str) -> Optional[dict]:
    page = _get(url)
    if not page:
        return None
    found = from_jsonld(page)
    if found and len(found.get("text", "")) >= MIN_DESCRIPTION:
        return found
    text = _text(page)
    if len(text) >= 1500 and re.search(r"responsibilit|qualification|requirement|what you.ll do", text, re.I):
        return {"text": text, "source": "employer page"}
    if re.search(r"no longer (accepting|available)|position (has been )?filled|job (is )?closed", text, re.I):
        return {"open": False, "note": "employer page says the job is closed"}
    return None


def fetch_url(url: str) -> Optional[dict]:
    return from_workday(url) if _WORKDAY.match(url or "") else from_page(url)


def store(conn: sqlite3.Connection, job_id: int, found: dict) -> str:
    """Save a fetched description and what it says about age and open/closed. Returns an outcome label."""
    row = conn.execute("SELECT status, signals, posted_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    signals = json.loads(row["signals"] or "{}")
    outcome = "no text"
    text = (found.get("text") or "")[:MAX_DESCRIPTION]
    if len(text) >= MIN_DESCRIPTION:
        conn.execute("UPDATE jobs SET description = ?, description_source = ? WHERE id = ?",
                     (text, found.get("source", "employer page"), job_id))
        rescore_when_described(conn, job_id)
        outcome = "described"
    if found.get("posted_days") is not None and not row["posted_at"]:
        posted = datetime.now(timezone.utc) - timedelta(days=found["posted_days"])
        conn.execute("UPDATE jobs SET posted_at = ? WHERE id = ?", (posted.isoformat(timespec="seconds"), job_id))
    if found.get("open") is False:
        signals["employer_closed"] = found.get("note") or "employer posting is closed"
        if row["status"] in liveness.DROPPABLE:
            conn.execute("UPDATE jobs SET status = 'closed', filter_reason = ? WHERE id = ?", (signals["employer_closed"], job_id))
        outcome = "closed"
    conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job_id))
    conn.commit()
    return outcome


def run(conn: sqlite3.Connection) -> Dict[str, int]:
    """Fetch descriptions from employer URLs for live jobs that don't have one."""
    counts = {"described": 0, "closed": 0, "no text": 0, "unreachable": 0}
    # Jobs scored on title alone whose description has since arrived go back for scoring.
    for r in conn.execute("SELECT id FROM jobs WHERE status = 'scored' AND brief LIKE '%scored on title only%' "
                          "AND length(COALESCE(description, '')) >= ?", (MIN_DESCRIPTION,)).fetchall():
        rescore_when_described(conn, r["id"])
    conn.commit()
    rows = conn.execute(f"SELECT id, apply_url FROM jobs WHERE {OPEN} AND apply_url IS NOT NULL "
                        "AND length(COALESCE(description, '')) < ?", (MIN_DESCRIPTION,)).fetchall()
    for r in rows:
        found = fetch_url(r["apply_url"])
        if not found:
            counts["unreachable"] += 1
            continue
        counts[store(conn, r["id"], found)] += 1
    return counts


def missing(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {OPEN} AND length(COALESCE(description, '')) < ?",
                        (MIN_DESCRIPTION,)).fetchone()[0]
