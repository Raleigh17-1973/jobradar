"""After you apply: outcomes, follow-up notes, interview prep, referral asks and phone alerts.

  ./jr outcomes                       how your Job Radar applications are doing, by score, source, system and setting
  ./jr followups-due [--out FILE]     applied 7+ days ago with no reply: context for Claude to draft a follow-up
  ./jr followups-save FILE            check the drafts (writing rules + claim check) and store them in signals.follow_up
  ./jr prep-due                       interviews that have no prep brief yet (or a newer round than the brief)
  ./jr prep-context ID                context for interview_prep.md, written into the job's application folder
  ./jr prep-save ID                   check that folder's interview_prep.md and attach it to the job
  ./jr referrals-due [--out FILE]     good open jobs where you know someone: context for a referral ask
  ./jr referrals-save FILE            check and store the referral asks in signals.referral
  ./jr alerts                         one line for a phone notification about what's new since the last one

Claude writes every note and brief (prompts/followup.md, prompts/interview_prep.md); this module picks the jobs,
gathers the context, checks what comes back and keeps the state. Nothing here sends a message: the dashboard shows
each draft with a Copy button and you send it yourself.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from . import config
from .config import ROOT
from .store import now

FOLLOW_UP_AFTER_DAYS = 7      # first nudge a week after applying
FOLLOW_UP_WINDOW_DAYS = 21    # past three weeks a follow-up note no longer helps much
PER_RUN = 5                   # drafts per run, so a run never spends long on them
NOTE_MAX = 300                # a LinkedIn connection note
MESSAGE_MAX = 900             # an email or LinkedIn message
PREP_TEXT_MAX = 14_000        # what the dashboard carries of each interview brief
ALERT_MAX = 200               # phones cut notifications off around here
STRONG_SCORE = 80
FRESH_DAYS = 2
ALERT_STATE = ROOT / "data" / "alerts_state.json"
APPLICATIONS = config.setting_path("applications", "applications")
FOLDER_BASE = APPLICATIONS.parent      # saved packet and prep paths are relative to this
OUTCOME_LABEL = {"applied": "waiting", "interview": "interview", "offer": "offer", "rejected": "rejected",
                 "withdrawn": "withdrawn", None: "no confirmation email"}


# ---------- helpers ----------

def _dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _days_since(iso: Optional[str]) -> Optional[float]:
    d = _dt(iso)
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400 if d else None


def _signals(r: sqlite3.Row) -> dict:
    return json.loads(r["signals"] or "{}")


def _brief(r: sqlite3.Row) -> dict:
    return json.loads(r["brief"] or "{}")


def _save_signals(conn: sqlite3.Connection, job_id: int, signals: dict) -> None:
    conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job_id))


def _people_search(company: str, words: str = "recruiter") -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} {words}")


def _folder(r: sqlite3.Row) -> Path:
    from .packet import folder_for
    built = (_signals(r).get("packet") or {}).get("folder")
    return FOLDER_BASE / built if built else folder_for(dict(r))


def _cover_text(folder: Path) -> Optional[str]:
    f = folder / "cover.json"
    if not f.exists():
        return None
    try:
        return "\n\n".join(json.loads(f.read_text()).get("paragraphs", []))
    except ValueError:
        return None


def _check(text: str) -> List[str]:
    """Your writing rules plus the claim check: every number and risky term must come from truth.md or the master."""
    from .packet import claim_check, lint
    return lint(text, "letter") + claim_check(text)


# ---------- outcomes ----------

def _source(conn: sqlite3.Connection, job_id: int) -> str:
    s = conn.execute("SELECT source, gm_msgid FROM sightings WHERE job_id = ? ORDER BY seen_at LIMIT 1", (job_id,)).fetchone()
    if not s:
        return "added by hand"
    if s["source"] == "linkedin" and (s["gm_msgid"] or "").startswith("search:"):
        return "LinkedIn search"
    return {"linkedin": "LinkedIn alert", "indeed": "Indeed", "glassdoor": "Glassdoor", "google": "Google Jobs",
            "board": "Company board", "recruiter": "Recruiter"}.get(s["source"], s["source"])


def applied_jobs(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE status = 'applied' AND applied_at IS NOT NULL ORDER BY applied_at").fetchall()


def outcome_of(r: sqlite3.Row) -> Optional[str]:
    return (_signals(r).get("history") or {}).get("status")


def outcomes(conn: sqlite3.Connection) -> dict:
    """Every application sent through Job Radar, its outcome so far, and outcome rates by a few traits."""
    rows = applied_jobs(conn)
    dims = {"Score": lambda r: "80+" if (r["score"] or 0) >= 80 else "70–79" if (r["score"] or 0) >= 70 else "under 70",
            "Found through": lambda r: _source(conn, r["id"]),
            "Applicant system": lambda r: r["ats"] or "unknown",
            "Work setting": lambda r: _setting(r),
            "Pay posted": lambda r: "yes" if r["salary_source"] == "posted" else "no",
            "Age when applied": lambda r: _age_band(r)}
    table: Dict[str, Dict[str, Dict[str, int]]] = {d: defaultdict(lambda: defaultdict(int)) for d in dims}
    totals: Dict[str, int] = defaultdict(int)
    for r in rows:
        label = OUTCOME_LABEL.get(outcome_of(r), "waiting")
        totals[label] += 1
        for d, fn in dims.items():
            table[d][fn(r)]["sent"] += 1
            table[d][fn(r)][label] += 1
    waiting_long = sum(1 for r in rows if outcome_of(r) in (None, "applied") and (_days_since(r["applied_at"]) or 0) >= 14)
    return {"sent": len(rows), "totals": dict(totals), "waiting_14_days": waiting_long,
            "by": {d: {k: dict(v) for k, v in t.items()} for d, t in table.items()}}


def _setting(r: sqlite3.Row) -> str:
    text = " ".join(str(x) for x in (r["work_mode"], _signals(r).get("work_setting"), r["location"]) if x).lower()
    return "remote" if "remote" in text else "hybrid" if "hybrid" in text else "on-site" if ("on-site" in text or "onsite" in text) else "not stated"


def _age_band(r: sqlite3.Row) -> str:
    posted = _dt(r["posted_at"]) or _dt(r["first_seen"])
    applied = _dt(r["applied_at"])
    if not posted or not applied:
        return "unknown"
    days = (applied - posted).days
    return "0–2 days" if days <= 2 else "3–7 days" if days <= 7 else "8+ days"


def outcomes_text(report: dict) -> str:
    t = report["totals"]
    lines = [f"Applications sent through Job Radar: {report['sent']}. "
             + ", ".join(f"{n} {k}" for k, n in sorted(t.items(), key=lambda kv: -kv[1]))
             + f". {report['waiting_14_days']} have waited 14+ days with no reply."]
    cols = ["sent", "interview", "rejected", "waiting", "no confirmation email"]
    for d, groups in report["by"].items():
        lines.append(f"\n{d}:")
        for k, c in sorted(groups.items(), key=lambda kv: -kv[1]["sent"]):
            lines.append(f"  {k:<22} " + "  ".join(f"{c.get(col, 0):>3} {col}" for col in cols))
    return "\n".join(lines)


# ---------- follow-ups ----------

def followups_due(conn: sqlite3.Connection, limit: int = PER_RUN) -> List[dict]:
    """Applied 7–21 days ago, no reply of any kind, no follow-up drafted yet. Oldest first."""
    out = []
    for r in applied_jobs(conn):
        s = _signals(r)
        age = _days_since(r["applied_at"]) or 0
        if s.get("follow_up") or not (FOLLOW_UP_AFTER_DAYS <= age <= FOLLOW_UP_WINDOW_DAYS):
            continue
        if outcome_of(r) not in (None, "applied"):
            continue
        b = _brief(r)
        folder = _folder(r)
        out.append({
            "id": r["id"], "company": r["company"], "title": r["title"], "applied_at": r["applied_at"][:10],
            "days_since_applied": int(age), "applied_through": r["ats"], "apply_url": r["apply_url"],
            "why_it_fits": b.get("why"), "role_needs": b.get("role_needs"), "proof_points": b.get("proof_points"),
            "company_note": b.get("company"),
            "people_you_know": ((s.get("network") or {}).get("people") or [])[:4],
            "recruiter_search": _people_search(r["company"]),
            "your_cover_letter": _cover_text(folder),
        })
        if len(out) >= limit:
            break
    return out


def save_followups(conn: sqlite3.Connection, items: List[dict]) -> Dict[str, List[str]]:
    """Store each clean draft; return the problems found in the others (fix and save again)."""
    problems: Dict[str, List[str]] = {}
    for it in items:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (it["id"],)).fetchone()
        if not r:
            problems[str(it["id"])] = ["no such job"]
            continue
        note, message = (it.get("linkedin_note") or "").strip(), (it.get("message") or "").strip()
        issues = _check(note + "\n" + message)
        if not message:
            issues.append("message is empty")
        if len(note) > NOTE_MAX:
            issues.append(f"LinkedIn note is {len(note)} characters (limit {NOTE_MAX})")
        if len(message) > MESSAGE_MAX:
            issues.append(f"message is {len(message)} characters (limit {MESSAGE_MAX})")
        if issues:
            problems[f"{r['id']} {r['company']}"] = issues
            continue
        s = _signals(r)
        s["follow_up"] = {"status": "drafted", "drafted_at": now(), "applied_at": r["applied_at"],
                          "contact": it.get("contact") or None, "contact_hint": it.get("contact_hint") or None,
                          "recruiter_search": _people_search(r["company"]),
                          "linkedin_note": note or None, "message": message, "alerted": False}
        _save_signals(conn, r["id"], s)
    conn.commit()
    return problems


def mark_follow_up(conn: sqlite3.Connection, job_id: int, decision: str, at: str) -> bool:
    """Your Sent it / Not needed click on the dashboard."""
    r = conn.execute("SELECT signals FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        return False
    s = _signals(r)
    fu = s.get("follow_up")
    if not fu:
        return False
    status = {"followed_up": "sent", "no_follow_up": "not needed"}[decision]
    if fu.get("status") == status and fu.get("decided_at") == at:
        return False
    fu.update({"status": status, "decided_at": at})
    _save_signals(conn, job_id, s)
    return True


# ---------- interview prep ----------

def _interview_events(r: sqlite3.Row) -> List[dict]:
    return [e for e in ((_signals(r).get("history") or {}).get("events") or []) if e.get("kind") in ("interview", "assessment", "offer")]


def prep_due(conn: sqlite3.Connection) -> Dict[str, list]:
    """Linked jobs at the interview stage without a brief (or with a newer round than the brief), plus recent
    interview emails for applications that have no job on file."""
    due, linked_keys = [], set()
    from .history import company_key
    for r in conn.execute("SELECT * FROM jobs WHERE signals LIKE '%\"history\"%'").fetchall():
        s = _signals(r)
        h = s.get("history") or {}
        if h.get("status") not in ("interview", "offer"):
            continue
        linked_keys.add(company_key(r["company"]))
        events = _interview_events(r)
        last = max((e.get("date") or "" for e in events), default="")
        prep = s.get("interview_prep") or {}
        if not prep or last > (prep.get("built_at") or "")[:10]:
            due.append({"id": r["id"], "company": r["company"], "title": r["title"], "latest_event": last,
                        "notes": [e.get("note") for e in events if e.get("note")]})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    unlinked = [dict(company=a["company"], role=a["role"], status=a["status"], at=(a["status_at"] or "")[:10])
                for a in conn.execute("SELECT * FROM applications WHERE status IN ('interview', 'offer') AND status_at >= ?",
                                      (cutoff,)).fetchall() if a["company_key"] not in linked_keys]
    return {"due": due, "no_job_on_file": unlinked}


def prep_context(conn: sqlite3.Connection, job_id: int) -> Path:
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        raise SystemExit(f"No job {job_id}.")
    s, b = _signals(r), _brief(r)
    folder = _folder(r)
    folder.mkdir(parents=True, exist_ok=True)
    packet_files = {}
    for name in ("resume.json", "cover.json", "answers.md"):
        f = folder / name
        if f.exists():
            packet_files[name] = f.read_text()[:12_000]
    context = {
        "id": r["id"], "company": r["company"], "title": r["title"], "applied_at": r["applied_at"],
        "interview_events": _interview_events(r), "pay": {"min": r["salary_min"], "max": r["salary_max"],
                                                          "source": r["salary_source"]},
        "salary_answer": (b.get("pay") or {}).get("salary_answer") if isinstance(b.get("pay"), dict) else None,
        "work_setting": s.get("work_setting"), "locations": s.get("locations"),
        "people_you_know": (s.get("network") or {}).get("people"),
        "research": b, "description": r["description"] or "(no description on file: say so in the brief)",
        "what_you_sent": packet_files,
    }
    path = folder / "prep_context.json"
    path.write_text(json.dumps(context, indent=1, ensure_ascii=False))
    return path


def save_prep(conn: sqlite3.Connection, job_id: int) -> List[str]:
    from .packet import claim_check
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        raise SystemExit(f"No job {job_id}.")
    folder = _folder(r)
    f = folder / "interview_prep.md"
    if not f.exists():
        raise SystemExit(f"Write {f} first (prompts/interview_prep.md).")
    text = f.read_text()
    issues = claim_check(text)
    if issues:
        return issues
    s = _signals(r)
    s["interview_prep"] = {"built_at": now(), "path": str(f.relative_to(FOLDER_BASE)), "text": text[:PREP_TEXT_MAX],
                           "alerted": False}
    _save_signals(conn, job_id, s)
    conn.commit()
    return []


# ---------- referral asks ----------

REFERRAL_REPEAT_DAYS = 30     # one ask per company a month, however many of its roles are open


def referrals_due(conn: sqlite3.Connection, limit: int = PER_RUN) -> List[dict]:
    """Open jobs worth pursuing where you know someone, one ask per company, best score first.

    A company's other open roles ride along in also_open, so one message covers them all. Companies asked in the
    last REFERRAL_REPEAT_DAYS are skipped."""
    from .history import company_key
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REFERRAL_REPEAT_DAYS)).isoformat()
    asked = set()
    for r in conn.execute("SELECT company, signals FROM jobs WHERE signals LIKE '%\"referral\"%'").fetchall():
        ref = _signals(r).get("referral") or {}
        if (ref.get("drafted_at") or "") >= cutoff:
            asked.add(company_key(r["company"]))
    out: List[dict] = []
    by_company: Dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM jobs WHERE (status IN ('queued', 'packet_ready', 'maybe') "
                          "OR (status = 'scored' AND verdict = 'apply')) AND signals LIKE '%\"network\"%' "
                          "ORDER BY score DESC").fetchall():
        s = _signals(r)
        people = (s.get("network") or {}).get("people") or []
        key = company_key(r["company"])
        if not people or s.get("referral") or key in asked:
            continue
        if key in by_company:
            by_company[key]["also_open"].append({"id": r["id"], "title": r["title"], "score": r["score"],
                                                 "apply_url": r["apply_url"]})
            continue
        if len(out) >= limit:
            continue
        b = _brief(r)
        item = {"id": r["id"], "company": r["company"], "title": r["title"], "score": r["score"],
                "status": r["status"], "apply_url": r["apply_url"], "why_it_fits": b.get("why"),
                "proof_points": b.get("proof_points"), "people_you_know": people[:4], "also_open": []}
        by_company[key] = item
        out.append(item)
    return out


def save_referrals(conn: sqlite3.Connection, items: List[dict]) -> Dict[str, List[str]]:
    """Store each ask on its job. Roles listed in also_open get a pointer to it, so they aren't asked about again."""
    problems: Dict[str, List[str]] = {}
    for it in items:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (it["id"],)).fetchone()
        if not r:
            problems[str(it["id"])] = ["no such job"]
            continue
        message = (it.get("message") or "").strip()
        issues = _check(message) + ([] if message else ["message is empty"])
        if len(message) > MESSAGE_MAX:
            issues.append(f"message is {len(message)} characters (limit {MESSAGE_MAX})")
        if not (it.get("contact") or {}).get("name"):
            issues.append("contact.name is missing (pick one of people_you_know)")
        if issues:
            problems[f"{r['id']} {r['company']}"] = issues
            continue
        s = _signals(r)
        s["referral"] = {"drafted_at": now(), "contact": it["contact"], "message": message, "alerted": False}
        _save_signals(conn, r["id"], s)
        for other in it.get("also_open") or []:
            oid = other.get("id") if isinstance(other, dict) else other
            o = conn.execute("SELECT * FROM jobs WHERE id = ?", (oid,)).fetchone()
            if not o or o["id"] == r["id"]:
                continue
            so = _signals(o)
            so["referral"] = {"drafted_at": now(), "contact": it["contact"], "covered_by": r["id"], "alerted": True}
            _save_signals(conn, o["id"], so)
    conn.commit()
    return problems


