"""VC portfolio job boards: one search per venture firm covers every startup it has funded.

  ./jr vc-boards            read every board in config/vc_boards.yaml (also part of `./jr run` once enabled)
  ./jr vc-find DOMAIN...    check a firm's job board, e.g. jobs.accel.com, and print the line to add

Many firms (Accel, General Catalyst, Insight, Khosla and others) publish their portfolio's jobs on Getro, which
answers a plain search, newest first: remote jobs in the US, jobs in your home state, and every US job (for the
ones Getro calls on-site with no place but "United States", which are often remote). The first run reads back to
your posting-age limit; later runs read only what's new since the last one (`--full` reads back the whole limit).
It keeps the titles that pass your title rules and stores them with the employer's own apply link, pay and
company stage. The usual steps then fetch the description and score them.
- "United States"-only jobs are kept only when the employer's posting says remote (remote_check.classify).
- Acquired startups (stage "acquisition") and jobs whose link is another company's careers site are skipped: Getro
  files a buyer's jobs under the startup's old name (Venmo for PayPal, HotelTonight for Airbnb).

Boards on Consider (Sequoia, Lightspeed, Bessemer, Kleiner Perkins, First Round and others) need a session token
from their page, so Job Radar doesn't read them.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import ats, config, describe, filters, remote_check, watchlist
from .parsers import ParsedJob
from .store import now, record_email, upsert_job

SOURCE = "vc_board"          # sightings.source for jobs found on a VC portfolio board
EMAIL_SOURCE = "vc_boards"   # emails.source for the pseudo-email each run records
GETRO_SEARCH = "https://api.getro.com/api/v2/collections/{id}/search/jobs"
PAGE_SIZE = 100
# Getro files an acquired startup's jobs under its old name, but they're the buyer's jobs (Venmo and Simility both
# list PayPal's openings), so the same job arrives twice under the wrong company. Skipped unless the config says so.
SKIP_STAGES = ["acquisition"]
_US = ("United States", "Remote")
ALL_US = {"searchable_locations": ["United States"]}
COUNTRY = "country only"     # where(): an on-site job whose only location is the whole country
_COUNTRY_ONLY = re.compile(r"^\s*(united states( of america)?|usa|us|remote|north america)\s*$", re.I)
VERIFY_LIMIT = 40            # "United States"-only postings read per run to see if they're remote
# Applicant systems and job boards: their host names say nothing about the employer.
_ATS_HOSTS = re.compile(r"(greenhouse|lever|ashbyhq|myworkdayjobs|workday|smartrecruiters|jobvite|icims|workable|"
                        r"bamboohr|rippling|dover|breezy|recruitee|jazzhr|applytojob|gem|pinpointhq|teamtailor|personio|comeet|"
                        r"successfactors|taleo|oraclecloud|ultipro|ukg|paylocity|paycom|adp|dayforce|eightfold|phenom|"
                        r"avature|getro|linkedin|indeed|wellfound|ycombinator|notion|google|trinethire|polymer|"
                        r"careers-page|hire|jobs)\.", re.I)


def _post_json(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": ats.USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


def searches(home_state: Optional[str]) -> List[dict]:
    """Remote jobs anywhere in the US, every job in your home state (offices near you, hybrid or on-site), and every
    US job, for the on-site ones whose only location is "United States"."""
    out = [{"work_mode": ["remote"], "searchable_locations": ["United States"]}]
    if home_state:
        out.append({"searchable_locations": [f"{home_state}, USA"]})
    out.append(dict(ALL_US))
    return out


def getro_pages(board_id: str, filters_: dict, cutoff: datetime, max_pages: int) -> Optional[List[dict]]:
    """Jobs newest first until they're older than cutoff. None when the board doesn't answer."""
    jobs: List[dict] = []
    for page in range(max_pages):
        data = _post_json(GETRO_SEARCH.format(id=board_id),
                          {"hits_per_page": PAGE_SIZE, "page": page, "query": "", "filters": filters_})
        if data is None:
            return None if page == 0 else jobs
        found = (data.get("results") or {}).get("jobs") or []
        jobs.extend(found)
        if len(found) < PAGE_SIZE or min(j.get("created_at") or 0 for j in found) < cutoff.timestamp():
            break
    return jobs


def _annual_pay(j: dict) -> Tuple[Optional[int], Optional[int]]:
    if (j.get("compensation_currency") or "USD") != "USD" or (j.get("compensation_period") or "year") != "year":
        return None, None
    lo, hi = j.get("compensation_amount_min_cents"), j.get("compensation_amount_max_cents")
    return (lo // 100 if lo else None), (hi // 100 if hi else None)


def where(j: dict, home_state: Optional[str]) -> Optional[str]:
    """The location to store, COUNTRY when only the posting can say (an on-site job listed only as "United States"),
    or None when the job is outside your search: remote only in other states, or an office outside your home area."""
    locs = [l for l in j.get("locations") or [] if l]
    searchable = j.get("searchable_locations") or []
    if j.get("work_mode") == "remote":
        if any(l in _US for l in locs) or (home_state and any(home_state in s for s in searchable)):
            return "Remote, United States"
        return None
    if locs and all(_COUNTRY_ONLY.match(l) for l in locs):
        return COUNTRY
    area = watchlist.IN_AREA
    near = [l for l in locs if area and area.search(l) and not watchlist.ABROAD.search(l)]
    return "; ".join(near) if near else None


def other_company(url: str, company: str) -> bool:
    """True when the apply link is a different company's own careers site (careers.airbnb.com for HotelTonight):
    not a job board's host, and the company's name appears nowhere in the link."""
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    if not host or _ATS_HOSTS.search(host + "."):
        return False
    link = re.sub(r"[^a-z0-9]", "", (url or "").lower())
    name = re.sub(r"[^a-z0-9]", "", company.lower())
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) >= 4]
    return not ((name and name in link) or any(w in link for w in words))


def last_read(conn: sqlite3.Connection) -> Optional[datetime]:
    row = conn.execute("SELECT MAX(received_at) FROM emails WHERE source = ?", (EMAIL_SOURCE,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def run(conn: sqlite3.Connection, cfg: dict, prefs: dict, max_age_days: int = 14, full: bool = False) -> Dict:
    """Read every Getro board in cfg; store fresh jobs whose titles fit, then run the free filters."""
    boards = [b for b in cfg.get("boards", []) if b.get("platform", "getro") == "getro" and b.get("id")]
    home_state = (config.home_area() or {}).get("state")
    rules = filters.Rules(prefs)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    # After the first read, page back only to the last one (with two hours of overlap); jobs are newest first.
    previous = None if full else last_read(conn)
    since = max(cutoff, previous - timedelta(hours=2)) if previous else cutoff
    max_pages = int(cfg.get("max_pages", 20))
    skip_stages = set(cfg.get("skip_stages", SKIP_STAGES) or [])
    tasks = [(b, f) for b in boards for f in searches(home_state)]

    def fetch(t):
        b, f = t
        # The all-US search is the big one; it's only there for the "United States"-only jobs.
        pages = max_pages * 4 if f == ALL_US else max_pages
        return b, getro_pages(str(b["id"]), f, since, pages)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, tasks))
    counts = {"boards": len(boards), "unreachable": 0, "listings": 0, "fit": 0, "outside": 0, "acquired": 0,
              "other_company": 0, "country_remote": 0, "country_not_remote": 0, "country_unread": 0, "new": 0,
              "since": since.isoformat(timespec="minutes")}
    unreachable: List[str] = []
    seen: Dict[int, dict] = {}
    for b, jobs in results:
        if jobs is None:
            if b["name"] not in unreachable:
                unreachable.append(b["name"])
            continue
        for j in jobs:
            if not j.get("id") or (j.get("created_at") or 0) < cutoff.timestamp():
                continue
            counts["listings"] += 1
            entry = seen.setdefault(j["id"], {"job": j, "boards": []})
            if b["name"] not in entry["boards"]:
                entry["boards"].append(b["name"])
    counts["unreachable"] = len(unreachable)
    counts["unique"] = len(seen)
    found = []
    for entry in seen.values():
        j = entry["job"]
        company = ((j.get("organization") or {}).get("name") or "").strip()
        title = (j.get("title") or "").strip()
        if not company or not title or not j.get("url") or not watchlist._title_passes(rules, title, company):
            continue
        if (j.get("organization") or {}).get("stage") in skip_stages:
            counts["acquired"] += 1
            continue
        if other_company(j["url"], company):
            counts["other_company"] += 1
            continue
        location = where(j, home_state)
        if location is None:
            counts["outside"] += 1
            continue
        entry["check"] = None
        if location == COUNTRY:
            if counts["country_remote"] + counts["country_not_remote"] + counts["country_unread"] >= VERIFY_LIMIT:
                counts["country_unread"] += 1
                continue
            page = describe.fetch_url(j["url"]) or {}
            text = page.get("text") or ""
            if len(text) < 500:
                counts["country_unread"] += 1
                continue
            check = remote_check.classify(text, title=title)
            if check["verdict"] != "remote":
                counts["country_not_remote"] += 1
                continue
            counts["country_remote"] += 1
            entry["check"] = {**check, "source": "employer page", "checked_at": now()}
            entry["text"] = text
            location = "Remote, United States"
        counts["fit"] += 1
        found.append((entry, company, title, location))
    msgid = f"vc_boards:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}"
    record_email(conn, msgid, EMAIL_SOURCE, "VC portfolio boards", f"{len(boards)} boards", now(), len(found))
    for entry, company, title, location in found:
        j = entry["job"]
        lo, hi = _annual_pay(j)
        org = j.get("organization") or {}
        signals = {"vc_boards": entry["boards"], "company_stage": org.get("stage"),
                   "vc_work_mode": j.get("work_mode"), "vc_locations": j.get("locations") or [],
                   "remote_check": entry.get("check")}
        job = ParsedJob(source=SOURCE, source_job_id=f"getro:{j['id']}", title=title, company=company,
                        location=location, url=j["url"], salary_min=lo, salary_max=hi,
                        signals={k: v for k, v in signals.items() if v})
        job_id, created = upsert_job(conn, job, msgid)
        counts["new"] += created
        posted = datetime.fromtimestamp(j["created_at"], tz=timezone.utc).isoformat(timespec="seconds")
        conn.execute("UPDATE jobs SET apply_url = COALESCE(apply_url, ?), posted_at = COALESCE(posted_at, ?) WHERE id = ?",
                     (j["url"], posted, job_id))
        if entry.get("text"):
            conn.execute("UPDATE jobs SET description = ?, description_source = 'employer page' WHERE id = ? "
                         "AND length(COALESCE(description, '')) < ?", (entry["text"][:15000], job_id, len(entry["text"])))
    conn.commit()
    filters.run(conn, prefs)
    counts["unreachable_names"] = unreachable
    return counts


def due(conn: sqlite3.Connection, every_hours: float) -> bool:
    row = conn.execute("SELECT MAX(received_at) FROM emails WHERE source = ?", (EMAIL_SOURCE,)).fetchone()
    if not row or not row[0]:
        return True
    hours = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
    return hours >= every_hours - 0.25


def find_board(domain: str) -> Tuple[str, Optional[dict]]:
    """(platform, details) for a firm's job board domain: getro with its id and name, consider, or other."""
    domain = re.sub(r"^https?://", "", domain).strip("/").split("/")[0]
    req = urllib.request.Request(f"https://{domain}/jobs", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = resp.read().decode("utf-8", "replace")
    except OSError as e:
        return "unreachable", {"error": str(e)[:120]}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m and "getro" in page.lower():
        try:
            net = json.loads(m.group(1))["props"]["pageProps"]["network"]
            return "getro", {"id": str(net["id"]), "name": net.get("name") or domain, "domain": domain}
        except (KeyError, TypeError, ValueError):
            pass
    if "consider.com" in page:
        return "consider", {"domain": domain}
    return "other", {"domain": domain}
