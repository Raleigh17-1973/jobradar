"""SQLite store for alert emails, jobs, and where each job was seen.

Job status moves through:
  new -> filtered (dropped by rules) | scored -> queued -> packet_ready -> applied
  -> interviewing | rejected | offer;  any of these -> skipped
"""
from __future__ import annotations

import heapq
import html
import json
import re
import sqlite3
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
  gm_msgid     TEXT PRIMARY KEY,
  source       TEXT NOT NULL,           -- linkedin | indeed | glassdoor | other
  sender       TEXT,
  subject      TEXT,
  received_at  TEXT,
  parsed_jobs  INTEGER NOT NULL DEFAULT 0,
  processed_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY,
  dedupe_key    TEXT NOT NULL UNIQUE,   -- normalized company|title
  title         TEXT NOT NULL,
  company       TEXT NOT NULL,
  location      TEXT,
  work_mode     TEXT,                   -- remote | hybrid | onsite | unknown
  salary_min    INTEGER,
  salary_max    INTEGER,
  salary_source TEXT,                   -- posted | indeed | estimate
  posted_at     TEXT,
  first_seen    TEXT NOT NULL,
  ats           TEXT,                   -- greenhouse | lever | ashby | workday | ...
  apply_url     TEXT,
  description   TEXT,
  signals       TEXT,                   -- JSON: easy_apply, saved_by_you, ratings, locations, ...
  status        TEXT NOT NULL DEFAULT 'new',
  filter_reason TEXT,
  score         INTEGER,
  verdict       TEXT,                   -- apply | maybe | skip
  brief         TEXT,                   -- research brief (JSON)
  decided_at    TEXT,
  applied_at    TEXT,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS sightings (
  job_id        INTEGER NOT NULL REFERENCES jobs(id),
  source        TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  url           TEXT,
  gm_msgid      TEXT REFERENCES emails(gm_msgid),
  seen_at       TEXT NOT NULL,
  UNIQUE (source, source_job_id)
);

CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""

_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh)\b\.?", re.I)
_TITLE_LOCATION_TAIL = re.compile(r"\s*[(\[|–-]\s*(remote|hybrid|on-?site|us|usa|united states)\s*[)\]]?\s*$", re.I)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(s: Optional[str]) -> str:
    return _NON_WORD.sub(" ", (s or "").lower()).strip()


def dedupe_key(company: str, title: str) -> str:
    """One row per company + role, however many sites or cities it's posted under."""
    company = _norm(_COMPANY_SUFFIXES.sub("", company or ""))
    title = re.sub(r"\bsr\b", "senior", _norm(_TITLE_LOCATION_TAIL.sub("", title or "")))
    return f"{company}|{title}"


def _title_key(title: str) -> str:
    return dedupe_key("", title).split("|", 1)[1]


# ---------- the same posting under another company name or title ----------
# Aggregators spell employers their own way ("Northwindsecurity" for Northwind AI), so company|title misses some
# repeats. The employer's applicant-system posting id in the apply link doesn't change, so it catches them.

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_POSTING_PATHS = (   # host (itself or a subdomain), path pattern whose first group is the posting id, key prefix
    ("greenhouse.io", r"^/[^/]+/jobs/(\d+)", "greenhouse"),
    ("ashbyhq.com", rf"^/[^/]+/({_UUID})", "ashby"),
    ("lever.co", rf"^/[^/]+/({_UUID})", "lever"),
    ("smartrecruiters.com", r"^/[^/]+/(\d{6,})", "smartrecruiters"),
    ("rippling.com", rf"^/[^/]+/jobs/({_UUID})", "rippling"),
    ("gem.com", r"^/[^/]+/([A-Za-z0-9_-]{16,})", "gem"),
    ("workable.com", r"^/[^/]+/j/([A-Za-z0-9]{6,})", "workable"),
    ("linkedin.com", r"^/jobs/view/(?:[^/]*-)?(\d{6,})", "linkedin"),
)
_TRACKING_PARAMS = re.compile(r"^(utm_.*|gh_src|src|source|ref|refid|referrer|trk|trackingid|lever-source|"
                              r"lever-origin|iis|iisn|fbclid|gclid|mode)$", re.I)
DUPLICATE_OF = "duplicate of job "
_DEAD = ("filtered", "closed", "expired")
_OPEN = ("new", "candidate", "scored", "queued", "packet_ready", "maybe")
# Which copy of a posting stays: the one furthest along (applied, then packet, queue, Maybe, your Skip), oldest first.
# Among scored copies, one Claude rated Apply or Maybe beats one it rated Skip: the later copy often has better facts
# (Daylight 11832 read "New York only"; the remote board's copy and Daylight's own posting say remote).
_KEEP_RANK = {"packet_ready": 1, "queued": 2, "maybe": 3, "skipped": 4, "scored": 5, "candidate": 6, "new": 7}

