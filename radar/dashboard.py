"""Prepares Job Radar's dashboard data and reads back the decisions made on it.

The dashboard is a claude.ai page. Python can't reach it directly; Claude moves the data with the
claude-artifacts tools (the old ArtifactData tool is gone since Nov 6, 2026):

  1. ./jr decisions-since                    -> the `where` filter for the next step
  2. db_query  decisions  with that filter   -> Claude saves the result's document lines to a file
  3. ./jr save-decisions FILE                -> data/dashboard_decisions/j<id>.json, one per decision
  4. ./jr apply-decisions                    -> your clicks update job status
  5. ./jr dashboard-export                   -> data/dashboard/data.json (every job, list and the summary)
  6. publish_files  data.json                -> the page reads it next to index.html

Jobs and lists travel as a published file because publish_files sends it from disk without its bytes passing
through Claude; a row written with db_write has to be spelled out inline. Only your clicks stay in the page's
database (`decisions`). Clearing a choice saves {"decision": null} rather than deleting the row, so a read of
the rows changed since the last one also catches undone choices.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import ats
from .store import now

SOURCE_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "google": "Google Jobs",
                 "board": "Company board"}
BRIEF_ONLY_KEYS = {"why", "flags", "tier"}

# Status set by the dashboard decision, and the statuses a decision may overwrite.
DECISION_STATUS = {"apply": "queued", "maybe": "maybe", "skip": "skipped", "closed": "closed"}
DECIDABLE = ("scored", "queued", "packet_ready", "maybe", "skipped", "closed", "expired")
# Apply / Maybe / Skip never reopen a job Job Radar found closed or too old: the page hides those
# jobs, so any such click predates the closure. Only your own "Closed?" can be undone from the page.
REOPENABLE = ("scored", "queued", "packet_ready", "maybe", "skipped")
CLOSED_BY_YOU = "marked closed by you"
HIDDEN = ("closed", "expired")
# "Bring back" on the Filtered out tab: the job skips the rules and gets scored on the next run.
RESTORE = "restore"
# "Sent it" / "Not needed" on the Follow up tab. They land on jobs you've applied to, whose Apply click no longer matters.
FOLLOW_UP = ("followed_up", "no_follow_up")
FILTERED_DAYS = 14   # the Filtered out tab lists jobs first seen this recently (the posting-age limit)
LIST_BYTES_MAX = 600_000   # keeps data.json quick to load, phones included; long lists keep their newest entries
DATA_FILE = "data.json"   # published next to the page's index.html
LISTS_FILE = "lists.json"   # the Skipped and Filtered out lists; the page loads them after its first paint
SPLIT_LISTS = True    # Skipped and Filtered out go in lists.json, loaded after first paint
PULLED_AT = ".pulled_at"   # in the decisions folder: when save-decisions last ran
PULL_OVERLAP = timedelta(hours=1)   # re-read a little before the last pull, for clock drift between page and Mac


def _fit(entries: List[dict]) -> List[dict]:
    """Keep the leading (newest) entries whose combined JSON fits under LIST_BYTES_MAX."""
    size = 0
    for i, e in enumerate(entries):
        size += len(json.dumps(e, ensure_ascii=False).encode()) + 1
        if size > LIST_BYTES_MAX:
            return entries[:i]
    return entries


def _job_doc(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    signals = json.loads(r["signals"] or "{}")
    brief = json.loads(r["brief"] or "{}")
    # Sightings from `./jr search` carry a "search:..." gm_msgid; label them so they stand apart from alert emails.
    links = []
    for s in conn.execute("SELECT source, url, MAX(COALESCE(gm_msgid, '') NOT LIKE 'search:%') AS from_email, "
                          "MAX(COALESCE(gm_msgid, '') LIKE 'search:%') AS from_search FROM sightings "
                          "WHERE job_id = ? AND url IS NOT NULL GROUP BY source, url", (r["id"],)):
        label = SOURCE_LABELS.get(s["source"], s["source"])
        if s["from_search"]:
            label += " alert + search" if s["from_email"] else " search"
        links.append({"label": label, "url": s["url"]})
    research = {k: v for k, v in brief.items() if k not in BRIEF_ONLY_KEYS}
    pay = ({"min": r["salary_min"], "max": r["salary_max"], "source": r["salary_source"],
            "text": signals.pop("salary_text", None)} if (r["salary_min"] or r["salary_max"]) else None)
    research_pay = research.get("pay") if isinstance(research.get("pay"), dict) else {}
    if not pay and research_pay.get("range") and "unknown" not in str(research_pay["range"]).lower():
        source = str(research_pay.get("source", ""))
        pay = {"min": None, "max": None, "text": research_pay["range"],
               "source": "posted" if source.lower().startswith("posted") else "estimate"}
    return {
        "id": r["id"], "title": r["title"], "company": r["company"],
        "locations": signals.pop("locations", [r["location"]] if r["location"] else []),
        "work_setting": signals.pop("work_setting", None),
        "pay": pay,
        "score": r["score"], "verdict": r["verdict"], "tier": brief.get("tier"),
        "why": brief.get("why", ""), "flags": brief.get("flags", []),
        "signals": signals, "links": links, "apply_url": r["apply_url"], "ats": r["ats"],
        "sign_in": ats.sign_in(r["apply_url"], r["ats"]),
        "description_source": r["description_source"] if len(r["description"] or "") >= 500 else None,
        "status": r["status"], "applied_at": r["applied_at"], "first_seen": r["first_seen"], "posted_at": r["posted_at"],
        "brief": research if research.get("researched_at") else None,
    }


def _skipped_entry(doc: dict) -> dict:
    keep = ("id", "title", "company", "locations", "pay", "score", "tier", "why", "flags", "signals", "links", "apply_url")
    return {k: doc[k] for k in keep}


def _filtered_entry(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    doc = _job_doc(conn, r)
    entry = {k: doc[k] for k in ("id", "title", "company", "locations", "pay", "links", "first_seen")}
    entry["reason"] = r["filter_reason"]
    entry["sponsored"] = bool(doc["signals"].get("sponsored"))
    return entry


def export(conn: sqlite3.Connection, out_dir: Path, summary: dict) -> Path:
    """Write everything the page shows to one data.json for publish_files. Returns its path."""
    # Only our own files are replaced; anything else in the folder stays.
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs, skipped = [], []
    for r in conn.execute("SELECT * FROM jobs WHERE score IS NOT NULL ORDER BY score DESC"):
        if r["status"] in HIDDEN:   # closed or too old: counted in summary["hidden"], never shown
            continue
        doc = _job_doc(conn, r)
        # Claude's skips you haven't acted on live in one compact list; every other scored job gets a full card.
        if r["verdict"] == "skip" and r["status"] == "scored":
            skipped.append(_skipped_entry(doc))
        else:
            jobs.append(doc)
    keep_ids = {e["id"] for e in _fit(sorted(skipped, key=lambda e: -e["id"]))}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FILTERED_DAYS)).isoformat(timespec="seconds")
    filtered = _fit([_filtered_entry(conn, r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'filtered' AND first_seen >= ? ORDER BY first_seen DESC, id DESC", (cutoff,))])

    lists = {"skipped": [e for e in skipped if e["id"] in keep_ids], "filtered": filtered, "filtered_days": FILTERED_DAYS}
    path = out_dir / DATA_FILE
    if SPLIT_LISTS:
        (out_dir / LISTS_FILE).write_text(json.dumps(lists, ensure_ascii=False, default=str))
        head = {"summary": summary, "jobs": jobs, "lists_file": LISTS_FILE, "filtered_days": FILTERED_DAYS,
                "list_counts": {"skipped": len(lists["skipped"]), "filtered": len(filtered)}}
    else:
        head = {"summary": summary, "jobs": jobs, **lists}
    path.write_text(json.dumps(head, ensure_ascii=False, default=str))
    return path


def decisions_since(decisions_dir: Path) -> Optional[str]:
    """The `at` time from which to re-read decisions, or None when a full read is needed (no earlier pull)."""
    stamp = decisions_dir / PULLED_AT
    if not stamp.exists():
        return None
    pulled = datetime.fromisoformat(stamp.read_text().strip().replace("Z", "+00:00"))
    return (pulled - PULL_OVERLAP).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def save_decisions(decisions_dir: Path, text: str, full: bool = False) -> Dict[str, int]:
    """Store decision rows from a db_query / db_list result as j<id>.json files.

    `text` holds the result's document lines, one JSON object each ({"id", "data", ...}); other lines are ignored.
    A row whose decision was cleared removes its file. With full=True the rows replace every saved decision.
    A decision made in chat (its file's data has "local": true) stands until a newer click on the page: each read
    re-reads the last hour of clicks, and an older Apply must not undo a Skip made here since.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("id", "")).startswith("j") and isinstance(row.get("data"), dict):
            rows.append(row)
    if full and not rows:
        # An empty or garbled paste must not read as "every choice was cleared".
        raise SystemExit("No decision rows found in that file; nothing was changed.")
    decisions_dir.mkdir(parents=True, exist_ok=True)
    local = {}
    for f in decisions_dir.glob("j*.json"):
        try:
            data = json.loads(f.read_text()).get("data") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(data, dict) and data.get("local"):
            local[f.stem] = data
    if full:
        for f in decisions_dir.glob("j*.json"):
            f.unlink()
    counts = {"saved": 0, "cleared": 0, "kept_local": 0}
    for row in rows:
        f = decisions_dir / f"{row['id']}.json"
        mine = local.get(row["id"])
        if mine and str(mine.get("at") or "")[:19] >= str(row["data"].get("at") or "")[:19]:
            counts["kept_local"] += 1
            continue
        local.pop(row["id"], None)
        if row["data"].get("decision") in (*DECISION_STATUS, RESTORE, *FOLLOW_UP):
            f.write_text(json.dumps({"data": row["data"]}))
            counts["saved"] += 1
        else:
            if f.exists():
                f.unlink()
            counts["cleared"] += 1
    if full:   # put back the chat decisions no newer click replaced
        for stem, data in local.items():
            (decisions_dir / f"{stem}.json").write_text(json.dumps({"data": data}))
    (decisions_dir / PULLED_AT).write_text(now())
    return counts


