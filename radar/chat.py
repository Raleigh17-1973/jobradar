"""Dashboard chat: talk with Claude from the dashboard's Chat tab (prompts/chat.md).

A scheduled task checks the page's `messages` every few minutes (settings.yaml `chat.check_minutes`). When one is
waiting, that session answers it and stays: it checks again every 20 seconds while you're talking, then less often,
and ends once the chat has been quiet for `chat.quiet_minutes`. Replies go to the page's `replies` collection, so
they show up without a refresh, and the session keeps `chat/status` current so the page can say whether Claude is
there.

  ./jr chat-on               ON when chat is enabled in settings.yaml (the hourly run then leaves messages to chat)
  ./jr chat-start            take the chat lock (one chat session at a time): STARTED <token>, or BUSY
  ./jr chat-wait             wait before the next check: CHECK, or QUIET when the chat has gone quiet
  ./jr chat-seen             note activity (save-messages and save-replies do this too), which keeps the chat going
  ./jr chat-done TOKEN       release the lock
  ./jr chat-file PATH        the Google Drive connector's upload fields for a file (résumés, PDFs)
  ./jr chat-folder [ID]      the Drive folder id Job Radar files go in (saved once the folder exists)
  ./jr chat-propose FILE     record facts Claude proposes for truth.md; prints the reply rows to write
  ./jr chat-approvals FILE   read the page's `approvals` rows and add each approved fact to truth.md

Only your tap on Approve in the page writes an `approvals` row (only the page's owner can write there), and Claude
never writes there itself. An approved fact goes into truth.md exactly as the page showed it, added by this code
rather than by Claude, so the "ask before editing truth.md" rule in .claude/settings.json still covers every other
change.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .store import now

STALE_MINUTES = 120         # a chat session that stopped without releasing the lock
MAX_WAIT_SECONDS = 100      # one wait stays under the default 2-minute Bash timeout
# Seconds between checks, by minutes since the last message or reply: quick while you're talking, slower after.
PACE = [(5, 20), (15, 60)]
SLOW_SECONDS = MAX_WAIT_SECONDS

MIME = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain", ".json": "application/json",
        ".csv": "text/csv", ".png": "image/png"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_facts (
  id          TEXT PRIMARY KEY,   -- also the id of the reply row the page shows with Approve / Not right
  message_id  TEXT,               -- the message it answers
  section     TEXT NOT NULL,      -- where in truth.md it would go
  text        TEXT NOT NULL,      -- the exact fact, as it would be added
  proposed_at TEXT NOT NULL,
  decision    TEXT,               -- approve | reject (from the page's approvals row)
  decided_at  TEXT,
  added_at    TEXT                -- when Claude added it to truth.md
);
"""


def settings() -> dict:
    s = config.load_optional("settings").get("chat") or {}
    return {"enabled": bool(s.get("enabled")), "check_minutes": int(s.get("check_minutes") or 10),
            "quiet_minutes": int(s.get("quiet_minutes") or 45), "drive_folder": s.get("drive_folder") or "Job Radar files"}


# ---------- one chat session at a time ----------

def _path(name: str) -> Path:
    return config.project_path(f"data/{name}")


def _read_json(name: str) -> dict:
    try:
        return json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return {}


def _write_json(name: str, data: dict) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))


def start(now_fn: Callable[[], datetime] = None) -> str:
    at = (now_fn or (lambda: datetime.now(timezone.utc)))()
    lock = _read_json("chat.lock")
    try:
        began = datetime.fromisoformat(lock["started"])
    except (KeyError, TypeError, ValueError):
        began = None
    if began and at - began < timedelta(minutes=STALE_MINUTES):
        return f"BUSY: a chat session has been running since {began.astimezone().strftime('%-I:%M %p')}."
    token = at.strftime("%Y%m%dT%H%M%S-") + secrets.token_hex(2)
    _path("chat.lock").unlink(missing_ok=True)
    try:
        with open(_path("chat.lock"), "x") as f:
            json.dump({"token": token, "started": at.isoformat(timespec="seconds")}, f)
    except FileExistsError:
        return "BUSY: another chat session took the lock a moment ago."
    state = _read_json("chat_state.json")
    state["started"] = at.isoformat(timespec="seconds")
    state.setdefault("last_activity", state["started"])
    _write_json("chat_state.json", state)
    return f"STARTED {token}"