# ---------- the same posting re-listed with no link to compare ----------
# Google and remote boards re-list postings with no apply link, their own company name ("Kelso's - Corporate")
# and a padded title ("..., Remote Job", "... job at Gainwell Technologies in US National"). The description barely
# changes, so near-identical text plus a matching title marks a repeat. Titles may differ only by filler, a place
# or the company's name, never by a level or role word: some employers reuse one description for Director and
# Senior Director, or for two analytics roles.
_TEXT_MIN_WORDS = 150   # shorter descriptions are snippets or templates, too thin to compare
_TEXT_RUN = 8           # compare overlapping 8-word runs
_TEXT_SAME = 0.8        # share of runs in common; unrelated jobs that share boilerplate stay under 0.4
_TEXT_SKETCH = 12       # runs per job used to find candidate pairs
_TEXT_BUCKET_MAX = 200  # a run shared by more jobs than this is boilerplate (EEO text), useless for pairing
_WORDS = re.compile(r"[a-z0-9]+")
_TITLE_FILLER = frozenset("a an and at for in of on the to with job jobs remote hybrid onsite site anywhere us usa "
                          "united states national opportunity opening position role hiring now urgent new".split())
_TITLE_DISTINCT = frozenset(
    "senior sr junior jr lead principal staff director associate assistant head vp avp svp evp vice president chief "
    "manager mgr executive intern internship i ii iii iv 1 2 3 4 entry mid distinguished fellow partner deputy "
    "supervisor coordinator specialist analyst consultant engineer architect officer owner scientist developer "
    "designer strategist advisor technical tpm pm pmo program project product contract contractor temp temporary "
    "part".split())
_TITLE_EXTRA_MAX = 4


def posting_key(url: Optional[str]) -> Optional[str]:
    """The applicant system's id for one posting ("greenhouse:8000000001"), or None for a link without one."""
    parts = urllib.parse.urlsplit(url or "")
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    query = {k.lower(): v for k, v in urllib.parse.parse_qsl(parts.query)}
    on = lambda domain: host == domain or host.endswith("." + domain)
    if query.get("gh_jid", "").isdigit():   # Greenhouse boards embedded in an employer's own site
        return f"greenhouse:{query['gh_jid']}"
    if on("greenhouse.io") and query.get("token", "").isdigit():
        return f"greenhouse:{query['token']}"
    for domain, pattern, prefix in _POSTING_PATHS:
        m = on(domain) and re.search(pattern, path, re.I)
        if m:
            return f"{prefix}:{m.group(1).lower()}"
    if on("myworkdayjobs.com") and "/job/" in path:
        segment = next((s for s in reversed(path.split("/")) if "_" in s), "")
        if segment:
            return f"workday:{host.split('.')[0]}:{segment.rsplit('_', 1)[1].lower()}"
    m = on("icims.com") and re.search(r"/jobs/(\d+)", path)
    if m:
        return f"icims:{host[:-len('.icims.com')]}:{m.group(1)}"
    m = on("oraclecloud.com") and re.search(r"/job/(\d+)", path)
    if m:
        return f"oracle:{host.split('.')[0]}:{m.group(1)}"
    if on("ultipro.com") and query.get("opportunityid"):
        return f"ultipro:{query['opportunityid'].lower()}"
    # ADP: MyJobs (myjobs.adp.com/<tenant>/cx/job-details?reqId=N) and Workforce Now (?cid=<company>&jobId=N).
    # Aggregators repost ADP jobs under new titles, so the req id is what ties them together.
    if on("myjobs.adp.com") and query.get("reqid", "").isdigit():
        return f"adp:{path.strip('/').split('/')[0].lower()}:{query['reqid']}"
    if on("workforcenow.adp.com") and query.get("jobid", "").isdigit() and query.get("cid"):
        return f"adp:{query['cid'].lower()}:{query['jobid']}"
    return None


def applied_repeat(conn: sqlite3.Connection, job_id: int, urls: Iterable[Optional[str]]) -> Optional[Tuple[sqlite3.Row, str]]:
    """The job you've applied to whose posting id matches one of these links (its apply link or a sighting's)."""
    keys = {k for k in map(posting_key, urls) if k}
    if not keys:
        return None
    for a in conn.execute("SELECT id, company, title, applied_at, apply_url FROM jobs "
                          "WHERE applied_at IS NOT NULL AND id != ? ORDER BY applied_at", (job_id,)):
        theirs = {posting_key(a["apply_url"])} | {posting_key(s["url"]) for s in
                                                   conn.execute("SELECT url FROM sightings WHERE job_id = ?", (a["id"],))}
        hit = sorted(keys & theirs)
        if hit:
            return a, hit[0]
    return None