def apply_decisions(conn: sqlite3.Connection, decisions_dir: Path) -> Dict[str, int]:
    """Turn the dashboard's Apply / Maybe / Skip / Bring back clicks into job statuses."""
    if not decisions_dir.exists():
        # The list step failed or never ran. Reading that as "every choice was cleared" would empty your queue.
        raise SystemExit(f"No decisions folder at {decisions_dir}: list the dashboard's decisions first.")
    decided = {}
    for f in decisions_dir.rglob("*.json"):
        try:
            body = json.loads(f.read_text())
        except ValueError:
            continue
        data = body.get("data", body)
        if f.stem.startswith("j") and f.stem[1:].isdigit() and data.get("decision") in (*DECISION_STATUS, RESTORE, *FOLLOW_UP):
            decided[int(f.stem[1:])] = (data["decision"], data.get("at") or now())
    counts = {"queued": 0, "maybe": 0, "skipped": 0, "undone": 0}
    counts["closed"] = counts["restored"] = counts["follow_ups"] = 0
    for job_id, (decision, at) in decided.items():
        if decision in FOLLOW_UP:
            from .followup import mark_follow_up
            counts["follow_ups"] += mark_follow_up(conn, job_id, decision, at)
            continue
        if decision == RESTORE:
            row = conn.execute("SELECT signals FROM jobs WHERE id = ? AND status = 'filtered'", (job_id,)).fetchone()
            if row:
                signals = {**json.loads(row["signals"] or "{}"), "restored_by_you": at}
                conn.execute("UPDATE jobs SET status = 'candidate', filter_reason = NULL, decided_at = ?, signals = ? "
                             "WHERE id = ?", (at, json.dumps(signals, sort_keys=True), job_id))
                counts["restored"] += 1
            continue
        status = DECISION_STATUS[decision]
        row = conn.execute("SELECT status, signals FROM jobs WHERE id = ?", (job_id,)).fetchone()
        built = json.loads(row["signals"] or "{}").get("packet") if row else None
        if built:
            # A built packet answers the Apply click. Any other choice moves the job only if made after the build
            # (a packet built from chat can predate no click, or follow an older Maybe).
            if status == "queued":
                status = "packet_ready"
            elif row["status"] == "packet_ready" and at[:19] <= built.get("built_at", "")[:19]:
                continue
        if decision == "closed":
            cur = conn.execute(f"UPDATE jobs SET status = ?, decided_at = ?, filter_reason = ? "
                               f"WHERE id = ? AND status IN ({','.join('?' * len(DECIDABLE))}) AND status != ?",
                               (status, at, CLOSED_BY_YOU, job_id, *DECIDABLE, status))
        else:
            cur = conn.execute(f"UPDATE jobs SET status = ?, decided_at = ?, "
                               f"filter_reason = CASE WHEN filter_reason = ? THEN NULL ELSE filter_reason END "
                               f"WHERE id = ? AND (status IN ({','.join('?' * len(REOPENABLE))}) "
                               f"OR (status = 'closed' AND filter_reason = ?)) AND status != ?",
                               (status, at, CLOSED_BY_YOU, job_id, *REOPENABLE, CLOSED_BY_YOU, status))
        counts[DECISION_STATUS[decision]] += cur.rowcount
    # A cleared choice on the page sends the job back to review (closures found by Job Radar itself stay closed).
    # A packet built from chat, with no click behind it, has no decided_at and stays.
    for r in conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'maybe', 'skipped') "
                          "OR (status = 'packet_ready' AND decided_at IS NOT NULL) "
                          "OR (status = 'closed' AND filter_reason = ?)", (CLOSED_BY_YOU,)).fetchall():
        if r["id"] not in decided:
            conn.execute("UPDATE jobs SET status = 'scored', decided_at = NULL, filter_reason = NULL WHERE id = ?", (r["id"],))
            counts["undone"] += 1
    conn.commit()
    return counts


