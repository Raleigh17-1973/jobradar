"""Is a remote search's listing in some other city actually remote?

LinkedIn's public search ignores its remote filter, and it shows a remote job tied to an office as that city,
e.g. "Cleveland, OH" for a remote Cleveland Clinic role. Those listings start out filtered (see filters.py).
This reads each one's posting and records the verdict in signals.remote_check, which the filter then uses:
- a title that says "remote" settles it without reading anything
- otherwise the employer's own job board, when it has a public one (free, no LinkedIn requests)
- otherwise LinkedIn's public page, a limited number per run, newest first, stopping at the first rate-limit
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from . import ats, config, liveness
from .store import now

PENDING = "location %: remote not checked yet"   # filter_reason of a listing still waiting (see filters.py)

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}
HOME_STATE = config.home_area().get("state") or ""   # remote jobs limited to other states are filtered
HOME_AREA = config.area_pattern(config.home_area().get("places") or [])   # "must live within 50 miles of …"
_STATE_NAME = re.compile(r"\b(" + "|".join(sorted(STATES, key=len, reverse=True)) + r")\b", re.I)
_STATE_ABBR = re.compile(r"(?<![A-Za-z])(" + "|".join(STATES.values()) + r")(?![A-Za-z])")
_BY_ABBR = {abbr: name for name, abbr in STATES.items()}
_BY_LOWER = {name.lower(): name for name in STATES}

# "Fully remote", "remote-first", "this role is remote", "Location: Remote", "Remote (US)"...
STRONG = re.compile(
    r"\b(?:fully|100\s?%|completely|entirely|permanently)\s+remote\b"
    r"|\bremote[\s-](?:first|eligible)\b"
    r"|\b(?:this|the)\s+(?:role|position|job|opportunity)\s+(?:is|can\s+be|will\s+be)\s+(?:a\s+)?(?:fully\s+)?remote\b"
    r"|\b(?:location|workplace(?:\s+type)?|work\s+(?:location|arrangement|model|setting))\s*[:\-–]\s*(?:fully\s+)?remote\b"
    r"|\bremote\s*[(,\-–]\s*(?:us|u\.s\.?|usa|united\s+states)\b"
    r"|\bremote(?:ly)?\s+(?:within|in|across|from)\s+(?:the\s+)?(?:us|u\.s\.?|usa|united\s+states|north\s+america|continental)\b",
    re.I)
# Weaker phrasing that a hybrid posting can also use ("work remotely two days a week"). "Hybrid or remote" isn't
# here at all: it's mostly boilerplate ("In Office, Hybrid or Remote", "remote or hybrid for military spouses").
WEAK = re.compile(
    r"\bremote\s+(?:position|role|opportunity|job|employee|worker|work\s+arrangement)\b"
    r"|\bworks?\s+remotely\b|\bworking\s+remotely\b|\btelecommut|#(?:li-)?remote\b"
    r"|\bwork\s+from\s+(?:home|anywhere)\b(?!\s+(?:reimbursement|stipend|allowance|equipment|setup|days?))", re.I)
# "Location: Denver, CO / Remote"
LOCATION_LINE = re.compile(r"\blocation\s*:\s*([^.:;]{0,60})", re.I)
# Board locations such as "Remote-US-MA; Remote-US-MD" name the only states a remote hire can live in.
REMOTE_IN_STATE = re.compile(r"\b(?i:remote)\s*[-–,]\s*(?:(?i:us)\s*[-–,]\s*)?([A-Z]{2})\b")
ONSITE = re.compile(
    r"\bhybrid\b|\bin[\s-]office\b|\bon[\s-]?site\b|\b(?:\d|one|two|three|four|five)\s*\+?\s*days?\s+(?:a|per|each)\s+week\s+(?:in|on|at)\b",
    re.I)
NOT_REMOTE = re.compile(
    r"\bnot\s+(?:a\s+|be\s+|eligible\s+for\s+|open\s+to\s+)?(?:fully\s+)?remote\b|\bno\s+remote\b|\bremote\s+work\s+is\s+not\b",
    re.I)

# Where a remote job says its people must live.
_LIVE = r"(?:reside|live|be\s+located|be\s+based|be\s+(?:a\s+)?residents?)"
RESIDE = re.compile(
    r"\b(?:must|required\s+to|need\s+to|will\s+need\s+to|should)\s+(?:currently\s+)?" + _LIVE + r"\s+(?:in|within)\b([^.;]{0,250})"
    r"|\b(?:open|available|limited|restricted)\s+(?:only\s+)?to\s+(?:candidates\s+|applicants\s+)?(?:who\s+)?"
    r"(?:reside|living|located|residents?)\s+(?:in|of)\b([^.;]{0,250})"
    r"|\b(?:able|eligible|approved|registered)\s+to\s+(?:hire|employ)\s+(?:in|residents\s+of)\b([^.;]{0,300})", re.I)
EXCLUDED = re.compile(
    r"\b(?:not\s+(?:able|eligible|available|open)|unable|cannot)\b[^.;]{0,60}?\b(?:in|residents\s+of|from)\b([^.;]{0,300})",
    re.I)
NEARBY = re.compile(
    r"\b(?:reside|live|located|based)\s+within\s+(\d+)\s+miles\s+(?:of|from)\s+([^.;,]{1,60})"
    r"|\bwithin\s+(?:a\s+)?(?:reasonable\s+)?commut\w*\s+distance\s+(?:of|to|from)\s+(?:our\s+|the\s+|one\s+of\s+our\s+)?([^.;]{1,80})",
    re.I)


def _states(text: str) -> List[str]:
    found = {_BY_LOWER[re.sub(r"\s+", " ", m.group(1).lower())] for m in _STATE_NAME.finditer(text)}
    found |= {_BY_ABBR[m.group(1)] for m in _STATE_ABBR.finditer(text)}
    return sorted(found)


def _named(states: List[str]) -> str:
    return states[0] if len(states) == 1 else ", ".join(states[:-1]) + " or " + states[-1]


def classify(text: str, location: str = "", title: str = "") -> dict:
    """{'verdict': 'remote' | 'not remote' | 'restricted', 'why': ...} from a posting's text and location."""
    text = re.sub(r"\s+", " ", text or "")
    if NOT_REMOTE.search(text):
        return {"verdict": "not remote", "why": "the posting says it isn't remote"}
    remote = (re.search(r"\bremote\b", title or "", re.I)
              or (re.search(r"\bremote\b", location or "", re.I) and not re.search(r"\bhybrid\b", location, re.I))
              or STRONG.search(text) or (WEAK.search(text) and not ONSITE.search(text))
              or any(re.search(r"\bremote\b", m.group(1), re.I) and not re.search(r"\bhybrid\b", m.group(1), re.I)
                     for m in LOCATION_LINE.finditer(text)))
    if not remote:
        return {"verdict": "not remote", "why": "no remote option in the posting"}
    remote_states = sorted({_BY_ABBR[s] for s in REMOTE_IN_STATE.findall(location or "") if s in _BY_ABBR})
    if remote_states and HOME_STATE not in remote_states:
        return {"verdict": "restricted", "why": f"only for people living in {_named(remote_states)}"}
    for m in RESIDE.finditer(text):
        states = _states(next(g for g in m.groups() if g is not None))
        if states and HOME_STATE not in states:
            return {"verdict": "restricted", "why": f"only for people living in {_named(states)}"}
    for m in EXCLUDED.finditer(text):
        if HOME_STATE in _states(m.group(1)):
            return {"verdict": "restricted", "why": f"not open to {HOME_STATE} residents"}
    for m in NEARBY.finditer(text):
        where = (m.group(2) or m.group(3) or "").strip()
        if not (HOME_AREA and HOME_AREA.search(where)):
            distance = f"within {m.group(1)} miles" if m.group(1) else "within commuting distance"
            return {"verdict": "restricted", "why": f"you must live {distance} of {where}"}
    return {"verdict": "remote", "why": "the posting says remote"}


