"""Free, rule-based filters from preferences.yaml. Nothing here calls Claude.

A job that passes becomes a 'candidate' for lookup and scoring; one that fails is 'filtered' with a reason,
so it can be reviewed or brought back by editing preferences and re-running `./jr filter --recheck`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import List, Optional

from . import config
from .store import _norm

# Statuses the filter may (re)assign. Anything further along was decided by scoring or by you.
REFILTERABLE = ("new", "filtered", "candidate")


def _plain(text: str) -> str:
    """Lowercase with hyphens and slashes as spaces, so 'Vice-President' matches 'vice president'."""
    return re.sub(r"[-–—/]", " ", text.lower())


def _word_patterns(words: List[Optional[str]]) -> List[re.Pattern]:
    return [re.compile(r"\b" + re.escape(_plain(w).strip()) + r"s?\b") for w in words if w and str(w).strip()]


class Rules:
    def __init__(self, prefs: dict):
        self.include = _word_patterns(prefs.get("title_keywords_include", []))
        excluded = [w for w in prefs.get("title_keywords_exclude") or [] if w and str(w).strip()]
        self.exclude = list(zip(excluded, _word_patterns(excluded)))
        self.project = re.compile(r"\bproject\b")
        self.project_levels = _word_patterns(prefs.get("project_titles_need_one_of", []))
        self.drop_below_max = prefs.get("hard_drop_below_max") or prefs.get("min_base_salary") or 0
        self.min_hourly = prefs.get("contract_min_hourly_remote") or 0
        self.blocked = {_norm(c): "staffing firm or aggregator" for c in prefs.get("staffing_agencies", [])}
        self.blocked.update({_norm(c): "company you excluded" for c in prefs.get("exclude_companies", [])})

    def reason_to_drop(self, job: sqlite3.Row) -> Optional[str]:
        title = _plain(job["title"])
        company = _norm(job["company"])
        for name, why in self.blocked.items():
            if company == name or company.startswith(name + " "):
                return why
        for word, pattern in self.exclude:
            if pattern.search(title):
                return f"title says '{word}'"
        in_scope = any(p.search(title) for p in self.include) or (
            self.project.search(title) and any(p.search(title) for p in self.project_levels))
        if not in_scope:
            return "title outside your target roles"
        signals = json.loads(job["signals"] or "{}")
        # Location goes last: it can cost a posting read (remote_check.py), the rules above cost nothing.
        return self._pay_or_type(job, signals) or self._location(signals)

    def _pay_or_type(self, job: sqlite3.Row, signals: dict) -> Optional[str]:
        # Contracts are judged on the hourly rate (stored annualized at 2080 hours), not the salary floor.
        if re.search(r"\bhour\b|/hr\b", signals.get("salary_text", ""), re.I):
            if job["salary_max"] and job["salary_max"] / 2080 < self.min_hourly:
                return f"posted rate tops out at ${job['salary_max'] / 2080:.0f}/hr"
            return None
        if job["salary_max"] and job["salary_max"] < self.drop_below_max:
            return f"posted pay tops out at ${job['salary_max'] // 1000}K"
        if re.search(r"part-time", signals.get("job_type", ""), re.I):
            return "part-time role"
        return None

    @staticmethod
    def _location(signals: dict) -> Optional[str]:
        """A remote search's listing in some other city stays out until its posting says it's remote,
        unless the job was also seen in the area."""
        outside = set(signals.get("outside_search_area", []))
        if not outside or set(signals.get("locations", [])) - outside:
            return None
        check = signals.get("remote_check") or {}
        where = ", ".join(sorted(outside))
        if not check:
            return f"location {where}: remote not checked yet"
        if check["verdict"] == "restricted":
            return f"remote, but {check['why']}"
        if check["verdict"] != "remote":
            return f"location {where} is not remote or in {config.home_area().get('name') or 'your home area'}"
        return None


def run(conn: sqlite3.Connection, prefs: dict, recheck: bool = False) -> Counter:
    """Filter new jobs (or, with recheck, every job not yet scored). Returns counts by outcome."""
    rules = Rules(prefs)
    statuses = REFILTERABLE if recheck else ("new",)
    rows = conn.execute(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))})", statuses).fetchall()
    outcomes = Counter()
    for job in rows:
        # Jobs you brought back from the dashboard's Filtered out tab, and ones a full board review kept
        # (radar/board_review.py), skip the rules from then on.
        signals = json.loads(job["signals"] or "{}")
        if signals.get("restored_by_you") or signals.get("kept_by_review"):
            continue
        reason = rules.reason_to_drop(job)
        conn.execute("UPDATE jobs SET status = ?, filter_reason = ? WHERE id = ?",
                     ("filtered" if reason else "candidate", reason, job["id"]))
        outcomes[reason or "candidate"] += 1
    conn.commit()
    return outcomes