def summary(conn: sqlite3.Connection, lookback_days: int) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]
    has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'applications'").fetchone()
    apps = dict(conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall()) if has_history else {}
    from .followup import outcomes
    radar = outcomes(conn)
    return {
        "applications": {"total": sum(apps.values()), **apps} if apps else None,
        "radar_apps": {"sent": radar["sent"], **radar["totals"]},
        # The page calls these "closed or posted over 2 weeks ago", so repeats of another job don't count.
        "hidden": one("SELECT COUNT(*) FROM jobs WHERE status IN ('closed', 'expired') AND score IS NOT NULL "
                      "AND COALESCE(filter_reason, '') NOT LIKE 'duplicate of job %'"),
        "updated_at": now(),
        "emails": one("SELECT COUNT(*) FROM emails WHERE source NOT IN ('linkedin_search', 'google_jobs', 'watchlist')"),
        "listings": one("SELECT COUNT(*) FROM sightings"),
        "unique": one("SELECT COUNT(*) FROM jobs"),
        "candidates": one("SELECT COUNT(*) FROM jobs WHERE status != 'filtered'"),
        "scored": one("SELECT COUNT(*) FROM jobs WHERE verdict IS NOT NULL"),
        "apply": one("SELECT COUNT(*) FROM jobs WHERE verdict = 'apply'"),
        "maybe": one("SELECT COUNT(*) FROM jobs WHERE verdict = 'maybe'"),
        "skip": one("SELECT COUNT(*) FROM jobs WHERE verdict = 'skip'"),
        "window": f"since {conn.execute('SELECT MIN(received_at) FROM emails').fetchone()[0][:10]}"
                  if one("SELECT COUNT(*) FROM emails") else "",
    }
