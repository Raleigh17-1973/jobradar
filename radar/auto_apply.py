"""The apply task's bookkeeping: which queued job is next, one run at a time, and the jobs left for you.

The apply task (prompts/auto_apply.md, a scheduled task of its own) fills and submits applications in Chrome for
jobs you clicked Apply on, where no account or sign-in is needed. Chrome and the form are Claude's part. This
module only decides what's next and remembers what happened:
- `start` takes a lock so two apply runs never overlap, and lists the queue as the task sees it. With nothing
  ready it stops, so the run never starts or connects to Chrome for nothing.
- `next_job` hands out one job at a time, within the run's time and count limits.
- `hold` marks a job the run had to leave for you (a CAPTCHA, a question with no saved answer, an account step).
  Held jobs are skipped until `clear`, and the phone alert names them once.
- `end` releases the lock and sums up the run.

Turned on by `auto_apply: enabled: true` in config/settings.yaml.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from . import ats, config, store
from .store import now

DEFAULTS = {"enabled": False, "max_per_run": 4, "minutes": 45}
STALE_MINUTES = 90        # a lock older than this was left by a run that died
SAME_TEXT = 0.8           # share of the shorter description's 8-word runs found in the other: the same posting
HELD = "needs_you"


def options(settings: dict) -> dict:
    return {**DEFAULTS, **(settings.get("auto_apply") or {})}


def _lock_path() -> Path:
    return config.project_path("data/auto_apply.lock")


def _state_path() -> Path:
    return config.project_path("data/auto_apply_state.json")


def _decisions_dir() -> Path:
    return config.project_path("data/dashboard_decisions")


def _folder_base() -> Path:
    """Packet folders are stored relative to the applications folder's parent (as packet.FOLDER_BASE)."""
    return config.setting_path("applications", "applications").parent


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


def _when(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _signals(r: sqlite3.Row) -> dict:
    return json.loads(r["signals"] or "{}")


def _save_signals(conn: sqlite3.Connection, job_id: int, signals: dict) -> None:
    conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), job_id))


def clicked_apply(job_id: int) -> bool:
    """Your latest dashboard choice for the job is still Apply."""
    doc = _read(_decisions_dir() / f"j{job_id}.json")
    return (doc.get("data") or doc).get("decision") == "apply"


def why_not(r: sqlite3.Row) -> Optional[str]:
    """Why the apply task can't take this queued job now, or None when it can."""
    s = _signals(r)
    packet = s.get("packet")
    if not packet or packet.get("placeholder"):
        return "needs a packet"
    if s.get("paused"):
        return "paused"
    held = s.get("auto_apply") or {}
    if held.get("status") == HELD:
        return f"waiting on you: {held.get('reason')}"
    si = ats.sign_in(r["apply_url"], r["ats"])
    if si and si["need"] == "account":
        return f"needs your account step ({si['system']})"
    if si and si["need"] == "signed_in":
        return f"needs you signed in ({si['system']})"
    if not clicked_apply(r["id"]):
        return "no Apply click on the dashboard"
    return None


def queue(conn: sqlite3.Connection) -> List[Tuple[sqlite3.Row, Optional[str]]]:
    rows = conn.execute("SELECT * FROM jobs WHERE status IN ('queued', 'packet_ready') "
                        "ORDER BY decided_at DESC, id DESC").fetchall()
    return [(r, why_not(r)) for r in rows]


def same_posting(conn: sqlite3.Connection, r: sqlite3.Row) -> Optional[sqlite3.Row]:
    """A job you've already applied to whose description is this one's, e.g. one role posted by a company and by
    its parent under two names. The duplicate check closes repeats only when the titles agree too; this catches
    the rest before a second application goes out."""
    mine = store._text_runs(r["description"])
    if not mine:
        return None
    for other in conn.execute("SELECT * FROM jobs WHERE status = 'applied' AND id != ?", (r["id"],)):
        theirs = store._text_runs(other["description"])
        if theirs and len(mine & theirs) >= SAME_TEXT * min(len(mine), len(theirs)):
            return other
    return None


def start(conn: sqlite3.Connection, settings: dict, at: Optional[datetime] = None) -> Tuple[bool, str]:
    at = at or datetime.now(timezone.utc)
    opts = options(settings)
    if not opts["enabled"]:
        return False, "STOP: applying on its own is off (auto_apply: enabled in config/settings.yaml)."
    lock = _read(_lock_path())
    began = _when(lock.get("started"))
    if began and at - began < timedelta(minutes=STALE_MINUTES):
        return False, f"STOP: another apply run has been going since {began.astimezone():%H:%M}."
    jobs = queue(conn)
    lines = [f"{r['id']:>6}  {r['company']}: {r['title']}  ->  {why or 'ready'}" for r, why in jobs]
    ready = sum(1 for _, why in jobs if not why)
    if not ready:
        # An hour with nothing to submit never starts or connects to Chrome. It also ends a Chrome-down stretch:
        # the next run that needs Chrome and can't reach it alerts you again.
        state = _read(_state_path())
        if state.pop("chrome_down_at", None):
            _write(_state_path(), state)
        return False, "\n".join(["STOP: nothing is ready to apply, so this run doesn't connect to Chrome."] + lines)
    _write(_lock_path(), {"started": at.isoformat(timespec="seconds"), "tried": []})
    head = (f"STARTED. {ready} ready to apply; this run takes up to {opts['max_per_run']} and starts none after "
            f"{opts['minutes']} minutes.")
    return True, "\n".join([head] + lines)