def done(token: str) -> str:
    lock = _read_json("chat.lock")
    if not lock:
        return "No chat lock to release."
    if lock.get("token") != token:
        return "Not released: the chat lock belongs to another session."
    _path("chat.lock").unlink(missing_ok=True)
    return "Released the chat lock."


def touch(at: Optional[datetime] = None) -> None:
    """Note activity: a message from you or a reply from Claude. It keeps the chat session going."""
    state = _read_json("chat_state.json")
    state["last_activity"] = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    _write_json("chat_state.json", state)


def next_wait(idle: timedelta, quiet_minutes: int) -> Optional[int]:
    """Seconds to wait before the next check, or None when the chat has been quiet long enough to end."""
    minutes = idle.total_seconds() / 60
    if minutes >= quiet_minutes:
        return None
    for limit, seconds in PACE:
        if minutes < limit:
            return seconds
    return SLOW_SECONDS


def wait(now_fn: Callable[[], datetime] = None, sleep: Callable[[float], None] = time.sleep,
         conn: Optional[sqlite3.Connection] = None) -> str:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    state = _read_json("chat_state.json")
    try:
        last = datetime.fromisoformat(state["last_activity"])
    except (KeyError, TypeError, ValueError):
        last = now_fn()
    quiet = settings()["quiet_minutes"]
    seconds = next_wait(now_fn() - last, quiet)
    if seconds is None:
        return f"QUIET: nothing for {quiet} minutes. End the chat (prompts/chat.md, last step)."
    sleep(seconds)
    pending = conn is not None and bool(conn.execute(
        "SELECT 1 FROM chat_facts WHERE decision IS NULL LIMIT 1").fetchone())
    return "CHECK" + (" (facts waiting for your approval: read `approvals` too)" if pending else "")


# ---------- files ----------

def drive_upload(path: str) -> dict:
    """The Google Drive connector's create_file fields for a file: it reads the bytes from the sha256 reference."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"No file at {p}.")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    fields = {"title": p.name, "contentMimeType": MIME.get(p.suffix.lower(), "application/octet-stream"),
              "base64Content": f"sha256:{digest}:{p.as_posix()}", "disableConversionToGoogleType": True}
    folder = _read_json("chat_state.json").get("drive_folder_id")
    if folder:
        fields["parentId"] = folder
    return fields


def folder(folder_id: Optional[str] = None) -> str:
    state = _read_json("chat_state.json")
    if folder_id:
        state["drive_folder_id"] = folder_id
        _write_json("chat_state.json", state)
        return f"Saved the Drive folder id {folder_id}."
    return state.get("drive_folder_id") or (f"NONE: create a Drive folder named \"{settings()['drive_folder']}\" "
                                             "(mime type application/vnd.google-apps.folder), then ./jr chat-folder <id>.")


# ---------- facts for truth.md ----------

def connect_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def propose(conn: sqlite3.Connection, items: List[dict]) -> List[dict]:
    """Record proposed facts. Returns the reply rows to write to the page: [{doc_id, data}]."""
    connect_schema(conn)
    rows = []
    for it in items:
        section, text = str(it.get("section") or "").strip(), str(it.get("text") or "").strip()
        if not section or not text:
            raise SystemExit("Each fact needs a section and the exact text to add.")
        fid = "f" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + secrets.token_hex(2)
        at = now()
        conn.execute("INSERT INTO chat_facts (id, message_id, section, text, proposed_at) VALUES (?, ?, ?, ?, ?)",
                     (fid, it.get("message_id"), section, text, at))
        rows.append({"doc_id": fid, "data": {"reply_to": it.get("message_id"), "kind": "fact", "at": at,
                                             "text": str(it.get("note") or "").strip(), "fact": text,
                                             "section": section}})
    conn.commit()
    return rows


def _approval_rows(text: str) -> Dict[str, dict]:
    out = {}
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("data"), dict) and str(row.get("id", "")).startswith("f"):
            out[row["id"]] = row["data"]
    return out


def read_approvals(conn: sqlite3.Connection, text: str) -> Tuple[List[dict], List[str]]:
    """Store the page's decisions. Returns (approved facts not yet added, ids you turned down this time)."""
    connect_schema(conn)
    rejected = []
    for fid, data in _approval_rows(text).items():
        decision = data.get("decision")
        if decision not in ("approve", "reject"):
            continue
        cur = conn.execute("UPDATE chat_facts SET decision = ?, decided_at = ? WHERE id = ? AND decision IS NULL",
                           (decision, str(data.get("at") or now()), fid))
        if cur.rowcount and decision == "reject":
            rejected.append(fid)
    conn.commit()
    todo = [dict(r) for r in conn.execute(
        "SELECT id, message_id, section, text, decided_at FROM chat_facts "
        "WHERE decision = 'approve' AND added_at IS NULL ORDER BY proposed_at")]
    return todo, rejected


