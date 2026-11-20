"""One scheduled run at a time.

The hourly task starts a new run on the hour even when the last one is still going, and the first run of the day
often takes more than an hour. Two runs at once score the same jobs twice, answer a message twice and race to
publish the dashboard. So each run takes this lock first (prompts/scheduled_run.md, step 0):
- `start` takes the lock and returns STARTED with a token. If another run holds it, `start` waits for it, up to
  `wait_minutes`, and returns WAITING if that run still isn't done. The run calls it a few more times, then gives up.
- `done` releases the lock, but only for the token that took it, so a run that gave up can't free another run's lock.
A lock older than STALE_MINUTES was left by a run that stopped without releasing it, and the next run takes it
over. To clear one by hand, delete data/run.lock.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import config

STALE_MINUTES = 180     # the longest runs so far took about two hours
WAIT_MINUTES = 9        # one call stays under the 10-minute Bash timeout
POLL_SECONDS = 20


def _lock_path() -> Path:
    return config.project_path("data/run.lock")


def _read() -> dict:
    try:
        return json.loads(_lock_path().read_text())
    except (OSError, ValueError):
        return {}


def _began(lock: dict) -> Optional[datetime]:
    """When the run holding the lock started. An unreadable lock (one being written this instant) counts from
    the file's own time."""
    try:
        return datetime.fromisoformat(lock["started"])
    except (KeyError, TypeError, ValueError):
        try:
            return datetime.fromtimestamp(_lock_path().stat().st_mtime, timezone.utc)
        except OSError:
            return None


def _clock(at: datetime) -> str:
    return at.astimezone().strftime("%-I:%M %p")


def start(wait_minutes: float = WAIT_MINUTES, now: Callable[[], datetime] = None,
          sleep: Callable[[float], None] = time.sleep) -> Tuple[bool, str]:
    now = now or (lambda: datetime.now(timezone.utc))
    deadline = now() + timedelta(minutes=wait_minutes)
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        at = now()
        lock = _read()
        began = _began(lock)
        stale = began is not None and at - began >= timedelta(minutes=STALE_MINUTES)
        if stale:
            path.unlink(missing_ok=True)   # left by a run that stopped without releasing it
        if began is None or stale:
            token = at.strftime("%Y%m%dT%H%M%S-") + secrets.token_hex(2)
            try:
                with open(path, "x") as f:   # "x" fails if another run took the lock a moment ago
                    json.dump({"token": token, "started": at.isoformat(timespec="seconds")}, f)
            except FileExistsError:
                continue
            note = (f"\nThe lock from the {_clock(began)} run was more than {STALE_MINUTES // 60} hours old, so that "
                    f"run stopped without releasing it." if stale else "")
            return True, f"STARTED {token}{note}"
        if at >= deadline:
            minutes = int((at - began).total_seconds() // 60)
            return False, (f"WAITING: the run that started at {_clock(began)} is still going "
                           f"({minutes} minutes so far).")
        sleep(POLL_SECONDS)


def done(token: str) -> str:
    lock = _read()
    if not lock:
        return "No run lock to release."
    if lock.get("token") != token:
        began = _began(lock)
        when = f"the run that started at {_clock(began)}" if began else "another run"
        return f"Not released: the lock belongs to {when}. Leave it for that run."
    _lock_path().unlink(missing_ok=True)
    return "Released the run lock."
