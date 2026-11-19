"""Is the job still open, and how old is it?

Open/closed comes from LinkedIn's public, logged-out job page: an open job shows an Apply button, a job that is
"No longer accepting applications" shows none. No login and no account involved; checks are few, spaced out,
and stop at the first rate-limit.

Age prefers the employer's own posting date (aggregator dates lie), then the research check, then Indeed's
"N days ago", then LinkedIn's, then the date the job first appeared in your alerts.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from .store import now, rescore_when_described

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
SPACING_SECONDS = 10
RECHECK_HOURS = 20
# Statuses where a closed or expired job leaves the list. Jobs you've queued or held keep their status and get a flag.
DROPPABLE = ("candidate", "scored")


def _relative_days(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", text or "", re.I)
    if not m:
        return 0 if re.search(r"just posted|today", text or "", re.I) else None
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]


def check_linkedin(job_id: str) -> Tuple[str, dict]:
    """('open' | 'closed' | 'rate_limited' | 'error', details)."""
    req = urllib.request.Request(GUEST_URL.format(job_id), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("rate_limited" if e.code == 429 else "error"), {"http": e.code}
    except OSError:
        return "error", {}
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    details = {}
    posted = re.search(r"(\d+\+?\s*(?:minute|hour|day|week|month)s?\s+ago|Just posted)", text, re.I)
    if posted:
        details["linkedin_posted"] = posted.group(1)
    applicants = re.search(r"((?:Over |Be among the first )?\d+\s+applicants)", text, re.I)
    if applicants:
        details["linkedin_applicants"] = applicants.group(1)
    if "top-card-layout__title" not in html:
        return "error", details  # not a job page (removed, or LinkedIn changed the layout)
    desc = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', html, re.S)
    if desc:
        details["description"] = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"<[^>]+>", "\n", desc.group(1))).strip()
    return ("open" if "top-card-layout__cta--primary" in html else "closed"), details


def run_checks(conn: sqlite3.Connection, limit: int = 40, spacing: int = SPACING_SECONDS,
               patient: bool = False) -> Dict[str, int]:
    """Check new candidates first (so closed jobs never get scored), then jobs still missing a
    description (any verdict, so a title-only Skip gets a real read), then the shortlist by score.

    Each check also saves the job's description when we don't have one. `patient` (for long background
    runs) waits out a rate-limit a few times instead of stopping at the first one.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RECHECK_HOURS)).isoformat()
    rows = conn.execute(
        "SELECT j.id, j.status, j.signals, s.source_job_id, length(COALESCE(j.description, '')) AS dlen "
        "FROM jobs j JOIN sightings s ON s.job_id = j.id "
        "WHERE s.source = 'linkedin' AND (j.status = 'candidate' OR (j.status IN ('scored', 'queued', 'maybe') "
        "AND (j.verdict IN ('apply', 'maybe') OR length(COALESCE(j.description, '')) < 500))) "
        "GROUP BY j.id ORDER BY (j.status = 'candidate') DESC, (dlen < 500) DESC, COALESCE(j.score, 0) DESC").fetchall()
    counts = {"open": 0, "closed": 0, "error": 0, "rate_limited": 0, "described": 0}
    checked = strikes = 0
    for r in rows:
        signals = json.loads(r["signals"] or "{}")
        if signals.get("linkedin_checked_at", "") > cutoff:
            continue
        if checked >= limit:
            break
        if checked:
            time.sleep(spacing)
        status, details = check_linkedin(r["source_job_id"])
        checked += 1
        counts[status] += 1
        if status == "rate_limited":
            strikes += 1
            if not patient or strikes > 4:
                break
            time.sleep(180)
            continue
        strikes = 0
        if status == "error":
            continue
        description = details.pop("description", "")
        if len(description) >= 500 and r["dlen"] < 500:
            conn.execute("UPDATE jobs SET description = ?, description_source = 'linkedin' WHERE id = ?",
                         (description[:15000], r["id"]))
            rescore_when_described(conn, r["id"])
            counts["described"] += 1
        signals.update(details, linkedin_status=status, linkedin_checked_at=now())
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), r["id"]))
        if status == "closed" and r["status"] in DROPPABLE:
            conn.execute("UPDATE jobs SET status = 'closed', filter_reason = 'no longer accepting applications (LinkedIn)' "
                         "WHERE id = ?", (r["id"],))
        conn.commit()
    return counts


def posted_estimate(conn: sqlite3.Connection, job: sqlite3.Row) -> Tuple[Optional[datetime], str]:
    signals = json.loads(job["signals"] or "{}")
    brief = json.loads(job["brief"] or "{}")
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    try:
        if job["posted_at"]:
            d = parse(job["posted_at"])
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)), "employer posting"
        verified = (brief.get("verified") or {}).get("posted")
        if verified and re.match(r"\d{4}-\d{2}-\d{2}", str(verified)):
            return datetime.fromisoformat(str(verified)[:10]).replace(tzinfo=timezone.utc), "employer posting (research)"
    except ValueError:
        pass
    first_alert = conn.execute("SELECT MIN(e.received_at) FROM sightings s JOIN emails e ON e.gm_msgid = s.gm_msgid "
                               "WHERE s.job_id = ?", (job["id"],)).fetchone()[0]
    first_alert = parse(first_alert) if first_alert else None
    days = _relative_days(signals.get("posted", ""))
    if days is not None and first_alert:
        return first_alert - timedelta(days=days), "Indeed"
    days = _relative_days(signals.get("linkedin_posted", ""))
    if days is not None and signals.get("linkedin_checked_at"):
        return parse(signals["linkedin_checked_at"]) - timedelta(days=days), "LinkedIn"
    if signals.get("google_listed"):
        return datetime.fromisoformat(signals["google_listed"]).replace(tzinfo=timezone.utc), "Google Jobs"
    return first_alert, "first seen in your alerts"


def expire_old(conn: sqlite3.Connection, max_days: int) -> Dict[str, int]:
    """Drop jobs older than max_days from review. Jobs you've queued or held only get flagged."""
    counts = {"expired": 0, "flagged": 0}
    today = datetime.now(timezone.utc)
    for job in conn.execute("SELECT * FROM jobs WHERE status IN ('candidate', 'scored', 'queued', 'maybe')").fetchall():
        when, source = posted_estimate(conn, job)
        if not when:
            continue
        age = (today - when).days
        signals = json.loads(job["signals"] or "{}")
        signals["age_days"], signals["age_source"] = age, source
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job["id"]))
        if age > max_days:
            if job["status"] in DROPPABLE:
                conn.execute("UPDATE jobs SET status = 'expired', filter_reason = ? WHERE id = ?",
                             (f"posted {age} days ago ({source})", job["id"]))
                counts["expired"] += 1
            else:
                counts["flagged"] += 1
    conn.commit()
    return counts