def _page_key(url: Optional[str]) -> Optional[str]:
    """A link without a posting id, minus tracking parameters; only paired with the title does it mean one job."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    host = host[4:] if host.startswith("www.") else host
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k))
    return host + (parts.path.rstrip("/") or "/") + ("?" + urllib.parse.urlencode(query) if query else "")


def _keep_rank(r: sqlite3.Row) -> float:
    if r["applied_at"] or r["status"] not in _KEEP_RANK:
        return 0
    return _KEEP_RANK[r["status"]] + (0.5 if r["status"] == "scored" and r["verdict"] == "skip" else 0)


def _title_words(title: str) -> set:
    return set(_title_key(html.unescape(title or "")).split()) - _TITLE_FILLER


def _same_title(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """One title holds the other's words, plus at most a few extras that are filler, a place or a company name."""
    ta, tb = _title_words(a["title"]), _title_words(b["title"])
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not small or not small <= big:
        return False
    names = set(_norm(_COMPANY_SUFFIXES.sub("", f"{a['company'] or ''} {b['company'] or ''}")).split())
    extra = big - small - names
    return len(extra) <= _TITLE_EXTRA_MAX and not extra & _TITLE_DISTINCT


def _text_runs(text: Optional[str]) -> Optional[set]:
    words = _WORDS.findall(html.unescape(text or "").lower())
    if len(words) < _TEXT_MIN_WORDS:
        return None
    return {hash(tuple(words[i:i + _TEXT_RUN])) for i in range(len(words) - _TEXT_RUN + 1)}


def _same_text_repeats(rows: dict) -> List[Tuple[int, int]]:
    """(repeat id, kept id) for open jobs whose description and title match a copy that stays.

    Jobs are taken in keep order (furthest along first), and a job closes only when it matches a kept job directly,
    so two different reqs are never joined through a third, vaguer title.
    """
    runs = {job_id: s for job_id, r in rows.items() if (s := _text_runs(r["description"]))}
    buckets = defaultdict(list)
    for job_id, s in runs.items():
        for h in heapq.nsmallest(_TEXT_SKETCH, s):
            buckets[h].append(job_id)
    near = defaultdict(set)
    for ids in buckets.values():
        if len(ids) <= _TEXT_BUCKET_MAX:
            for job_id in ids:
                near[job_id].update(ids)
    order = lambda job_id: (_keep_rank(rows[job_id]), job_id)
    kept, repeats = set(), []
    for job_id in sorted(runs, key=order):
        r, mine = rows[job_id], runs[job_id]
        match = None
        if r["status"] in _OPEN:
            match = next((k for k in sorted(near[job_id] & kept, key=order)
                          if len(mine & runs[k]) >= _TEXT_SAME * len(mine | runs[k]) and _same_title(r, rows[k])), None)
        if match is None:
            kept.add(job_id)
        else:
            repeats.append((job_id, match))
    return repeats


def close_as_repeat(conn: sqlite3.Connection, r: sqlite3.Row, keep: int) -> None:
    signals = {**json.loads(r["signals"] or "{}"), "duplicate_of": keep}
    conn.execute("UPDATE jobs SET status = 'closed', filter_reason = ?, signals = ? WHERE id = ?",
                 (f"{DUPLICATE_OF}{keep}", json.dumps(signals, sort_keys=True), r["id"]))
    conn.execute("UPDATE sightings SET job_id = ? WHERE job_id = ?", (keep, r["id"]))


def mark_duplicates(conn: sqlite3.Connection) -> List[Tuple[int, int]]:
    """Close every open job that repeats another job's posting under a different company name or title.

    Same posting: the same applicant-system posting id in the apply link or a sighting's link, the same apply
    link and title, or near-identical description text with a matching title (see _same_text_repeats). The copy
    furthest along stays (applied, packet, queue, Maybe, your Skip; oldest on a tie). The others close with
    filter_reason "duplicate of job N", which no dashboard click reopens, and their sightings move to the kept
    job. Applied and skipped copies are left as they are. Returns (duplicate id, kept id) pairs.
    """
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, company, title, status, verdict, applied_at, apply_url, signals, description FROM jobs "
        f"WHERE status NOT IN ({','.join('?' * len(_DEAD))})", _DEAD)}
    keys = defaultdict(set)
    for r in rows.values():
        key = posting_key(r["apply_url"])
        page = None if key else _page_key(r["apply_url"])
        if key or page:
            keys[r["id"]].add(key or f"page:{page}|{_title_key(r['title'])}")
    for s in conn.execute("SELECT job_id, url FROM sightings WHERE url IS NOT NULL"):
        key = s["job_id"] in rows and posting_key(s["url"])
        if key:
            keys[s["job_id"]].add(key)

    parent = {job_id: job_id for job_id in keys}

    def root(job_id: int) -> int:
        while parent[job_id] != job_id:
            parent[job_id] = parent[parent[job_id]]
            job_id = parent[job_id]
        return job_id

    first = {}
    for job_id in sorted(keys):
        for key in keys[job_id]:
            if key in first:
                parent[root(job_id)] = root(first[key])
            else:
                first[key] = job_id
    groups = defaultdict(list)
    for job_id in keys:
        groups[root(job_id)].append(rows[job_id])

    pairs = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (_keep_rank(r), r["id"]))
        keep = members[0]["id"]
        for r in members[1:]:
            if r["status"] not in _OPEN:
                continue
            close_as_repeat(conn, r, keep)
            pairs.append((r["id"], keep))
    closed = {dup for dup, _ in pairs}
    for dup, keep in _same_text_repeats({job_id: r for job_id, r in rows.items() if job_id not in closed}):
        close_as_repeat(conn, rows[dup], keep)
        pairs.append((dup, keep))
    return pairs


