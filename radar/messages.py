"""Messages you send Claude from the dashboard, and Claude's replies.

The page saves each message in the dashboard database's `messages` collection as {text, at, job_id}; only the page's
owner can write there (the page's db rules). Each hourly run:
  1. ./jr messages-since            -> the db_query filter (or FULL) for messages sent since the last read
  2. ./jr save-messages FILE        -> store the rows that read returned
  3. ./jr messages --out FILE       -> the messages waiting for an answer, with each job's details and the thread so far
  4. ./jr save-replies FILE         -> store Claude's answers: [{id, reply, status}]
  5. ./jr dashboard-export          -> the replies go back to the page in data.json
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .store import now

STATUSES = ("answered", "needs_you")   # needs_you: the request needs you in Claude Code (apply, send, truth.md...)
MAX_TEXT = 4000         # the page allows 2,000 characters; anything longer is cut
READ_OVERLAP = timedelta(days=1)   # re-read a day before the newest saved message, for clock drift between devices
EXPORT_DAYS = 60        # replies older than this drop off the page
EXPORT_MAX = 200
THREAD_CONTEXT = 6      # earlier messages in the same thread that come with each waiting one


def since_filter(conn: sqlite3.Connection) -> Optional[list]:
    """The db_query filter for messages sent since the last read, or None for a full read (none saved yet)."""
    newest = conn.execute("SELECT MAX(sent_at) FROM messages").fetchone()[0]
    if not newest:
        return None
    try:
        t = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    value = (t - READ_OVERLAP).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return [{"field": "at", "op": ">=", "value": value}]


def _rows(text: str) -> List[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("id", "")).startswith("m") and isinstance(row.get("data"), dict):
            rows.append(row)
    return rows


def save_messages(conn: sqlite3.Connection, text: str) -> Dict[str, int]:
    """Store message rows from a db_query / db_list result. A message already saved is never changed."""
    counts = {"new": 0, "seen": 0, "skipped": 0}
    for row in _rows(text):
        data = row["data"]
        body = str(data.get("text") or "").strip()
        at = str(data.get("at") or "").strip()
        if not body or not at or len(row["id"]) > 64:
            counts["skipped"] += 1
            continue
        job_id = data.get("job_id")
        job_id = job_id if isinstance(job_id, int) and not isinstance(job_id, bool) else None
        cur = conn.execute("INSERT OR IGNORE INTO messages (id, job_id, text, sent_at, saved_at) VALUES (?, ?, ?, ?, ?)",
                           (row["id"], job_id, body[:MAX_TEXT], at, now()))
        counts["new" if cur.rowcount else "seen"] += 1
    conn.commit()
    return counts


def _job(conn: sqlite3.Connection, job_id: Optional[int]) -> Optional[dict]:
    if job_id is None:
        return None
    r = conn.execute("SELECT id, title, company, score, verdict, status, apply_url FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) if r else {"id": job_id, "missing": True}


def _thread(conn: sqlite3.Connection, m: sqlite3.Row) -> List[dict]:
    same = "job_id = ?" if m["job_id"] is not None else "job_id IS NULL"
    args = (m["job_id"],) if m["job_id"] is not None else ()
    rows = conn.execute(f"SELECT text, sent_at, reply, status FROM messages WHERE {same} AND sent_at < ? "
                        f"ORDER BY sent_at DESC LIMIT ?", (*args, m["sent_at"], THREAD_CONTEXT)).fetchall()
    return [dict(r) for r in reversed(rows)]


def waiting(conn: sqlite3.Connection) -> List[dict]:
    """Messages with no reply yet, oldest first, each with its job and the earlier messages in its thread."""
    out = []
    for m in conn.execute("SELECT * FROM messages WHERE status = 'new' ORDER BY sent_at, id").fetchall():
        out.append({"id": m["id"], "sent_at": m["sent_at"], "text": m["text"], "job": _job(conn, m["job_id"]),
                    "earlier": _thread(conn, m)})
    return out


def save_replies(conn: sqlite3.Connection, items: List[dict]) -> Dict[str, list]:
    """Store Claude's answers. Returns {"saved": [ids], "problems": ["id: why"]}."""
    saved, problems = [], []
    for it in items if isinstance(items, list) else []:
        mid, reply, status = str(it.get("id") or ""), str(it.get("reply") or "").strip(), it.get("status") or "answered"
        if not reply:
            problems.append(f"{mid or '?'}: empty reply")
        elif status not in STATUSES:
            problems.append(f"{mid}: status must be one of {', '.join(STATUSES)}")
        elif not conn.execute("UPDATE messages SET reply = ?, replied_at = ?, status = ?, alerted = 0 WHERE id = ?",
                              (reply[:MAX_TEXT], now(), status, mid)).rowcount:
            problems.append(f"{mid}: no such message (run ./jr save-messages first)")
        else:
            saved.append(mid)
    conn.commit()
    return {"saved": saved, "problems": problems}


def export(conn: sqlite3.Connection) -> List[dict]:
    """The messages the page shows, newest first: the text, and the reply once there is one."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=EXPORT_DAYS)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT id, job_id, text, sent_at, reply, replied_at, status FROM messages "
                        "WHERE sent_at >= ? OR replied_at >= ? ORDER BY sent_at DESC LIMIT ?",
                        (cutoff, cutoff, EXPORT_MAX)).fetchall()
    return [{"id": r["id"], "job_id": r["job_id"], "text": r["text"], "at": r["sent_at"], "reply": r["reply"],
             "replied_at": r["replied_at"], "status": r["status"]} for r in rows]


def alert_count(conn: sqlite3.Connection, mark: bool = True) -> int:
    """Replies not yet mentioned in a phone alert. With mark, they're marked as mentioned."""
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE reply IS NOT NULL AND alerted = 0").fetchone()[0]
    if n and mark:
        conn.execute("UPDATE messages SET alerted = 1 WHERE reply IS NOT NULL AND alerted = 0")
    return n
