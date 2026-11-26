"""Finds a job on the employer's own applicant system through public, no-login job-board APIs.

A match gives the full description, structured pay, posting date and the direct apply link.
Workday, iCIMS and other systems without a public board API are left to the research step.
"""
from __future__ import annotations

import html
import http.client
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from .store import _COMPANY_SUFFIXES, _norm, dedupe_key, now

USER_AGENT = "JobRadar/0.1 (personal job search)"
NEGATIVE_CACHE_DAYS = 7
TITLE_MATCH = 0.88


@dataclass
class Posting:
    ats: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    posted_at: Optional[str] = None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.load(resp)
    # OSError covers HTTP errors, DNS failures and (on Python 3.9) socket.timeout.
    except (OSError, http.client.HTTPException, ValueError):
        return None


def _get_json_status(url: str) -> Tuple[Optional[int], object]:
    """(HTTP status, parsed JSON). The status is None when the server couldn't be reached."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, http.client.HTTPException, ValueError):
        return None, None


def _text(markup: str) -> str:
    text = BeautifulSoup(html.unescape(markup or ""), "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _annual(lo, hi, interval: str = "year") -> Tuple[Optional[int], Optional[int]]:
    if not re.search(r"year|annual|salary", interval or "year", re.I):
        return None, None
    return (int(lo) if lo else None), (int(hi) if hi else None)


# ---------- one fetcher per applicant system ----------

def greenhouse(slug: str) -> Optional[List[Posting]]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true")
    if not data or not data.get("jobs"):
        return None
    out = []
    for j in data["jobs"]:
        pay = (j.get("pay_input_ranges") or [{}])[0]
        lo, hi = pay.get("min_cents"), pay.get("max_cents")
        out.append(Posting("greenhouse", j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""),
                           _text(j.get("content", "")), lo // 100 if lo else None, hi // 100 if hi else None,
                           j.get("first_published") or j.get("updated_at")))
    return out


def lever(slug: str) -> Optional[List[Posting]]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list) or not data:
        return None
    out = []
    for j in data:
        sr = j.get("salaryRange") or {}
        lo, hi = _annual(sr.get("min"), sr.get("max"), sr.get("interval", "year"))
        body = "\n\n".join([j.get("descriptionPlain", "")] +
                           [f"{l.get('text', '')}\n{_text(l.get('content', ''))}" for l in j.get("lists", [])] +
                           [j.get("additionalPlain", "")])
        created = j.get("createdAt")
        out.append(Posting("lever", j["text"], j.get("hostedUrl", ""), (j.get("categories") or {}).get("location", ""),
                           body.strip(), lo, hi,
                           datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else None))
    return out


def ashby(slug: str) -> Optional[List[Posting]]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not data or not data.get("jobs"):
        return None
    out = []
    for j in data["jobs"]:
        lo = hi = None
        for comp in ((j.get("compensation") or {}).get("summaryComponents") or []):
            if comp.get("compensationType") == "Salary":
                lo, hi = _annual(comp.get("minValue"), comp.get("maxValue"), comp.get("interval", "year"))
        out.append(Posting("ashby", j["title"], j.get("jobUrl", ""), j.get("location", ""),
                           j.get("descriptionPlain") or _text(j.get("descriptionHtml", "")), lo, hi, j.get("publishedAt")))
    return out


def smartrecruiters(slug: str) -> Optional[List[Posting]]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not data or not data.get("content"):
        return None
    return [Posting("smartrecruiters", j["name"], f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                    (j.get("location") or {}).get("city", ""), posted_at=j.get("releasedDate"),
                    description=j.get("ref", ""))  # full text fetched only for a match (see _complete)
            for j in data["content"]]


# Pay stated in a posting's text: "Estimated salary range: $80,000 to $130,000", "$150K - $190K".
_TEXT_PAY = re.compile(r"\$\s?(\d[\d,]*)\s*([Kk])?\s*(?:-|–|—|to)\s*\$?\s?(\d[\d,]*)\s*([Kk])?")


def _pay_in_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    for lo, lo_k, hi, hi_k in _TEXT_PAY.findall(text or ""):
        lo_n = int(lo.replace(",", "")) * (1000 if lo_k else 1)
        hi_n = int(hi.replace(",", "")) * (1000 if hi_k else 1)
        if 20000 <= lo_n <= hi_n:      # annual salaries only; skips hourly rates and funding amounts
            return lo_n, hi_n
    return None, None


def gem(slug: str) -> Optional[List[Posting]]:
    data = _get_json(f"https://api.gem.com/job_board/v0/{slug}/job_posts/")
    if not isinstance(data, list) or not data:
        return None
    out = []
    for j in data:
        body = j.get("content_plain") or _text(j.get("content", ""))
        out.append(Posting("gem", j["title"].strip(), j.get("absolute_url", ""), (j.get("location") or {}).get("name", ""),
                           body, *_pay_in_text(body), j.get("first_published_at") or j.get("created_at")))
    return out


class _Paced:
    """One call at a time to a host that throttles, spaced out, waiting out a 429. After the waits run out it gives
    up for the rest of the command, so a run doesn't stall on a host that has stopped answering."""

    def __init__(self, spacing: float, waits: Tuple[float, ...] = (5, 15, 30)):
        self.spacing, self.waits = spacing, waits
        self.lock = threading.Lock()
        self.next = 0.0
        self.gave_up = False

    def get(self, url: str) -> Tuple[bool, object]:
        """(answered, data). answered is False when the host kept saying 429: unknown, not missing."""
        with self.lock:
            if not self.gave_up:
                for wait in (0,) + tuple(self.waits):
                    time.sleep(max(wait, self.next - time.monotonic(), 0))
                    status, data = _get_json_status(url)
                    self.next = time.monotonic() + self.spacing
                    if status != 429:
                        return True, data
                self.gave_up = True
        return False, None