def _listed(signals: dict) -> str:
    return signals.get("linkedin_listed") or signals.get("google_listed", "")


def run(conn: sqlite3.Connection, limit: int = 20, spacing: int = liveness.SPACING_SECONDS,
        max_age_days: int = 14, patient: bool = False) -> Dict[str, int]:
    """Check listings still waiting, newest first. Returns counts for the summary."""
    rows = conn.execute("SELECT id, title, signals, description, ats_checked_at FROM jobs "
                        "WHERE status = 'filtered' AND filter_reason LIKE ?", (PENDING,)).fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).date().isoformat()
    jobs = sorted((r for r in rows if _listed(json.loads(r["signals"] or "{}")) >= cutoff),
                  key=lambda r: (_listed(json.loads(r["signals"] or "{}")), r["id"]), reverse=True)
    counts = {"remote": 0, "not remote": 0, "restricted": 0, "closed": 0, "waiting": 0,
              "too old": len(rows) - len(jobs), "rate_limited": 0, "errors": 0,
              "from title": 0, "from employer board": 0, "from saved posting": 0, "from LinkedIn": 0}

    def record(job_id: int, result: dict, source: str) -> None:
        signals = json.loads(conn.execute("SELECT signals FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] or "{}")
        signals["remote_check"] = {**result, "source": source, "checked_at": now()}
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job_id))
        counts[result["verdict"]] += 1
        counts[f"from {source}"] += 1

    rest = []
    for r in jobs:
        if re.search(r"\bremote\b", r["title"], re.I):
            record(r["id"], {"verdict": "remote", "why": "the title says remote"}, "title")
        else:
            rest.append(r)

    # Employer boards first: one parallel pass, no LinkedIn requests.
    matches: Dict[int, ats.Posting] = {}
    ats.run(conn, ids=[r["id"] for r in rest if not r["ats_checked_at"]], matches=matches)
    unread = []
    for r in rest:
        posting = matches.get(r["id"])
        if posting and len(posting.description) >= 500:
            record(r["id"], {**classify(posting.description, posting.location, r["title"]),
                             "board_location": posting.location}, "employer board")
        elif r["description"] and len(r["description"]) >= 500:
            record(r["id"], classify(r["description"], title=r["title"]), "saved posting")
        else:
            unread.append(r)
    conn.commit()

    # Then LinkedIn's public page, the same request the status check makes.
    checked = strikes = 0
    for r in unread:
        if checked >= limit:
            counts["waiting"] += 1
            continue
        sighting = conn.execute("SELECT source_job_id FROM sightings WHERE job_id = ? AND source = 'linkedin'",
                                (r["id"],)).fetchone()
        if not sighting:
            counts["waiting"] += 1
            continue
        if checked:
            time.sleep(spacing)
        status, details = liveness.check_linkedin(sighting["source_job_id"])
        checked += 1
        if status == "rate_limited":
            counts["rate_limited"] += 1
            strikes += 1
            if not patient or strikes > 4:
                counts["waiting"] += len(unread) - unread.index(r)
                break
            time.sleep(180)
            continue
        strikes = 0
        if status == "error":
            counts["errors"] += 1
            continue
        description = details.pop("description", "")
        signals = json.loads(conn.execute("SELECT signals FROM jobs WHERE id = ?", (r["id"],)).fetchone()[0] or "{}")
        signals.update(details, linkedin_status=status, linkedin_checked_at=now())
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), r["id"]))
        if len(description) >= 500:
            conn.execute("UPDATE jobs SET description = ?, description_source = 'linkedin' WHERE id = ?",
                         (description[:15000], r["id"]))
        if status == "closed":
            conn.execute("UPDATE jobs SET status = 'closed', filter_reason = 'no longer accepting applications (LinkedIn)' "
                         "WHERE id = ?", (r["id"],))
            counts["closed"] += 1
        else:
            record(r["id"], classify(description, title=r["title"]), "LinkedIn")
        conn.commit()
    return counts
