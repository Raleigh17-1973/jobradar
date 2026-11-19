"""Your application history, rebuilt from Gmail: confirmations, rejections, interviews and offers.

  ./jr history-collect          pull candidate emails (headers + a text excerpt) into history_emails
  ./jr history-export           unclassified emails as JSON for Claude to classify
  ./jr history-save FILE        store Claude's classifications, rebuild `applications`, match jobs

Each email is classified by Claude (wording differs across Workday, Greenhouse, recruiters...), and the
`applications` table is rebuilt from all classified emails every time, so a correction anywhere just re-runs.
"""
from __future__ import annotations

import email.policy
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parseaddr, parsedate_to_datetime
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from .store import _COMPANY_SUFFIXES, _norm, dedupe_key, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS history_emails (
  gm_msgid      TEXT PRIMARY KEY,
  sender        TEXT,
  subject       TEXT,
  received_at   TEXT,
  excerpt       TEXT,
  kind          TEXT,     -- application | rejection | interview | assessment | offer | withdrawn | recruiter | other
  company       TEXT,
  role          TEXT,
  req_id        TEXT,
  note          TEXT,
  classified_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
  id          INTEGER PRIMARY KEY,
  company     TEXT NOT NULL,
  company_key TEXT NOT NULL,
  role        TEXT,
  role_key    TEXT NOT NULL DEFAULT '',   -- '' when an email named the company but not the role
  req_id      TEXT,
  applied_at  TEXT,
  status      TEXT NOT NULL,              -- applied | interview | offer | rejected | withdrawn
  status_at   TEXT,
  events      TEXT,                       -- JSON list of {date, kind, subject}
  UNIQUE (company_key, role_key)
);
"""

# Searches that find application-related mail. Alert digests are excluded; Claude sorts out the rest.
QUERIES = [
    '(subject:(applying OR application OR applied OR "thank you for your interest") OR "we received your application" '
    'OR "thank you for applying")',
    '("unfortunately" OR "not moving forward" OR "move forward with other" OR "other candidates" OR "decided to pursue" '
    'OR "not be moving forward" OR "position has been filled") (application OR candidacy OR position OR role)',
    '(interview OR "phone screen" OR "next steps" OR "offer letter") (application OR position OR role) -category:promotions',
    # Recruiter scheduling notes often say none of those words (e.g. "Interview - <title> - <Company>",
    # "I'd like to schedule a call for this week"). No -category:promotions here: Gmail filed that iCIMS note
    # (it has an Unsubscribe link) under Promotions.
    '(subject:(interview OR "phone screen") OR "schedule a call" OR "schedule a time" OR "set up a call" '
    'OR "set up a time" OR "your availability")',
]
EXCLUDE = ("-from:(jobalerts-noreply@linkedin.com OR jobs-listings@linkedin.com OR donotreply@jobalert.indeed.com "
           "OR donotreply@match.indeed.com OR info@glassdoor.com OR substack.com)")
EXCERPT_CHARS = 1200
KIND_TO_STATUS = {"application": "applied", "assessment": "interview", "interview": "interview",
                  "offer": "offer", "rejection": "rejected", "withdrawn": "withdrawn"}
STATUS_RANK = {"applied": 0, "interview": 1, "offer": 2, "rejected": 3, "withdrawn": 3}
NO_RESPONSE_DAYS = 30


def connect_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def company_key(company: str) -> str:
    return _norm(_COMPANY_SUFFIXES.sub("", re.sub(r"^the\s+", "", company or "", flags=re.I)))


def loose_company_key(key: str) -> str:
    """Drop a trailing "ai" so "Northwind AI" finds mail from "Northwind"; used only when the exact key finds nothing."""
    words = key.split()
    return " ".join(words[:-1]) if len(words) > 1 and words[-1] == "ai" else key


def role_key(role: Optional[str]) -> str:
    return dedupe_key("", role or "").split("|", 1)[1] if role else ""


def _excerpt(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()[:EXCERPT_CHARS]


def collect(conn: sqlite3.Connection, gm, days: int) -> int:
    """Store new candidate emails from the last `days` days. Returns how many were added."""
    connect_schema(conn)
    uids = sorted({u for q in QUERIES for u in gm.search(f"{q} {EXCLUDE} newer_than:{days}d")}, key=int)
    ids = gm.gm_msgids(uids)
    new = [u for u in uids if ids.get(u) and not conn.execute(
        "SELECT 1 FROM history_emails WHERE gm_msgid = ?", (ids[u],)).fetchone()]
    added = 0
    for msg in gm.fetch(new):
        m = msg.parsed()
        name, addr = parseaddr(m["From"])
        received = parsedate_to_datetime(m["Date"]).isoformat() if m["Date"] else None
        conn.execute("INSERT OR IGNORE INTO history_emails (gm_msgid, sender, subject, received_at, excerpt) "
                     "VALUES (?, ?, ?, ?, ?)", (msg.gm_msgid, f"{name} <{addr}>", m["Subject"] or "", received, _excerpt(m)))
        added += 1
    conn.commit()
    return added


def export_unclassified(conn: sqlite3.Connection) -> List[dict]:
    connect_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT gm_msgid, sender, subject, received_at, excerpt FROM history_emails "
        "WHERE classified_at IS NULL ORDER BY received_at")]


def save_classifications(conn: sqlite3.Connection, items: List[dict]) -> int:
    connect_schema(conn)
    for it in items:
        conn.execute("UPDATE history_emails SET kind = ?, company = ?, role = ?, req_id = ?, note = ?, classified_at = ? "
                     "WHERE gm_msgid = ?", (it.get("kind"), it.get("company"), it.get("role"), it.get("req_id"),
                                           it.get("note"), now(), it["gm_msgid"]))
    conn.commit()
    return len(items)


def rebuild_applications(conn: sqlite3.Connection) -> Dict[str, int]:
    """Replay every classified event in date order into one row per company + role."""
    connect_schema(conn)
    conn.execute("DELETE FROM applications")
    apps: Dict[tuple, dict] = {}
    events = conn.execute("SELECT * FROM history_emails WHERE kind IN ({}) AND company IS NOT NULL AND company != '' "
                          "ORDER BY received_at".format(",".join("?" * len(KIND_TO_STATUS))), list(KIND_TO_STATUS)).fetchall()
    for e in events:
        ck, rk = company_key(e["company"]), role_key(e["role"])
        if not rk:
            # A rejection that names no role belongs to the one open application at that company, if there is one.
            open_apps = [k for k, a in apps.items() if k[0] == ck and a["status"] in ("applied", "interview")]
            if len(open_apps) == 1:
                rk = open_apps[0][1]
        app = apps.setdefault((ck, rk), {"company": e["company"], "role": e["role"], "req_id": e["req_id"],
                                         "applied_at": None, "status": "applied", "status_at": None, "events": []})
        status = KIND_TO_STATUS[e["kind"]]
        if e["kind"] == "application" and not app["applied_at"]:
            app["applied_at"] = e["received_at"]
        if STATUS_RANK[status] >= STATUS_RANK[app["status"]] or e["kind"] != "application":
            app["status"], app["status_at"] = status, e["received_at"]
        app["role"] = app["role"] or e["role"]
        app["req_id"] = app["req_id"] or e["req_id"]
        app["events"].append({"date": (e["received_at"] or "")[:10], "kind": e["kind"], "subject": e["subject"][:120],
                              "note": e["note"] or None})
    for (ck, rk), a in apps.items():
        conn.execute("INSERT INTO applications (company, company_key, role, role_key, req_id, applied_at, status, status_at, events) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (a["company"], ck, a["role"], rk, a["req_id"], a["applied_at"],
                                                             a["status"], a["status_at"], json.dumps(a["events"])))
    conn.commit()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    stale = (datetime.now(timezone.utc) - timedelta(days=NO_RESPONSE_DAYS)).isoformat()
    counts["no response 30+ days"] = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'applied' AND COALESCE(status_at, applied_at) < ?", (stale,)).fetchone()[0]
    return counts


def _days_apart(a: str, b: str) -> float:
    try:
        da, db = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
        if da.tzinfo is None:
            da = da.replace(tzinfo=timezone.utc)
        if db.tzinfo is None:
            db = db.replace(tzinfo=timezone.utc)
        return abs((da - db).total_seconds()) / 86400
    except ValueError:
        return 999.0


def _label(app: sqlite3.Row) -> str:
    date = (app["status_at"] or app["applied_at"] or "")[:10]
    return {"applied": f"applied {(app['applied_at'] or date)[:10]}", "interview": f"interviewing (last {date})",
            "offer": f"offer {date}", "rejected": f"rejected {date}", "withdrawn": f"withdrew {date}"}[app["status"]]


def match_jobs(conn: sqlite3.Connection) -> Dict[str, int]:
    """Attach your history to every job: same role (you've applied) or same company (other roles)."""
    connect_schema(conn)
    apps = conn.execute("SELECT * FROM applications").fetchall()
    by_company: Dict[str, list] = {}
    by_loose: Dict[str, list] = {}
    for a in apps:
        by_company.setdefault(a["company_key"], []).append(a)
        by_loose.setdefault(loose_company_key(a["company_key"]), []).append(a)
    mail_for = lambda key: by_company.get(key) or by_loose.get(loose_company_key(key), [])
    # Applications made through Job Radar count even when no confirmation email arrived (ADP often sends none), so a
    # repost under a new title doesn't read "no applications on record". One an email covers is skipped.
    radar_company: Dict[str, list] = {}
    radar_loose: Dict[str, list] = {}
    for j in conn.execute("SELECT id, company, title, applied_at FROM jobs WHERE applied_at IS NOT NULL").fetchall():
        ck, rk = company_key(j["company"]), role_key(j["title"])
        if any((a["role_key"] and SequenceMatcher(None, rk, a["role_key"]).ratio() >= 0.85)
               or (a["applied_at"] and _days_apart(a["applied_at"], j["applied_at"]) <= 3) for a in mail_for(ck)):
            continue
        a = {"job_id": j["id"], "company": j["company"], "company_key": ck, "role": j["title"], "role_key": rk,
             "req_id": None, "applied_at": j["applied_at"], "status": "applied", "status_at": None, "events": "[]"}
        radar_company.setdefault(ck, []).append(a)
        radar_loose.setdefault(loose_company_key(ck), []).append(a)
    counts = {"same_role": 0, "same_company": 0}
    for job in conn.execute("SELECT id, company, title, signals, status, applied_at FROM jobs").fetchall():
        signals = json.loads(job["signals"] or "{}")
        signals.pop("history", None)
        key = company_key(job["company"])
        company_apps = mail_for(key)
        radar_apps = [a for a in radar_company.get(key) or radar_loose.get(loose_company_key(key), [])
                      if a["job_id"] != job["id"]]
        if company_apps or radar_apps:
            want = role_key(job["title"])
            same = [a for a in company_apps if a["role_key"] and SequenceMatcher(None, want, a["role_key"]).ratio() >= 0.85]
            if not same and job["applied_at"]:
                # Confirmation emails often word the title differently. A job you applied to through Job Radar takes
                # the company's one application dated within 3 days of yours.
                near = [a for a in company_apps if a["applied_at"] and _days_apart(a["applied_at"], job["applied_at"]) <= 3]
                same = near if len(near) == 1 else []
            if not same:
                # Only an identical title makes a Job Radar application the same role: Globex lists several Staff
                # TPM roles that differ by a suffix (", Security"), and a near match would hide them as applied.
                same = [a for a in radar_apps if a["role_key"] == want]
            company_apps = list(company_apps) + radar_apps
            others = [a for a in company_apps if a not in same]
            signals["history"] = {
                "same_role": _label(same[0]) if same else None,
                "company_apps": len(company_apps),
                "company_summary": "; ".join(f"{a['role'] or 'a role'}: {_label(a)}" for a in others[:4]) or None,
            }
            if same:
                # Structured outcome for follow-ups, interview prep and the outcomes report. For a job applied to
                # through Job Radar, only mail from that application on counts (a repost can share an old rejection).
                events = json.loads(same[0]["events"] or "[]")
                if job["applied_at"]:
                    since = job["applied_at"][:10]
                    events = [e for e in events if e.get("date", "") >= since or _days_apart(e.get("date") or "1970-01-01", since) <= 1]
                status, status_at = "applied", None
                for e in events:
                    s = KIND_TO_STATUS.get(e.get("kind"))
                    if s and (STATUS_RANK[s] >= STATUS_RANK[status] or e.get("kind") != "application"):
                        status, status_at = s, e.get("date")
                signals["history"].update({"status": status, "status_at": status_at, "events": events[-6:]})
            counts["same_role" if same else "same_company"] += 1
        conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job["id"]))
    conn.commit()
    return counts