# Workable has two public feeds (tested 2026-11-25):
# - a company's own board (apply.workable.com widget): every job with its full text in one call, but it throttles
#   hard: 16 calls at once drew a 429 for everything after them, and even one call a second was mostly refused for a
#   while after that. So it's read only for named boards (the watchlist, watch-find, board reviews), never swept
#   across every candidate's guessed names. A board that stays throttled goes in THROTTLED: unknown, not missing.
# - the job search (jobs.workable.com), which answered 30 searches in a row at one a second. `run` uses it: one
#   search per job no other board matched, by title, keeping results from the same company.
WORKABLE_BOARD = _Paced(spacing=1.0)
WORKABLE_SEARCH = _Paced(spacing=1.0)
WORKABLE_SEARCH_LIMIT = 80      # searches per command; jobs past it wait for the next run
THROTTLED: set = set()


def _workable_place(j: dict) -> str:
    """'Remote, United States' or 'Austin, Texas, United States'; several locations are joined with '; '."""
    places = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
              for l in (j.get("locations") or []) if not l.get("hidden")]
    places = [p for p in places if p] or [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
    if j.get("telecommuting"):
        places = [f"Remote, {p}" if p else "Remote" for p in places]
    return "; ".join(p for p in places if p)


def workable(slug: str) -> Optional[List[Posting]]:
    """A company's own Workable board. Pay is in the text."""
    answered, data = WORKABLE_BOARD.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{urllib.parse.quote(slug)}?details=true")
    if not answered:
        THROTTLED.add(("workable", slug))
    if not isinstance(data, dict) or not data.get("jobs"):
        return None
    out = []
    for j in data["jobs"]:
        body = _text(j.get("description", ""))
        day = j.get("published_on") or j.get("created_at")
        out.append(Posting("workable", j["title"].strip(), f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                           _workable_place(j), body, *_pay_in_text(body), f"{day}T00:00:00+00:00" if day else None))
    return out


def _same_company(company: str, posted_by: dict, body: str) -> bool:
    """The job's company is the Workable posting's: same name, a website with that name, or (for a renamed company,
    "Everyday Electric (formerly Renew Home)") the name in the posting's text."""
    want = _norm(_COMPANY_SUFFIXES.sub("", company))
    if not want:
        return False
    if want == _norm(_COMPANY_SUFFIXES.sub("", posted_by.get("title") or "")):
        return True
    site = (urllib.parse.urlsplit(posted_by.get("website") or "").hostname or "").lower()
    if any(g.replace("-", "") in site.replace("-", "").split(".") for g in slug_guesses(company)[:2]):
        return True
    return len(want) >= 4 and re.search(rf"\b{re.escape(want)}\b", _norm(body)) is not None


def workable_find(company: str, title: str) -> Tuple[bool, Optional[Posting]]:
    """(answered, posting): the job on Workable's job search, or None when it isn't there."""
    query = urllib.parse.quote(re.sub(r"\s*\([^)]*\)", "", title).strip() or title)
    answered, data = WORKABLE_SEARCH.get(f"https://jobs.workable.com/api/v1/jobs?query={query}&location=United%20States")
    if not answered:
        return False, None
    postings = []
    for j in (data.get("jobs") if isinstance(data, dict) else None) or []:
        body = _text("".join(j.get(k) or "" for k in ("description", "requirementsSection", "benefitsSection")))
        if not _same_company(company, j.get("company") or {}, body):
            continue
        loc = j.get("location") or {}
        place = ", ".join(x for x in (loc.get("city"), loc.get("subregion"), loc.get("countryName")) if x)
        if j.get("workplace") == "remote" or "TELECOMMUTE" in (j.get("locations") or []):
            place = f"Remote, {place}" if place else "Remote"
        postings.append(Posting("workable", (j.get("title") or "").strip(), j.get("url", ""), place, body,
                                *_pay_in_text(body), j.get("created")))
    return True, best_match(title, postings) if postings else None


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "gem": gem,
            "workable": workable}