def next_job(conn: sqlite3.Connection, settings: dict, at: Optional[datetime] = None) -> Tuple[Optional[dict], str]:
    """(job, message): the next job to apply to, or None and why the run should stop."""
    at = at or datetime.now(timezone.utc)
    opts = options(settings)
    lock = _read(_lock_path())
    began = _when(lock.get("started"))
    if not began:
        return None, "STOP: no apply run started (./jr apply-start)."
    tried = lock.get("tried", [])
    if len(tried) >= opts["max_per_run"]:
        return None, f"STOP: this run's {opts['max_per_run']} applications are done; the rest wait for the next run."
    if at - began >= timedelta(minutes=opts["minutes"]):
        return None, f"STOP: {opts['minutes']} minutes are up; the rest wait for the next run."
    for r, why in queue(conn):
        if why or r["id"] in tried:
            continue
        twin = same_posting(conn, r)
        if twin:
            hold(conn, r["id"], f"same posting as {twin['company']} ({twin['id']}), already applied",
                 details=f"Its description matches job {twin['id']}, {twin['company']}: {twin['title']}, which you "
                         f"applied to. Clear the hold (./jr apply-clear {r['id']}) to apply to this one too.")
            conn.commit()
            continue
        lock["tried"] = tried + [r["id"]]
        _write(_lock_path(), lock)
        s = _signals(r)
        si = ats.sign_in(r["apply_url"], r["ats"])
        folder = (s.get("packet") or {}).get("folder")
        return {
            "id": r["id"], "company": r["company"], "title": r["title"], "apply_url": r["apply_url"],
            "system": si["system"] if si else None,
            "pdfs": bool((s.get("packet") or {}).get("pdfs", True)),
            "folder": str(_folder_base() / folder) if folder else None,
            "pay": [r["salary_min"], r["salary_max"]],
        }, "NEXT"
    return None, "STOP: nothing else is ready to apply."


def hold(conn: sqlite3.Connection, job_id: int, reason: str, details: Optional[str] = None) -> None:
    """Leave a job for you. The run moves on; later runs skip it until `clear`; the phone alert names it once."""
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        raise KeyError(job_id)
    s = _signals(r)
    s["auto_apply"] = {"status": HELD, "reason": reason, "details": details, "at": now(), "alerted": False}
    _save_signals(conn, job_id, s)
    conn.commit()


def clear(conn: sqlite3.Connection, job_id: int) -> bool:
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        raise KeyError(job_id)
    s = _signals(r)
    if "auto_apply" not in s:
        return False
    s.pop("auto_apply")
    _save_signals(conn, job_id, s)
    conn.commit()
    return True


def held_jobs(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return [r for r in conn.execute("SELECT * FROM jobs WHERE signals LIKE '%\"auto_apply\"%' "
                                    "AND status IN ('queued', 'packet_ready') ORDER BY id")
            if (_signals(r).get("auto_apply") or {}).get("status") == HELD]


def end(conn: sqlite3.Connection, chrome_down: bool = False) -> str:
    """Release the lock and sum up the run. With chrome_down, the first such run in a row also returns an ALERT line."""
    lock = _read(_lock_path())
    tried = lock.get("tried", [])
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(tried))})", tried)} if tried else {}
    applied = [rows[i] for i in tried if i in rows and rows[i]["status"] == "applied"]
    held = [rows[i] for i in tried if i in rows and (_signals(rows[i]).get("auto_apply") or {}).get("status") == HELD]
    _lock_path().unlink(missing_ok=True)
    state = _read(_state_path())
    lines = []
    if chrome_down:
        if not state.get("chrome_down_at"):
            state["chrome_down_at"] = now()
            lines.append("ALERT: Job Radar couldn't reach Job Radar Chrome to apply. Run ./jr chrome-up in the "
                         "jobradar folder to see why.")
        else:
            lines.append("Chrome still not reachable (you were alerted at the first miss).")
    else:
        state.pop("chrome_down_at", None)
    _write(_state_path(), state)
    lines.append(f"Apply run: {len(applied)} submitted"
                 + (": " + "; ".join(f"{r['company']} ({r['id']})" for r in applied) if applied else "")
                 + f". {len(held)} left for you"
                 + (": " + "; ".join(f"{r['company']} ({r['id']}): {_signals(r)['auto_apply']['reason']}"
                                     for r in held) if held else "")
                 + ".")
    waiting = len(held_jobs(conn))
    if waiting:
        lines.append(f"{waiting} queued job{'s' if waiting > 1 else ''} in all waiting on you (./jr apply-held).")
    return "\n".join(lines)