# ---------- phone alerts ----------

def _alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE.read_text())
    except (OSError, ValueError):
        return {"apps": []}


def alerts(conn: sqlite3.Connection, mark: bool = True) -> Optional[str]:
    """One line about what's new since the last alert: interview news, strong fresh jobs, drafts ready.
    Everything mentioned is marked so it's never announced twice."""
    state = _alert_state()
    seen_apps = set(state.get("apps", []))
    news, strong, drafts, preps, updates = [], [], [], [], []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for a in conn.execute("SELECT * FROM applications WHERE status IN ('interview', 'offer') AND status_at >= ?", (cutoff,)):
        key = f"{a['company_key']}|{a['role_key']}|{a['status']}|{(a['status_at'] or '')[:10]}"
        if key not in seen_apps:
            news.append(("Offer" if a["status"] == "offer" else "Interview news") + f" from {a['company']}")
            seen_apps.add(key)
    for r in conn.execute("SELECT * FROM jobs WHERE status = 'scored' AND verdict = 'apply' ORDER BY score DESC").fetchall():
        s, b = _signals(r), _brief(r)
        if s.get("alerted_at") or not (b.get("tier") == "strong" or (r["score"] or 0) >= STRONG_SCORE):
            continue
        age = s.get("age_days")
        if age is None:
            age = _days_since(r["posted_at"] or r["first_seen"]) or 0
        if age > FRESH_DAYS:
            continue
        strong.append(f"{r['company']}: {r['title']} ({r['score']})")
        s["alerted_at"] = now()
        updates.append((r["id"], s))
    pending = dict(updates)   # a strong job may also carry a referral draft: keep both changes on one copy
    for r in conn.execute("SELECT * FROM jobs WHERE signals LIKE '%\"alerted\": false%'").fetchall():
        s = pending.get(r["id"]) or _signals(r)
        changed = False
        for key, bucket in (("follow_up", drafts), ("referral", drafts), ("interview_prep", preps)):
            item = s.get(key)
            if isinstance(item, dict) and item.get("alerted") is False:
                bucket.append(r["company"])
                item["alerted"] = True
                changed = True
        if changed:
            pending[r["id"]] = s
    updates = list(pending.items())
    parts = list(news)
    if strong:
        parts.append(f"{len(strong)} strong new job{'s' if len(strong) > 1 else ''}: " + "; ".join(strong[:2])
                     + ("…" if len(strong) > 2 else ""))
    if preps:
        parts.append("Interview prep ready: " + ", ".join(dict.fromkeys(preps)))
    if drafts:
        parts.append(f"{len(drafts)} follow-up/referral note{'s' if len(drafts) > 1 else ''} to send")
    if not parts:
        return None
    message = "Job Radar: " + ". ".join(parts) + "."
    if len(message) > ALERT_MAX:
        message = message[:ALERT_MAX - 1].rsplit(" ", 1)[0] + "…"
    if mark:
        for job_id, s in updates:
            _save_signals(conn, job_id, s)
        conn.commit()
        state["apps"] = sorted(seen_apps)[-500:]
        ALERT_STATE.write_text(json.dumps(state))
    return message