SWEEP = [a for a in FETCHERS if a != "workable"]   # boards tried under every candidate's guessed names (see above)


# ---------- signing in ----------

# What applying takes on each system, by apply-link host (and path, where the host is shared).
# "none": a plain form. "account": you create an account with that employer, or sign in, before the form.
# "signed_in": it uses an account you already have, so you just need to be signed in.
SIGN_IN = [
    ("greenhouse.io", "Greenhouse", "none"),
    ("lever.co", "Lever", "none"),
    ("ashbyhq.com", "Ashby", "none"),
    ("smartrecruiters.com", "SmartRecruiters", "none"),
    ("jobs.gem.com", "Gem", "none"),
    ("workable.com", "Workable", "none"),
    ("pinpointhq.com", "Pinpoint", "none"),   # one page, no account
    ("rippling.com", "Rippling", "none"),
    ("recruitee.com", "Recruitee", "none"),
    ("bamboohr.com", "BambooHR", "none"),
    ("jobvite.com", "Jobvite", "none"),
    ("silkroad.com", "SilkRoad", "none"),
    ("myworkdayjobs.com", "Workday", "account"),
    ("myworkdaysite.com", "Workday", "account"),
    ("icims.com", "iCIMS", "account"),
    ("taleo.net", "Taleo", "account"),
    ("successfactors.com", "SuccessFactors", "account"),
    ("successfactors.eu", "SuccessFactors", "account"),
    ("sapsf.eu", "SuccessFactors", "account"),       # tms: careers.morganstreet.com Apply -> career55.sapsf.eu sign-in (2026-11-07)
    ("sapsf.com", "SuccessFactors", "account"),
    ("careers.morganstreet.com", "SuccessFactors", "account"),
    ("ultipro.com", "UKG", "account"),
    ("ukg.net", "UKG", "account"),
    ("oraclecloud.com", "Oracle", "account"),
    ("myjobs.adp.com", "ADP", "account"),          # Apply goes to an ADP sign-in / create-profile page
    ("google.com/about/careers", "Google", "signed_in"),
    ("metacareers.com", "Meta Careers", "none"),     # the Career Profile account is optional; applied without one (2026-11-07)
]


def sign_in(apply_url: Optional[str], ats_name: Optional[str] = None) -> Optional[dict]:
    """{system, need} for a job's applicant system, or None when it's a company site we can't place."""
    parts = urllib.parse.urlsplit(apply_url or "")
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    for where, system, need in SIGN_IN:
        domain, _, prefix = where.partition("/")
        if host and (host == domain or host.endswith("." + domain)) and path.startswith("/" + prefix):
            return {"system": system, "need": need}
    for _, system, need in SIGN_IN:
        if ats_name and ats_name.lower() == system.lower():
            return {"system": system, "need": need}
    return None


def _complete(p: Posting) -> Posting:
    """SmartRecruiters lists postings without text; fetch the one we matched."""
    if p.ats == "smartrecruiters" and p.description.startswith("https://"):
        detail = _get_json(p.description) or {}
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        p.description = "\n\n".join(_text(s.get("text", "")) for s in sections.values() if isinstance(s, dict))
    return p


# ---------- matching ----------

def slug_guesses(company: str) -> List[str]:
    words = _norm(_COMPANY_SUFFIXES.sub("", re.sub(r"^the\s+", "", company, flags=re.I))).split()
    if not words:
        return []
    guesses = ["".join(words), "-".join(words)]
    if len(words) > 1 and len(words[0]) >= 4:
        guesses.append(words[0])
    return list(dict.fromkeys(guesses))