def _kept_copy(conn: sqlite3.Connection, job_id: int) -> int:
    """The job a closed duplicate points to (following a chain), or job_id itself."""
    for _ in range(5):
        r = conn.execute("SELECT filter_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()
        reason = (r["filter_reason"] or "") if r else ""
        if not (reason.startswith(DUPLICATE_OF) and reason[len(DUPLICATE_OF):].isdigit()):
            break
        job_id = int(reason[len(DUPLICATE_OF):])
    return job_id


# Columns added after the first release, applied to existing databases on connect.
MIGRATIONS = {
    "jobs": {"ats_checked_at": "TEXT", "description_source": "TEXT"},
}


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn


def rescore_when_described(conn: sqlite3.Connection, job_id: int) -> None:
    """A job scored without its description goes back for scoring once the description arrives."""
    conn.execute("UPDATE jobs SET status = 'candidate' WHERE id = ? AND status = 'scored'", (job_id,))


def email_seen(conn: sqlite3.Connection, gm_msgid: str) -> bool:
    return conn.execute("SELECT 1 FROM emails WHERE gm_msgid = ?", (gm_msgid,)).fetchone() is not None


def record_email(conn: sqlite3.Connection, gm_msgid: str, source: str, sender: str,
                 subject: str, received_at: str, parsed_jobs: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO emails VALUES (?, ?, ?, ?, ?, ?, ?)",
        (gm_msgid, source, sender, subject, received_at, parsed_jobs, now()),
    )


def _merge_signals(existing: Optional[str], new: dict, location: Optional[str]) -> str:
    merged = json.loads(existing) if existing else {}
    for key, value in new.items():
        if isinstance(value, list):
            merged[key] = sorted(set(merged.get(key, [])) | set(value))
        else:
            merged[key] = value
    if location:
        merged["locations"] = sorted(set(merged.get("locations", [])) | {location})
    return json.dumps(merged, sort_keys=True)


def upsert_job(conn: sqlite3.Connection, job, gm_msgid: Optional[str] = None) -> Tuple[int, bool]:
    """Insert a parsed job, or merge this sighting into the existing row. Returns (job id, created)."""
    key = dedupe_key(job.company, job.title)
    # A listing seen before stays with its job even if the site renamed the company or title since.
    seen = conn.execute("SELECT job_id FROM sightings WHERE source = ? AND source_job_id = ?",
                        (job.source, job.source_job_id)).fetchone()
    if seen:
        found = seen["job_id"]
    else:
        hit = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
        found = hit["id"] if hit else None
    row = conn.execute("SELECT id, signals, salary_max, salary_source FROM jobs WHERE id = ?",
                       (_kept_copy(conn, found),)).fetchone() if found else None
    has_pay = job.salary_min is not None or job.salary_max is not None
    if row:
        job_id, created = row["id"], False
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?",
                     (_merge_signals(row["signals"], job.signals, job.location), job_id))
        # Employer-posted pay beats an estimate; any pay beats none.
        if has_pay and (row["salary_max"] is None or (row["salary_source"] != "posted" and job.salary_source == "posted")):
            conn.execute("UPDATE jobs SET salary_min = ?, salary_max = ?, salary_source = ? WHERE id = ?",
                         (job.salary_min, job.salary_max, job.salary_source, job_id))
    else:
        job_id, created = conn.execute(
            "INSERT INTO jobs (dedupe_key, title, company, location, salary_min, salary_max, salary_source, "
            "signals, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, job.title, job.company, job.location, job.salary_min, job.salary_max,
             job.salary_source if has_pay else None, _merge_signals(None, job.signals, job.location), now()),
        ).lastrowid, True
    conn.execute(
        "INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, job.source, job.source_job_id, job.url, gm_msgid, now()),
    )
    return job_id, created
