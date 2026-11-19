"""LinkedIn job searches, run alongside the alert emails.

Uses LinkedIn's public (logged-out) search, the same source as its public job pages, so it needs no
login but also can't apply logged-in filters such as "top applicant": Job Radar's own scoring of the
full description does that part. Searches are configured in settings.yaml under linkedin_searches.
"""
from __future__ import annotations

import html
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from .liveness import USER_AGENT
from .parsers import ParsedJob, parse_salary
from .store import now, record_email, upsert_job

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
PAGE_SIZE = 10
SOURCE = "linkedin_search"     # emails.source for the pseudo-email each search run records

_CARD_ID = re.compile(r"urn:li:jobPosting:(\d+)")
_TITLE = re.compile(r'base-search-card__title[^>]*>\s*(.*?)\s*</h3>', re.S)
_COMPANY = re.compile(r'base-search-card__subtitle[^>]*>(.*?)</h4>', re.S)
_LOCATION = re.compile(r'job-search-card__location[^>]*>\s*(.*?)\s*</span>', re.S)
_LISTED = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})"')
_SALARY = re.compile(r'job-search-card__salary-info[^>]*>\s*(.*?)\s*</span>', re.S)


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def parse_cards(page: str) -> List[ParsedJob]:
    jobs = []
    for card in page.split("<li>"):
        job_id, title = _CARD_ID.search(card), _TITLE.search(card)
        if not job_id or not title:
            continue
        company, location = _COMPANY.search(card), _LOCATION.search(card)
        listed, salary = _LISTED.search(card), _SALARY.search(card)
        signals = {"linkedin_listed": listed.group(1)} if listed else {}
        lo = hi = None
        if salary:
            signals["salary_text"] = re.sub(r"\s+", " ", _clean(salary.group(1)))
            lo, hi = parse_salary(signals["salary_text"])
        jobs.append(ParsedJob(
            source="linkedin", source_job_id=job_id.group(1),
            title=re.sub(r"\s+", " ", _clean(title.group(1))),
            company=re.sub(r"\s+", " ", _clean(company.group(1))) if company else "",
            location=_clean(location.group(1)) if location else None,
            url=f"https://www.linkedin.com/jobs/view/{job_id.group(1)}/",
            salary_min=lo, salary_max=hi,
            signals=signals,
        ))
    return jobs


def fetch(params: dict, start: int) -> Tuple[str, str]:
    """(status, page html). status is 'ok', 'rate_limited' or 'error'."""
    query = urllib.parse.urlencode({**params, "start": start})
    req = urllib.request.Request(SEARCH_URL + query, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return "ok", resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("rate_limited" if e.code == 429 else "error"), ""
    except OSError:
        return "error", ""


def _fetch_patiently(params: dict, start: int, waits=(60, 180)) -> Tuple[str, str]:
    """Like fetch, but waits out a rate-limit a couple of times before giving up."""
    status, body = fetch(params, start)
    for wait in waits:
        if status != "rate_limited":
            break
        time.sleep(wait)
        status, body = fetch(params, start)
    return status, body


def window_seconds(conn: sqlite3.Connection, name: str, overlap_minutes: int, max_hours: int) -> int:
    """How far back a search should look: since it last ran plus some overlap, at most max_hours."""
    row = conn.execute("SELECT MAX(received_at) FROM emails WHERE source = ? AND subject = ?", (SOURCE, name)).fetchone()
    seconds = max_hours * 3600
    if row and row[0]:
        since = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
        seconds = min(seconds, since + overlap_minutes * 60)
    return max(int(seconds), 3600)


def run(conn: sqlite3.Connection, config: dict) -> Dict[str, int]:
    """Run every configured search; new jobs land as candidates like jobs from alert emails."""
    counts = {"searches": 0, "listings": 0, "outside_area": 0, "new": 0, "rate_limited": 0, "errors": 0}
    base, pages, spacing = config.get("base", {}), config.get("pages", 2), config.get("spacing", 10)
    overlap, max_hours = config.get("overlap_minutes", 30), config.get("max_window_hours", 24)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    first = True
    for q in config.get("queries", []):
        params = {**base, **{k: v for k, v in q.items() if k not in ("name", "keep_locations")}}
        # Without a fixed f_TPR, each search covers the time since it last ran, so hourly runs miss nothing.
        window = None
        if "f_TPR" not in params:
            window = window_seconds(conn, q["name"], overlap, max_hours)
            params["f_TPR"] = f"r{window}"
        # The public search ignores the remote filter (f_WT), so a remote query lists only the locations
        # it should keep: LinkedIn shows a fully remote US job's location as just "United States".
        keep = [k.lower() for k in q.get("keep_locations", [])]
        found: List[ParsedJob] = []
        outside: List[ParsedJob] = []
        for page in range(pages):
            if not first:
                time.sleep(spacing)
            first = False
            status, body = _fetch_patiently(params, page * PAGE_SIZE)
            if status != "ok":
                counts["rate_limited" if status == "rate_limited" else "errors"] += 1
                break
            cards = parse_cards(body)
            for c in cards:
                (found if not keep or any(k in (c.location or "").lower() for k in keep) else outside).append(c)
            if len(cards) < PAGE_SIZE:
                break
        if status == "rate_limited" and not found and not outside:
            break
        # Listings outside the area are still saved, so the filter can drop them with the location as the
        # reason and the dashboard can show them; a job also seen somewhere in the area is kept.
        for job in outside:
            job.signals["outside_search_area"] = [job.location or "an unlisted location"]
        counts["outside_area"] += len(outside)
        msgid = f"search:{q['name']}:{stamp}"
        # A search cut short by an error counts as run only up to its window's start, so the next run re-covers it.
        ran_at = now()
        if status != "ok" and window:
            ran_at = datetime.fromtimestamp(time.time() - window, timezone.utc).isoformat(timespec="seconds")
        record_email(conn, msgid, SOURCE, "LinkedIn search", q["name"], ran_at, len(found))
        for job in found + outside:
            job.signals["found_by_search"] = q["name"]
            _, created = upsert_job(conn, job, msgid)
            counts["new"] += created
        conn.commit()
        counts["searches"] += 1
        counts["listings"] += len(found)
    return counts