def _title_key(title: str) -> str:
    return dedupe_key("", title).split("|", 1)[1]


def best_match(title: str, postings: List[Posting]) -> Optional[Posting]:
    want = _title_key(title)
    scored = [(SequenceMatcher(None, want, _title_key(p.title)).ratio(), p) for p in postings]
    score, posting = max(scored, key=lambda sp: sp[0], default=(0, None))
    return posting if score >= TITLE_MATCH else None


# ---------- run ----------

BOARD_CACHE = """
CREATE TABLE IF NOT EXISTS ats_boards (
  ats        TEXT NOT NULL,
  slug       TEXT NOT NULL,
  found      INTEGER NOT NULL,
  checked_at TEXT NOT NULL,
  PRIMARY KEY (ats, slug)
);
"""


def run(conn: sqlite3.Connection, ids: Optional[List[int]] = None,
        matches: Optional[Dict[int, Posting]] = None) -> Dict[str, int]:
    """Look up every candidate not yet checked, or just the jobs in ids. Returns counts for the summary;
    matches, when given, collects each matched posting by job id."""
    conn.executescript(BOARD_CACHE)
    if ids is None:
        jobs = conn.execute("SELECT id, company, title, salary_max, salary_min FROM jobs "
                            "WHERE status = 'candidate' AND ats_checked_at IS NULL").fetchall()
    else:
        jobs = conn.execute(f"SELECT id, company, title, salary_max, salary_min FROM jobs "
                            f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    stale = (datetime.now(timezone.utc) - timedelta(days=NEGATIVE_CACHE_DAYS)).isoformat()
    known_missing = {(r["ats"], r["slug"]) for r in
                     conn.execute("SELECT ats, slug FROM ats_boards WHERE found = 0 AND checked_at > ?", (stale,))}
    boards = {(ats, slug) for job in jobs for slug in slug_guesses(job["company"]) for ats in SWEEP
              if (ats, slug) not in known_missing}

    def fetch(key):
        return key, FETCHERS[key[0]](key[1])

    with ThreadPoolExecutor(max_workers=16) as pool:
        fetched = dict(pool.map(fetch, boards))
    for (ats, slug), postings in fetched.items():
        conn.execute("INSERT OR REPLACE INTO ats_boards VALUES (?, ?, ?, ?)", (ats, slug, int(bool(postings)), now()))

    counts = {"checked": len(jobs), "matched": 0, "board_but_no_title_match": 0}
    searched = 0
    for job in jobs:
        postings = [p for slug in slug_guesses(job["company"]) for ats in SWEEP
                    for p in (fetched.get((ats, slug)) or [])]
        match = best_match(job["title"], postings) if postings else None
        if not match:
            if searched >= WORKABLE_SEARCH_LIMIT or WORKABLE_SEARCH.gave_up:
                counts["left for next run"] = counts.get("left for next run", 0) + 1
                continue        # not searched yet, so not checked: the next run looks it up again
            searched += 1
            answered, match = workable_find(job["company"], job["title"])
            if not answered:
                counts["left for next run"] = counts.get("left for next run", 0) + 1
                continue
        if match:
            match = _complete(match)
            conn.execute("UPDATE jobs SET ats = ?, apply_url = ?, description = ?, description_source = ?, posted_at = ? "
                         "WHERE id = ?", (match.ats, match.url, match.description,
                                          match.ats if match.description else None, match.posted_at, job["id"]))
            if match.salary_max and not job["salary_max"]:
                conn.execute("UPDATE jobs SET salary_min = ?, salary_max = ?, salary_source = 'posted' WHERE id = ?",
                             (match.salary_min, match.salary_max, job["id"]))
            counts["matched"] += 1
            counts[match.ats] = counts.get(match.ats, 0) + 1
            if matches is not None:
                matches[job["id"]] = match
        elif postings:
            # The company's own board is live but doesn't list this role: often filled, closed, or a stale repost.
            signals = json.loads(conn.execute("SELECT signals FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0] or "{}")
            signals["not_on_company_board"] = True
            conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job["id"]))
            counts["board_but_no_title_match"] += 1
        conn.execute("UPDATE jobs SET ats_checked_at = ? WHERE id = ?", (now(), job["id"]))
    conn.commit()
    return counts
