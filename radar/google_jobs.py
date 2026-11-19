"""Google for Jobs, read in your own Chrome: Google has no search API for it.

Claude runs search N of settings.yaml google_jobs.queries in Chrome:
  ./jr google-js list N          JavaScript that saves the page's cards, as {i, lines}, to Downloads/jr_google_N.json
  ./jr google-import N           store them like any other source; prints the card numbers that need a description
  ./jr google-js detail N NUMS   JavaScript that opens those cards and saves Google's description and apply links
  ./jr google-details N          save the descriptions, the LinkedIn posting and any employer apply link
Results travel as downloads because script return values are cut off at about 1,000 characters (Chrome must allow
automatic downloads for google.com). Google's own job ids look like tokens and get blocked, so a job is keyed by a hash of its title,
company and location instead.
See prompts/google_jobs.md for the full sweep.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ats, filters
from .parsers import HOURS_PER_YEAR, ParsedJob
from .store import now, record_email, upsert_job

SOURCE = "google_jobs"        # emails.source for the pseudo-email each search records
JS = Path(__file__).with_name("google_jobs.js")
MIN_DESCRIPTION = 500

_AGE = re.compile(r"^(\d+\+?\s*(minute|hour|day|week|month)s?\s+ago|just posted|today)$", re.I)
_JOB_TYPES = ("full-time", "part-time", "contractor", "internship", "temporary", "per diem")
_PAY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([Kk])?")
_OTHER_LOCATIONS = re.compile(r"\s*\(\+\d+ others?\)$")
_LINKEDIN_JOB = re.compile(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d{6,})")
# Repost sites Google lists as "Apply on ...": never treated as the employer's own apply link.
BOARDS = ("indeed.", "linkedin.", "glassdoor.", "ziprecruiter.", "talent.com", "bebee.", "learn4good.", "trabajo.org",
          "jobleads.", "dice.", "builtin", "monster.", "simplyhired.", "jooble.", "careerbuilder.", "jobgether.",
          "adzuna.", "lensa.", "salary.com", "snagajob.", "ladders.", "wellfound.", "getwork.", "recruit.net",
          "jobilize.", "tallo.", "whatjobs.", "joblist.", "jobright.", "teal", "hiring.cafe", "mediabistro.",
          "careerjet.", "talentify.", "upwardly.", "remoterocketship.", "himalayas.", "workingnomads.")


def search_url(cfg: dict, q: dict) -> str:
    """Google's date and remote filters are just words in the search box."""
    words = f"{q['q']} {cfg.get('window', 'in the last 3 days')}"
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": words, "udm": 8})


def parse_pay(text: str) -> Tuple[Optional[int], Optional[int]]:
    """'120K–150K a year', '154,375–193,125 a year', '45–60 an hour' -> annual (min, max)."""
    amounts = [float(n.replace(",", "")) * (1000 if k else 1) for n, k in _PAY.findall(text)]
    if not amounts:
        return None, None
    if re.search(r"\bhour\b", text, re.I) and max(amounts) < 500:
        amounts = [a * HOURS_PER_YEAR for a in amounts]
    return int(min(amounts)), int(max(amounts))


def parse_card(card: dict) -> Optional[ParsedJob]:
    """A results card's text lines: title, company, 'location • via site', then age, pay, job type, perks."""
    lines = [l for l in card.get("lines", []) if l]
    if lines and len(lines[0]) == 1:          # the letter Google shows when a company has no logo
        lines = lines[1:]
    if len(lines) < 3 or " via " not in lines[2]:
        return None
    where, _, via = lines[2].partition(" • via ")
    location = _OTHER_LOCATIONS.sub("", where.strip()) or None
    signals = {"google_via": via.strip()}
    lo = hi = None
    for line in lines[3:]:
        if _AGE.match(line):
            signals["google_posted"] = line
        elif re.search(r"\b(a year|an hour|a month)\b", line) and _PAY.search(line):
            signals["salary_text"] = line
            lo, hi = parse_pay(line)
        elif line.lower() in _JOB_TYPES:
            signals["job_type"] = line
    key = hashlib.sha1(f"{lines[0]}|{lines[1]}|{where.strip()}".encode()).hexdigest()[:16]
    return ParsedJob(source="google", source_job_id=key, title=lines[0], company=lines[1], location=location,
                     url="https://www.google.com/search?" + urllib.parse.urlencode({"q": f"{lines[0]} {lines[1]}", "udm": 8}),
                     salary_min=lo, salary_max=hi, signals=signals)


def due(conn: sqlite3.Connection, every_hours: float) -> Tuple[bool, Optional[float]]:
    """Whether the scheduled run should sweep Google now, and hours since the last sweep (None if never).
    A quarter-hour of slack keeps a sweep at 6:31 from making the 9:30 run wait an hour."""
    row = conn.execute("SELECT MAX(received_at) FROM emails WHERE source = ?", (SOURCE,)).fetchone()
    if not row or not row[0]:
        return True, None
    hours = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
    return hours >= every_hours - 0.25, hours


def _listed(posted: str, seen: datetime) -> Optional[str]:
    from .liveness import _relative_days
    days = _relative_days(posted)
    return (seen - timedelta(days=days)).date().isoformat() if days is not None else None


