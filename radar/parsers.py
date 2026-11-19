"""Turns LinkedIn, Indeed and Glassdoor alert emails into job records.

LinkedIn and Indeed are read from the plain-text part, which is clean. Glassdoor is read from
the HTML part because its plain text is line-wrapped. Layouts checked against real alerts on 2026-11-02.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


@dataclass
class ParsedJob:
    source: str
    source_job_id: str
    title: str
    company: str
    location: Optional[str] = None
    url: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_source: str = "posted"
    signals: dict = field(default_factory=dict)


# ---------- shared helpers ----------

_INVISIBLE = re.compile(r"[­͏​-‏⁠﻿]")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
HOURS_PER_YEAR = 2080


def _plain_lines(msg: EmailMessage) -> List[str]:
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part else ""
    return [l for l in (_INVISIBLE.sub("", s).strip() for s in text.splitlines()) if l]


def _html_lines(msg: EmailMessage) -> List[str]:
    part = msg.get_body(preferencelist=("html",))
    if not part:
        return []
    text = BeautifulSoup(part.get_content(), "html.parser").get_text("\n")
    return [l for l in (_INVISIBLE.sub("", s).strip() for s in text.splitlines()) if l]


def _fallback_id(*parts: Optional[str]) -> str:
    return "h" + hashlib.sha1("|".join(p or "" for p in parts).lower().encode()).hexdigest()[:12]


def parse_salary(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Annual (min, max) from '$183,000 - $245,000 a year', '$270K – $310K/yr', '$45 - $75 an hour'."""
    amounts = [float(n.replace(",", "")) * (1000 if k else 1) for n, k in _MONEY.findall(text)]
    if not amounts:
        return None, None
    # Employers sometimes label an annual figure "an hour"; only small numbers are really hourly.
    if re.search(r"\bhour\b|/hr\b", text, re.I) and max(amounts) < 500:
        amounts = [a * HOURS_PER_YEAR for a in amounts]
    lo, hi = int(min(amounts)), int(max(amounts))
    if len(amounts) == 1:
        if re.search(r"starting at|from", text, re.I):
            return lo, None
        if re.search(r"up to", text, re.I):
            return None, hi
    return lo, hi


# ---------- LinkedIn ----------

_LI_JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
# Short notes LinkedIn prints between a job's location and its "View job" link.
_LI_NOTE = re.compile(
    r"^(top applicant|fast growing|actively recruiting|be an early applicant|recently posted|promoted|"
    r"easy apply|apply with resume.*|\d+ (school|company) alum\w*|\d+ connections?|in your network|"
    r"this company is .*|.*\bactively hiring\b.*|hiring now.*|.*\$\d.*)$",
    re.I,
)
# "Hiring now: <title> at <company> starting at $130K/year" or "<title> at <company>: up to $218K/year"
_LI_SUBJECT_PAY = re.compile(
    r"(?:Hiring now: )?(?P<title>.+) at (?P<company>.+?):? (?P<pay>(?:starting at|up to) \$[\d,.]+K?)", re.I)


def parse_linkedin(msg: EmailMessage) -> List[ParsedJob]:
    jobs: List[ParsedJob] = []
    buf: List[str] = []
    saved_next = False
    for line in _plain_lines(msg):
        if line.startswith("Your saved job at"):
            saved_next = True
        m = _LI_JOB_ID.search(line) if line.startswith("View job:") else None
        if m:
            notes: List[str] = []
            while buf and _LI_NOTE.match(buf[-1]):
                notes.insert(0, buf.pop())
            if len(buf) >= 3:
                title, company, location = buf[-3:]
                job = ParsedJob("linkedin", m.group(1), title, company, location,
                                f"https://www.linkedin.com/jobs/view/{m.group(1)}/")
                if notes:
                    job.signals["linkedin_notes"] = notes
                if any(n.lower().startswith(("apply with resume", "easy apply")) for n in notes):
                    job.signals["easy_apply"] = True
                if saved_next:
                    job.signals["saved_by_you"] = True
                    saved_next = False
                jobs.append(job)
            buf = []
        elif set(line) <= set("-") or line.startswith(("http", "See all jobs", "Manage your job alerts")):
            buf = []
        else:
            buf.append(line)

    # Some subjects carry pay that the body doesn't show.
    sm = _LI_SUBJECT_PAY.search(msg["Subject"] or "")
    if sm:
        for job in jobs:
            if job.title.lower() == sm["title"].strip().lower() and job.salary_min is None:
                job.salary_min, job.salary_max = parse_salary(sm["pay"])
                job.signals["salary_text"] = sm["pay"]
    return jobs


# ---------- Indeed ----------

_INDEED_AGE = re.compile(r"^(just posted|today|active \d+ days? ago|\d+\+? days? ago)$", re.I)
_INDEED_PAY = re.compile(r"^(estimated |from |up to |starting at )?\$\d", re.I)
_INDEED_FIELD = re.compile(r"^(Salary|Job type|Work setting|Benefits):\s*(.*)$")


def indeed_job_key(url: str) -> Optional[str]:
    """Indeed's job key (jk) from an indeed.com link, or decoded from inside a cts.indeed.com tracking link."""
    jk = parse_qs(urlparse(url).query).get("jk")
    if jk:
        return jk[0]
    m = re.search(r"cts\.indeed\.com/v3/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    token = m.group(1)
    try:
        payload = zlib.decompress(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)), 16 + zlib.MAX_WBITS)
        inner = json.loads(payload).get("u", "")
    except (ValueError, zlib.error):
        return None
    return (parse_qs(urlparse(inner).query).get("jk") or [None])[0]