def add_to_truth(path: Path, section: str, text: str, day: str) -> str:
    """Add one approved fact to truth.md. `section` names a heading, optionally with "> a bullet" under it
    ("Acme: Project Manager > Billing system rollout"). Returns where it went."""
    lines = path.read_text().split("\n")
    parts = [x.strip() for x in section.split(">") if x.strip()]
    entry = f"{text.rstrip()} (Confirmed {day} on the dashboard.)"

    def level(line: str) -> int:
        return len(line) - len(line.lstrip("#")) if line.startswith("#") else 0

    def heading_text(line: str) -> str:
        return line.lstrip("#").strip().lower()

    want = parts[0].lower() if parts else ""
    heads = [i for i, l in enumerate(lines) if level(l)]
    match = (next((i for i in heads if heading_text(lines[i]) == want), None)
             or next((i for i in heads if heading_text(lines[i]).startswith(want)), None)
             or next((i for i in heads if want and want in heading_text(lines[i])), None))
    if match is None:
        if "## Added from the dashboard" not in lines:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", "## Added from the dashboard"]
        lines.append(f"- {entry}")
        path.write_text("\n".join(lines) + "\n")
        return "a new section, Added from the dashboard (no heading matched)"
    end = next((i for i in heads if i > match and level(lines[i]) <= level(lines[match])), len(lines))
    where, bullet, indent = lines[match].lstrip("#").strip(), None, ""
    if len(parts) > 1:
        bullet = next((i for i in range(match + 1, end)
                       if lines[i].lstrip().startswith("- ") and parts[1].lower() in lines[i].lower()), None)
    if bullet is not None:
        base = len(lines[bullet]) - len(lines[bullet].lstrip())
        indent = " " * (base + 2)
        at = bullet + 1
        while at < end and lines[at].strip() and len(lines[at]) - len(lines[at].lstrip()) > base:
            at += 1
        where += " > " + lines[bullet].strip()[2:60]
    else:
        at = end
        while at > match + 1 and not lines[at - 1].strip():
            at -= 1
    lines.insert(at, f"{indent}- {entry}")
    path.write_text("\n".join(lines))
    return where


def apply_approved(conn: sqlite3.Connection, truth: Path, day: str, todo: List[dict]) -> List[str]:
    """Add each approved fact to truth.md and mark it added. Returns one line per fact."""
    out = []
    for f in todo:
        where = add_to_truth(truth, f["section"], f["text"], day)
        mark_added(conn, f["id"])
        out.append(f"ADDED {f['id']}: {f['text']} (under {where})")
    return out


def mark_added(conn: sqlite3.Connection, fid: str) -> str:
    connect_schema(conn)
    cur = conn.execute("UPDATE chat_facts SET added_at = ? WHERE id = ? AND decision = 'approve'", (now(), fid))
    conn.commit()
    return f"Marked {fid} as added to truth.md." if cur.rowcount else f"{fid} isn't an approved fact."