def import_cards(conn: sqlite3.Connection, query: dict, cards: List[dict], prefs: dict) -> Dict[str, object]:
    """Store one search's cards, run the free filters, and list the card numbers still needing a description."""
    seen = datetime.now(timezone.utc)
    keep = [k.lower() for k in query.get("keep_locations", [])]
    msgid = f"google:{query['name']}:{seen.strftime('%Y-%m-%dT%H')}"
    parsed = [(c.get("i"), parse_card(c)) for c in cards]
    jobs = [j for _, j in parsed if j]
    record_email(conn, msgid, SOURCE, "Google for Jobs", query["name"], now(), len(jobs))
    counts = {"cards": len(cards), "listings": len(jobs), "new": 0, "outside_area": 0}
    ids: Dict[str, int] = {}
    for job in jobs:
        job.signals["found_by_google"] = query["name"]
        listed = _listed(job.signals.get("google_posted", ""), seen)
        if listed:
            job.signals["google_listed"] = listed
        if keep and not any(k in (job.location or "").lower() for k in keep):
            job.signals["outside_search_area"] = [job.location or "an unlisted location"]
            counts["outside_area"] += 1
        job_id, created = upsert_job(conn, job, msgid)
        counts["new"] += created
        ids[job.source_job_id] = job_id
    conn.commit()
    filters.run(conn, prefs)
    need = []
    for i, job in parsed:
        if not job or job.source_job_id not in ids:
            continue
        job_id = ids.pop(job.source_job_id)
        r = conn.execute("SELECT status, filter_reason, description FROM jobs WHERE id = ?", (job_id,)).fetchone()
        waiting = r["status"] == "candidate" or (r["status"] == "filtered" and
                                                 (r["filter_reason"] or "").endswith("remote not checked yet"))
        if waiting and len(r["description"] or "") < MIN_DESCRIPTION:
            need.append(i)
    counts["need_details"] = need
    return counts


def _employer_link(links: List[str]) -> Optional[str]:
    """The first apply link on an applicant system or a careers/jobs site that isn't a repost board."""
    for url in links:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(b in host for b in BOARDS):
            continue
        if ats.sign_in(url) or re.match(r"^(careers?|jobs?)\.", host):
            return url
    return None


def save_details(conn: sqlite3.Connection, details: List[dict]) -> Dict[str, int]:
    """Google's description panel: full text, the LinkedIn posting behind it, and the employer's apply link."""
    counts = {"described": 0, "linkedin": 0, "apply_links": 0, "unmatched": 0}
    for d in details:
        job = parse_card(d)
        row = job and conn.execute("SELECT j.id, j.description, j.apply_url, j.signals FROM sightings s "
                                   "JOIN jobs j ON j.id = s.job_id WHERE s.source = 'google' AND s.source_job_id = ?",
                                   (job.source_job_id,)).fetchone()
        if not row:
            counts["unmatched"] += 1
            continue
        text = (d.get("description") or "").strip()
        if text.lower().startswith("job description"):
            text = text[len("job description"):].strip()
        if len(text) >= MIN_DESCRIPTION and len(text) > len(row["description"] or ""):
            conn.execute("UPDATE jobs SET description = ?, description_source = 'google' WHERE id = ?", (text[:15000], row["id"]))
            counts["described"] += 1
        links = [l for l in d.get("apply", []) if l.startswith("http")]
        for url in links:
            m = _LINKEDIN_JOB.search(url)
            if m:
                conn.execute("INSERT OR IGNORE INTO sightings VALUES (?, 'linkedin', ?, ?, NULL, ?)",
                             (row["id"], m.group(1), f"https://www.linkedin.com/jobs/view/{m.group(1)}/", now()))
                counts["linkedin"] += 1
                break
        employer = _employer_link(links)
        if employer and not row["apply_url"]:
            conn.execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (employer, row["id"]))
            counts["apply_links"] += 1
        signals = json.loads(row["signals"] or "{}")
        signals["google_apply_sites"] = sorted({(urllib.parse.urlsplit(l).hostname or "") for l in links} - {""})
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), row["id"]))
    conn.commit()
    return counts


DOWNLOADS = Path.home() / "Downloads"


def download_name(n: int, details: bool = False) -> str:
    return f"jr_google_{n}{'_details' if details else ''}.json"


def detail_js(n: int, numbers: List[int], cards: List[dict]) -> str:
    """The cards to open, found on the page by title and company: Google re-renders the list, so positions shift."""
    by_number = {c.get("i"): c for c in cards}
    targets = []
    for x in numbers:
        job = parse_card(by_number.get(int(x), {}))
        if job:
            targets.append({"i": int(x), "title": job.title, "company": job.company})
    return (JS.read_text().replace("__MODE__", "detail").replace("__NAME__", download_name(n, True))
            .replace("__IDS__", json.dumps(targets, ensure_ascii=False)))


def last_cards(n: int, keep_dir: Path) -> List[dict]:
    """The cards from search N's latest list download, as google-import filed them."""
    files = sorted(keep_dir.glob(f"*_{download_name(n)}"))
    if not files:
        raise SystemExit(f"No list for search {n} in {keep_dir}; run google-import {n} first.")
    return json.loads(files[-1].read_text())


def list_js(n: int) -> str:
    return JS.read_text().replace("__MODE__", "list").replace("__NAME__", download_name(n)).replace("__IDS__", "[]")


def take_download(n: int, details: bool, keep_dir: Path, wait_seconds: int = 15) -> list:
    """Read the sweep's download and move it into keep_dir, so the next run's file keeps the same name."""
    import time
    src = DOWNLOADS / download_name(n, details)
    for _ in range(wait_seconds * 2):
        if src.exists() and not (DOWNLOADS / (src.name + ".crdownload")).exists():
            break
        time.sleep(0.5)
    else:
        raise SystemExit(f"{src} hasn't arrived. Did Chrome ask where to save it, or block multiple downloads?")
    data = json.loads(src.read_text())
    keep_dir.mkdir(parents=True, exist_ok=True)
    src.replace(keep_dir / f"{datetime.now().strftime('%H%M%S')}_{src.name}")
    return data
