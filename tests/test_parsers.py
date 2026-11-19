"""Parser tests against real alert emails saved by `./jr fetch-samples`.

Run from the jobradar folder:  .venv/bin/python -m unittest discover -s tests
"""
import os
from pathlib import Path

# The tests read config/examples, never your own config.
os.environ["JOBRADAR_CONFIG_DIR"] = str(Path(__file__).resolve().parent.parent / "config" / "examples")

import email
import email.policy
import glob
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from radar.linkedin_search import SOURCE, parse_cards, window_seconds
from radar.parsers import _LI_NOTE, looks_misread, parse_email, parse_salary
from radar.store import connect, dedupe_key, record_email

SAMPLES = os.path.join(os.path.dirname(__file__), "samples")


def load(path):
    with open(path, "rb") as f:
        msg = email.message_from_bytes(f.read(), policy=email.policy.default)
    return msg, parse_email(msg, parseaddr(msg["From"])[1])


def sample(pattern):
    return sorted(glob.glob(os.path.join(SAMPLES, pattern)))[0]


class EverySample(unittest.TestCase):
    def test_every_alert_yields_clean_jobs(self):
        paths = glob.glob(os.path.join(SAMPLES, "*", "*.eml"))
        self.assertTrue(paths, "no samples; run ./jr fetch-samples")
        for path in paths:
            with self.subTest(email=os.path.basename(path)):
                _, jobs = load(path)
                self.assertTrue(jobs)
                for job in jobs:
                    for value in (job.title, job.company, job.location or ""):
                        self.assertNotIn("http", value)
                        self.assertIsNone(_LI_NOTE.match(value), value)
                    self.assertTrue(job.title and job.company and job.source_job_id)
                    self.assertFalse(looks_misread(job), (job.title, job.company, job.location))


class LinkedIn(unittest.TestCase):
    def test_job_alert(self):
        _, jobs = load(sample("linkedin/*__jobalerts-noreply__1880538017716656584.eml"))
        first = jobs[0]
        self.assertEqual((first.source_job_id, first.title, first.company, first.location),
                         ("4480691561", "Business Technology Solutions Manager", "ZS", "Chicago, IL"))
        self.assertEqual(first.signals["linkedin_notes"], ["Top applicant"])

    def test_saved_job_reminder_flags_only_the_saved_job(self):
        _, jobs = load(sample("linkedin/*__jobs-noreply__1880225912177244409.eml"))
        self.assertTrue(jobs[0].signals.get("saved_by_you"))
        self.assertFalse(any(j.signals.get("saved_by_you") for j in jobs[1:]))

    def test_easy_apply_and_subject_pay(self):
        _, jobs = load(sample("linkedin/*__jobalerts-noreply__1880598415032193464.eml"))
        cyara = next(j for j in jobs if j.company == "Cyara")
        self.assertTrue(cyara.signals.get("easy_apply"))
        self.assertEqual((cyara.salary_min, cyara.salary_max), (130000, None))

    def test_actively_hiring_note_does_not_shift_fields(self):
        _, jobs = load(sample("linkedin/regression__1879709875467183232.eml"))
        self.assertTrue(jobs)
        self.assertFalse(any("actively hiring" in (j.location or "").lower() for j in jobs))

    def test_up_to_pay_in_subject(self):
        _, jobs = load(sample("linkedin/regression__1879949138033762323.eml"))
        archer = next(j for j in jobs if j.company == "Archer")
        self.assertEqual((archer.salary_min, archer.salary_max), (None, 218000))


class Indeed(unittest.TestCase):
    def test_digest_includes_sponsored_listings(self):
        _, jobs = load(sample("indeed/2026-10-20__*.eml"))
        self.assertEqual(len(jobs), 19)
        first = jobs[0]
        self.assertEqual((first.source_job_id, first.company, first.location, first.salary_min, first.salary_max),
                         ("7953d6be628f3a0d", "Revantage Corporate Services, LLC", "Chicago, IL", 183000, 245000))
        self.assertEqual(sum(1 for j in jobs if j.signals.get("sponsored")), 8)

    def test_match_email_decodes_job_key_from_tracking_link(self):
        _, jobs = load(sample("indeed/*__1880754524312795144.eml"))
        (job,) = jobs
        self.assertEqual((job.source_job_id, job.title, job.company), ("3e6d7e368fa704df", "Staff Program Manager, CCO Programs", "GitLab"))
        self.assertEqual((job.salary_min, job.salary_max, job.signals["work_setting"]), (139200, 235200, "Remote"))


class Glassdoor(unittest.TestCase):
    def test_match_card(self):
        _, jobs = load(sample("glassdoor/*__1880505005702989502.eml"))
        (job,) = jobs
        self.assertEqual((job.company, job.title, job.location), ("Anthropic", "GTM Programs Manager, GTM Initiatives", "Remote"))
        self.assertEqual((job.salary_min, job.salary_max, job.salary_source), (270000, 310000, "posted"))
        self.assertEqual((job.signals["glassdoor_company_rating"], job.signals["glassdoor_applicant_experience"]), (4.2, 2.8))


class Helpers(unittest.TestCase):
    def test_salary_formats(self):
        self.assertEqual(parse_salary("$183,000 - $245,000 a year"), (183000, 245000))
        self.assertEqual(parse_salary("$270K – $310K/yr (Employer est.)"), (270000, 310000))
        self.assertEqual(parse_salary("$45 - $75 an hour"), (93600, 156000))
        self.assertEqual(parse_salary("Up to $220,000 a year"), (None, 220000))
        # Annual figures mislabeled "an hour" by the employer stay annual.
        self.assertEqual(parse_salary("$90,000 - $115,000 an hour"), (90000, 115000))

    def test_same_role_across_sites_dedupes(self):
        self.assertEqual(dedupe_key("Acme, Inc.", "Sr. Program Manager (Remote)"),
                         dedupe_key("ACME", "Senior Program Manager"))
        self.assertNotEqual(dedupe_key("Acme", "Program Manager"), dedupe_key("Acme", "Senior Program Manager"))


class LinkedInSearch(unittest.TestCase):
    def test_search_page_cards(self):
        with open(os.path.join(SAMPLES, "linkedin_search.html"), encoding="utf-8") as f:
            jobs = parse_cards(f.read())
        self.assertEqual(len(jobs), 10)
        ey = next(j for j in jobs if j.source_job_id == "4484604749")
        self.assertEqual((ey.title, ey.company, ey.location), ("AI Project Manager", "EY", "Chicago, IL"))
        self.assertEqual(ey.signals["linkedin_listed"], "2026-11-02")
        for j in jobs:
            self.assertEqual(j.source, "linkedin")
            self.assertTrue(j.title and j.company and j.source_job_id.isdigit())

    def test_window_covers_time_since_last_run(self):
        conn = connect(":memory:")
        self.assertEqual(window_seconds(conn, "Q", 30, 72), 72 * 3600)     # never ran
        ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        record_email(conn, "search:Q:x", SOURCE, "LinkedIn search", "Q", ago, 0)
        self.assertAlmostEqual(window_seconds(conn, "Q", 30, 72), 90 * 60, delta=5)
        self.assertEqual(window_seconds(conn, "Other", 30, 72), 72 * 3600)


if __name__ == "__main__":
    unittest.main()