def _indeed_url(jk: str) -> str:
    return f"https://www.indeed.com/viewjob?jk={jk}"


def parse_indeed_digest(msg: EmailMessage) -> List[ParsedJob]:
    """Multi-job alert: title / 'Company - Location' / pay / 'Easily apply' / snippet / age / link."""
    jobs: List[ParsedJob] = []
    buf: List[str] = []
    for line in _plain_lines(msg):
        if "http" not in line:
            buf.append(line)
            continue
        url = line.split()[-1]
        is_job_link = re.search(r"indeed\.com/(rc|pagead)/clk", url)
        if is_job_link and len(buf) >= 2:
            company, _, location = buf[1].rpartition(" - ")
            if not company:
                company, location = buf[1], ""
            # Sponsored listings (pagead) carry no job key, so they're identified by company + title.
            jk = indeed_job_key(url)
            job = ParsedJob("indeed", jk or _fallback_id(company, buf[0]), buf[0], company, location or None,
                            _indeed_url(jk) if jk else url)
            if is_job_link.group(1) == "pagead":
                job.signals["sponsored"] = True
            for extra in buf[2:]:
                if _INDEED_PAY.match(extra):
                    job.salary_min, job.salary_max = parse_salary(extra)
                    job.signals["salary_text"] = extra
                elif extra.lower() == "easily apply":
                    job.signals["indeed_easy_apply"] = True
                elif _INDEED_AGE.match(extra):
                    job.signals["posted"] = extra
            jobs.append(job)
        buf = []
    return jobs


def parse_indeed_match(msg: EmailMessage) -> List[ParsedJob]:
    """Single-job match: intro / title / company / location / Salary: / Job type: / Work setting: / View job."""
    lines = _plain_lines(msg)
    view = next((k for k, l in enumerate(lines) if l.startswith("View job:")), None)
    if view is None:
        return []
    first_field = next((k for k, l in enumerate(lines[:view]) if _INDEED_FIELD.match(l)), view)
    if first_field < 3:
        return []
    title, company, location = lines[first_field - 3:first_field]
    url = lines[view].split(":", 1)[1].strip()
    jk = indeed_job_key(url)
    job = ParsedJob("indeed", jk or _fallback_id(company, title), title, company, location,
                    _indeed_url(jk) if jk else url)
    for line in lines[first_field:view]:
        m = _INDEED_FIELD.match(line)
        if not m:
            continue
        key, value = m.groups()
        if key == "Salary":
            job.salary_min, job.salary_max = parse_salary(value)
            job.signals["salary_text"] = value
        elif key == "Work setting":
            job.signals["work_setting"] = value
        elif key == "Job type":
            job.signals["job_type"] = value
    return [job]


# ---------- Glassdoor ----------

_GD_MATCH_PCT = re.compile(r"^\d{1,3}%$")


def parse_glassdoor(msg: EmailMessage) -> List[ParsedJob]:
    """Match card: company / title / location / pay / 83% / Excellent / Job match / ratings."""
    lines = _html_lines(msg)
    match_ids = re.findall(r"matchId=([0-9a-f-]{8,})", "\n".join(_plain_lines(msg)))
    pct_rows = [k for k, l in enumerate(lines) if _GD_MATCH_PCT.match(l)]
    jobs: List[ParsedJob] = []
    for n, k in enumerate(pct_rows):
        j = k - 1
        pay = lines[j] if j >= 0 and "$" in lines[j] else None
        if pay:
            j -= 1
        if j < 2:
            continue
        company, title, location = lines[j - 2:j + 1]
        job_id = match_ids[n] if n < len(match_ids) else _fallback_id(company, title)
        job = ParsedJob("glassdoor", job_id, title, company, location)
        job.signals["glassdoor_match"] = lines[k]
        if pay:
            job.salary_min, job.salary_max = parse_salary(pay)
            job.salary_source = "posted" if "employer" in pay.lower() else "estimate"
            job.signals["salary_text"] = pay
        window = lines[k:pct_rows[n + 1] if n + 1 < len(pct_rows) else len(lines)]
        for w, value in enumerate(window):
            if re.fullmatch(r"\d\.\d", value):
                label = " ".join(window[w + 1:w + 4]).lower()
                if "applicant" in label:
                    job.signals["glassdoor_applicant_experience"] = float(value)
                elif "company" in label:
                    job.signals["glassdoor_company_rating"] = float(value)
        jobs.append(job)
    return jobs


# ---------- health check ----------

_LOCATION_LIKE = re.compile(r"^(united states|remote|[a-z .'-]+, [a-z]{2}|.*, united states)$", re.I)


def looks_misread(job: ParsedJob) -> bool:
    """A company that reads like a place, or a location that reads like a note, means a layout changed."""
    return bool(_LOCATION_LIKE.match(job.company) or _LI_NOTE.match(job.location or ""))


# ---------- dispatch ----------

def parse_email(msg: EmailMessage, sender: str) -> List[ParsedJob]:
    sender = sender.lower()
    if sender.endswith("linkedin.com"):
        return parse_linkedin(msg)
    if "indeed.com" in sender:
        if any(l.startswith("View job:") for l in _plain_lines(msg)):
            return parse_indeed_match(msg)
        return parse_indeed_digest(msg)
    if sender.endswith("glassdoor.com"):
        return parse_glassdoor(msg)
    return []
