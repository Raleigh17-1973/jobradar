"""Filter and dashboard round trips on an in-memory database.

Run from the jobradar folder:  .venv/bin/python -m unittest discover -s tests
"""
import os
from pathlib import Path

# The tests read config/examples, never your own config.
os.environ["JOBRADAR_CONFIG_DIR"] = str(Path(__file__).resolve().parent.parent / "config" / "examples")

import json
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest import mock

from radar import ats, config, dashboard, filters, store, google_jobs, history, network, packet, remote_check, watchlist
from radar import vc_boards
from radar.parsers import ParsedJob
from radar.store import connect, upsert_job

packet.MASTER = config.EXAMPLES_DIR / "Master Resume.docx"   # a made-up person

PREFS = {"title_keywords_include": ["program"]}


def status(conn, job_id):
    return conn.execute("SELECT status, filter_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()



def read_export(path):
    """data.json, with lists.json merged back in when the export split them (as the page does after first paint)."""
    data = json.loads(path.read_text())
    if data.get("lists_file"):
        data.update(json.loads(path.parent.joinpath(data["lists_file"]).read_text()))
    return data


class OutsideSearchArea(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def job(self, job_id, location, outside):
        signals = {"outside_search_area": [location]} if outside else {}
        return ParsedJob("linkedin", job_id, "Senior Program Manager", "Acme", location, signals=signals)

    def test_waits_for_remote_check_then_follows_it(self):
        job_id, _ = upsert_job(self.conn, self.job("1", "Dallas, TX", outside=True))
        filters.run(self.conn, PREFS)
        self.assertEqual(tuple(status(self.conn, job_id)), ("filtered", "location Dallas, TX: remote not checked yet"))
        for check, want in [({"verdict": "not remote"}, ("filtered", "location Dallas, TX is not remote or in the Springfield area")),
                            ({"verdict": "restricted", "why": "only for people living in Texas"},
                             ("filtered", "remote, but only for people living in Texas")),
                            ({"verdict": "remote"}, ("candidate", None))]:
            with self.subTest(check=check["verdict"]):
                self.conn.execute("UPDATE jobs SET signals = json_set(signals, '$.remote_check', json(?)) WHERE id = ?",
                                  (json.dumps(check), job_id))
                filters.run(self.conn, PREFS, recheck=True)
                self.assertEqual(tuple(status(self.conn, job_id)), want)

    def test_pay_rule_comes_before_a_remote_check(self):
        job = self.job("1", "Dallas, TX", outside=True)
        job.salary_max = 120000
        job_id, _ = upsert_job(self.conn, job)
        filters.run(self.conn, {**PREFS, "hard_drop_below_max": 150000})
        self.assertEqual(status(self.conn, job_id)["filter_reason"], "posted pay tops out at $120K")

    def test_kept_when_also_seen_in_the_area(self):
        job_id, _ = upsert_job(self.conn, self.job("1", "Dallas, TX", outside=True))
        upsert_job(self.conn, self.job("2", "Chicago, IL", outside=False))
        filters.run(self.conn, PREFS)
        self.assertEqual(status(self.conn, job_id)["status"], "candidate")


class BringBack(unittest.TestCase):
    def test_restored_job_skips_the_rules(self):
        conn = connect(":memory:")
        job_id, _ = upsert_job(conn, ParsedJob("linkedin", "1", "Director, Transformation", "RJW", "Chicago, IL"))
        filters.run(conn, PREFS)
        self.assertEqual(status(conn, job_id)["status"], "filtered")
        self.assertEqual([j["id"] for j in self.export(conn)["filtered"]], [job_id])

        with tempfile.TemporaryDirectory() as d:
            Path(d, f"j{job_id}.json").write_text(json.dumps({"decision": "restore", "at": "2026-11-03T20:00:00+00:00"}))
            self.assertEqual(dashboard.apply_decisions(conn, Path(d))["restored"], 1)
        filters.run(conn, PREFS, recheck=True)
        self.assertEqual(tuple(status(conn, job_id)), ("candidate", None))
        self.assertEqual(self.export(conn)["filtered"], [])

    def export(self, conn):
        with tempfile.TemporaryDirectory() as d:
            return read_export(dashboard.export(conn, Path(d, "dashboard"), {}))


class DashboardSync(unittest.TestCase):
    """data.json carries the page's jobs; decisions come back as db_query document lines."""

    def test_export_splits_cards_skips_and_hidden(self):
        conn = connect(":memory:")
        ids = [upsert_job(conn, ParsedJob("linkedin", str(n), "Senior Program Manager", f"Co{n}", "Remote"))[0] for n in range(3)]
        for job_id, verdict, st in zip(ids, ("apply", "skip", "maybe"), ("scored", "scored", "expired")):
            conn.execute("UPDATE jobs SET score = 70, verdict = ?, status = ? WHERE id = ?", (verdict, st, job_id))
        with tempfile.TemporaryDirectory() as d:
            data = read_export(dashboard.export(conn, Path(d, "dashboard"), {"updated_at": "x"}))
        self.assertEqual([j["id"] for j in data["jobs"]], [ids[0]])
        self.assertEqual([j["id"] for j in data["skipped"]], [ids[1]])
        self.assertEqual(data["summary"], {"updated_at": "x"})

    def test_age_limit_sets_the_filtered_window_and_goes_out_with_the_summary(self):
        conn = connect(":memory:")
        job_id = upsert_job(conn, ParsedJob("linkedin", "1", "Senior Program Manager", "Co", "Remote"))[0]
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
        conn.execute("UPDATE jobs SET status = 'filtered', filter_reason = 'title', first_seen = ? WHERE id = ?",
                     (ten_days_ago, job_id))
        with tempfile.TemporaryDirectory() as d:
            week = read_export(dashboard.export(conn, Path(d, "a"), {}, max_age_days=7))
            month = read_export(dashboard.export(conn, Path(d, "b"), {}, max_age_days=30))
        self.assertEqual((week["filtered"], week["filtered_days"]), ([], 7))
        self.assertEqual([e["id"] for e in month["filtered"]], [job_id])
        self.assertEqual(dashboard.summary(conn, 30, max_age_days=21)["max_age_days"], 21)
        self.assertEqual(dashboard.summary(conn, 30)["max_age_days"], dashboard.FILTERED_DAYS)

    def test_save_decisions_reads_result_lines_and_clears(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            Path(folder, "j5.json").write_text(json.dumps({"data": {"decision": "apply"}}))
            result = "\n".join([
                '{"docs": 2, "nextCursor": ""}',
                '<untrusted-document-abc>',
                '{"id": "j7", "data": {"decision": "maybe", "at": "2026-11-06T18:00:00.000Z", "job_id": 7}, "version": 1}',
                '{"id": "j5", "data": {"decision": null, "at": "2026-11-06T18:01:00.000Z", "job_id": 5}, "version": 3}',
                '</untrusted-document-abc>'])
            self.assertEqual(dashboard.save_decisions(folder, result), {"saved": 1, "cleared": 1, "kept_local": 0})
            self.assertFalse(Path(folder, "j5.json").exists())
            self.assertEqual(json.loads(Path(folder, "j7.json").read_text())["data"]["decision"], "maybe")
            since = dashboard.decisions_since(folder)
            self.assertTrue(since.endswith(".000Z"))

    def test_full_save_replaces_and_refuses_an_empty_paste(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.assertIsNone(dashboard.decisions_since(folder))
            Path(folder, "j9.json").write_text(json.dumps({"data": {"decision": "skip"}}))
            with self.assertRaises(SystemExit):
                dashboard.save_decisions(folder, "nothing here", full=True)
            self.assertTrue(Path(folder, "j9.json").exists())
            dashboard.save_decisions(folder, '{"id": "j3", "data": {"decision": "skip"}}', full=True)
            self.assertEqual(sorted(p.name for p in folder.glob("j*.json")), ["j3.json"])


    def test_a_decision_made_in_chat_outlasts_an_older_click_re_read(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            chat_skip = {"at": "2026-11-09T19:40:00.000Z", "decision": "skip", "job_id": 4, "local": True}
            Path(folder, "j4.json").write_text(json.dumps({"data": chat_skip}))
            older_apply = '{"id": "j4", "data": {"decision": "apply", "at": "2026-11-09T19:18:11.287Z", "job_id": 4}}'
            self.assertEqual(dashboard.save_decisions(folder, older_apply)["kept_local"], 1)
            self.assertEqual(json.loads(Path(folder, "j4.json").read_text())["data"]["decision"], "skip")
            dashboard.save_decisions(folder, older_apply, full=True)   # a full read keeps it too
            self.assertEqual(json.loads(Path(folder, "j4.json").read_text())["data"]["decision"], "skip")
            newer_maybe = '{"id": "j4", "data": {"decision": "maybe", "at": "2026-11-09T20:05:00.000Z", "job_id": 4}}'
            dashboard.save_decisions(folder, newer_maybe)
            self.assertEqual(json.loads(Path(folder, "j4.json").read_text())["data"],
                             {"decision": "maybe", "at": "2026-11-09T20:05:00.000Z", "job_id": 4})


class ExportForClaude(unittest.TestCase):
    """--limit counts only the jobs an export is for. Filtering after the limit once left hundreds of described
    jobs unscored behind newer ones still waiting for a description."""

    def export(self, db, out, **opts):
        from argparse import Namespace
        from radar import __main__ as cli
        args = dict(status="candidate", verdict=None, ids=None, limit=2, unresearched=False, missing_descriptions=False,
                    described_only=False, descriptions=False, desc_chars=4000, out=str(out))
        args.update(opts)
        cli.cmd_export(Namespace(**args), {"paths": {"db": str(db)}})
        return [j["id"] for j in json.loads(out.read_text())]

    def setUp(self):
        from radar import describe
        self.dir = tempfile.TemporaryDirectory()
        self.db, self.out = Path(self.dir.name, "jobs.db"), Path(self.dir.name, "out.json")
        conn = connect(str(self.db))
        self.old = upsert_job(conn, ParsedJob("linkedin", "1", "Senior Program Manager", "Old Co", "Remote"))[0]
        conn.execute("UPDATE jobs SET status = 'candidate', description = ?, first_seen = '2026-01-01T00:00:00+00:00' "
                     "WHERE id = ?", ("x" * describe.MIN_DESCRIPTION, self.old))
        self.new = []
        for n in range(2, 6):
            job_id = upsert_job(conn, ParsedJob("linkedin", str(n), "Senior Program Manager", f"New Co {n}", "Remote"))[0]
            conn.execute("UPDATE jobs SET status = 'candidate' WHERE id = ?", (job_id,))
            self.new.append(job_id)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_described_only_looks_past_newer_jobs_without_descriptions(self):
        self.assertEqual(self.export(self.db, self.out, described_only=True), [self.old])

    def test_missing_descriptions_leaves_out_described_jobs(self):
        got = self.export(self.db, self.out, missing_descriptions=True, limit=10)
        self.assertEqual(sorted(got), sorted(self.new))

    def test_unresearched_looks_past_researched_jobs(self):
        conn = connect(str(self.db))
        brief = json.dumps({"why": "x", "researched_at": "2026-11-20T00:00:00+00:00"})
        conn.execute("UPDATE jobs SET status = 'scored', verdict = 'apply', score = 90, brief = ? WHERE id IN (?, ?)",
                     (brief, self.new[0], self.new[1]))
        conn.execute("UPDATE jobs SET status = 'scored', verdict = 'apply', score = 80, brief = '{\"why\": \"y\"}' "
                     "WHERE id = ?", (self.old,))
        conn.commit()
        conn.close()
        got = self.export(self.db, self.out, status="scored", verdict="apply", unresearched=True)
        self.assertEqual(got, [self.old])


class SamePostingElsewhere(unittest.TestCase):
    """company|title misses a posting an aggregator files under another company name; its posting id doesn't."""
    GH = "https://northwind.ai/careers/jobs/8000000001?gh_jid=8000000001"

    def setUp(self):
        self.conn = connect(":memory:")

    def add(self, source, n, company, title="Chief of Staff - CFO", status="scored", apply_url=None, url=None):
        job_id, _ = upsert_job(self.conn, ParsedJob(source, str(n), title, company, "Remote", url=url))
        self.conn.execute("UPDATE jobs SET status = ?, apply_url = ? WHERE id = ?", (status, apply_url, job_id))
        return job_id

    def test_posting_ids_from_apply_links(self):
        cases = {
            "https://northwind.ai/careers/jobs/8000000001?gh_jid=8000000001": "greenhouse:8000000001",
            "https://job-boards.greenhouse.io/northwindsecurity/jobs/8000000001": "greenhouse:8000000001",
            "https://boards.greenhouse.io/embed/job_app?for=x&token=123456": "greenhouse:123456",
            "https://jobs.ashbyhq.com/acme/3364FF3B-f52d-4665-92da-26d1b19aec94/application":
                "ashby:3364ff3b-f52d-4665-92da-26d1b19aec94",
            "https://jobs.lever.co/cfgi/8dc31c89-d4d1-4afb-835d-0a09b5790db3/apply": "lever:8dc31c89-d4d1-4afb-835d-0a09b5790db3",
            "https://jobs.smartrecruiters.com/servicenow/744000158326194": "smartrecruiters:744000158326194",
            "https://finastra.wd3.myworkdayjobs.com/en-US/FINC/job/Atlanta/Senior-Director_R-123-4/apply":
                "workday:finastra:r-123-4",
            "https://careersnoa-avalara.icims.com/jobs/17727/job": "icims:careersnoa-avalara:17727",
            "https://www.linkedin.com/jobs/view/4400000002/": "linkedin:4400000002",
            "https://www.linkedin.com/jobs/view/chief-of-staff-at-northwind-4400000002": "linkedin:4400000002",
            "https://myjobs.adp.com/acmetravel/cx/job-details?reqId=5001234567890": "adp:acmetravel:5001234567890",
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=AB12-cd&jobId=512345&lang=en_US":
                "adp:ab12-cd:512345",
            "https://myjobs.adp.com/acmetravel/cx": None,
            "https://acme.com/careers": None,
            "unknown: not found on judge.com": None,
        }
        for url, key in cases.items():
            self.assertEqual(store.posting_key(url), key, url)

    def test_repeat_under_another_name_closes_and_stays_closed(self):
        applied = self.add("linkedin", 1, "Northwind AI", status="applied", apply_url=self.GH)
        repeat = self.add("google", 2, "Northwindsecurity", status="queued", apply_url=self.GH)
        self.assertEqual(store.mark_duplicates(self.conn), [(repeat, applied)])
        self.assertEqual(tuple(status(self.conn, repeat)), ("closed", f"duplicate of job {applied}"))
        self.assertEqual(status(self.conn, applied)["status"], "applied")
        moved = self.conn.execute("SELECT job_id FROM sightings WHERE source = 'google'").fetchone()["job_id"]
        self.assertEqual(moved, applied)
        # The Apply click you made on the repeat before it closed doesn't reopen it.
        with tempfile.TemporaryDirectory() as d:
            Path(d, f"j{repeat}.json").write_text(json.dumps({"data": {"decision": "apply", "at": "2026-11-09T19:18:11Z"}}))
            dashboard.apply_decisions(self.conn, Path(d))
        self.assertEqual(status(self.conn, repeat)["status"], "closed")
        # Seen again under the aggregator's name, the listing joins the job you applied to.
        job_id, created = upsert_job(self.conn, ParsedJob("google", "3", "Chief of Staff - CFO", "Northwindsecurity", "Remote"))
        self.assertEqual((job_id, created), (applied, False))
        self.assertEqual(store.mark_duplicates(self.conn), [])

    def test_the_copy_furthest_along_stays(self):
        older = self.add("linkedin", 1, "Northwind AI", status="scored")
        self.add_sighting_url(older, self.GH)
        newer = self.add("google", 2, "Northwindsecurity", status="queued", apply_url=self.GH)
        self.assertEqual(store.mark_duplicates(self.conn), [(older, newer)])

    def add_sighting_url(self, job_id, url):
        self.conn.execute("INSERT INTO sightings VALUES (?, 'board', ?, ?, NULL, '2026-11-09')", (job_id, url, url))

    def test_a_shared_careers_page_needs_the_same_title(self):
        a = self.add("linkedin", 1, "Acme", title="Senior Program Manager", apply_url="https://www.acme.com/careers/?utm_source=x")
        b = self.add("google", 2, "Acme Corp Holdings", title="Director, PMO", apply_url="https://acme.com/careers")
        self.assertEqual(store.mark_duplicates(self.conn), [])
        c = self.add("google", 3, "ACME Holdings", title="Sr. Program Manager", apply_url="https://acme.com/careers")
        self.assertEqual(store.mark_duplicates(self.conn), [(c, a)])
        self.assertEqual(status(self.conn, b)["status"], "scored")

    def test_skipped_and_applied_copies_are_left_alone(self):
        skipped = self.add("linkedin", 1, "Northwind AI", status="skipped", apply_url=self.GH)
        applied = self.add("google", 2, "Northwindsecurity", status="applied", apply_url=self.GH)
        self.assertEqual(store.mark_duplicates(self.conn), [])
        self.assertEqual(status(self.conn, skipped)["status"], "skipped")
        repeat = self.add("google", 3, "Northwind Security Inc", status="scored", apply_url=self.GH)
        self.assertEqual(store.mark_duplicates(self.conn), [(repeat, applied)])


class SameTextNoLink(unittest.TestCase):
    """Google and remote boards re-list a posting with no link, another company name and a padded title."""
    TEXT = " ".join(f"duty{i} for the program office" for i in range(60))

    def setUp(self):
        self.conn = connect(":memory:")

    def add(self, n, company, title, status="scored", verdict="maybe", text=TEXT):
        job_id, _ = upsert_job(self.conn, ParsedJob("google", str(n), title, company, "Remote"))
        self.conn.execute("UPDATE jobs SET status = ?, verdict = ?, description = ? WHERE id = ?",
                          (status, verdict, text, job_id))
        return job_id

    def test_padded_title_and_company_variant_close_into_the_copy_further_along(self):
        queued = self.add(1, "Kelso's Corporation", "Sr Manager, Program Management", status="packet_ready")
        repeat = self.add(2, "Kelso's - Corporate", "Sr Manager, Program Management", text=self.TEXT + " Apply today.")
        padded = self.add(3, "Kelso's", "Senior Manager, Program Management job at Kelso's in US National")
        self.assertEqual(store.mark_duplicates(self.conn), [(repeat, queued), (padded, queued)])
        self.assertEqual(tuple(status(self.conn, repeat)), ("closed", f"duplicate of job {queued}"))
        self.assertEqual(store.mark_duplicates(self.conn), [])

    def test_claudes_maybe_copy_outlasts_its_older_skip_copy(self):
        older = self.add(1, "Daylight Security", "Senior Program Manager, Brand & Marketing", verdict="skip")
        newer = self.add(2, "Daylight", "Senior Program Manager, Brand & Marketing, Remote Job")
        self.assertEqual(store.mark_duplicates(self.conn), [(older, newer)])

    def test_one_description_for_two_levels_or_roles_stays_two_jobs(self):
        self.add(1, "EY-Parthenon", "AI Business Solutions - Director")
        self.add(2, "EY-Parthenon", "AI Business Solutions - Senior Director")
        self.add(3, "NEOGOV", "Product Manager, Business Intelligence")
        self.add(4, "NEOGOV", "Product Manager, Analytics & Reporting")
        self.assertEqual(store.mark_duplicates(self.conn), [])

    def test_different_text_or_a_short_snippet_is_not_enough(self):
        self.add(1, "Acme", "Program Manager")
        self.add(2, "Acme", "Program Manager", text=" ".join(f"task{i} in the delivery group" for i in range(60)))
        self.add(3, "Beta", "Chief of Staff", text="Short snippet about the role.")
        self.add(4, "Beta", "Chief of Staff", text="Short snippet about the role.")
        self.assertEqual(store.mark_duplicates(self.conn), [])

    def test_two_reqs_are_not_joined_through_a_vaguer_title(self):
        utility = self.add(1, "ComEd", "Principal Project Manager - Transmission and Substation Utility")
        business = self.add(2, "ComEd", "Principal Project Manager - Transmission & Substation New Business")
        vague = self.add(3, "Exelon", "Principal Project Manager - Transmission & Substation")
        self.assertEqual(store.mark_duplicates(self.conn), [(vague, utility)])
        self.assertEqual(status(self.conn, business)["status"], "scored")


class PacketDecisions(unittest.TestCase):
    BUILT = "2026-11-04T15:00:00+00:00"

    def setUp(self):
        self.conn = connect(":memory:")
        self.job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", "1", "Senior Program Manager", "Initech", "Remote"))
        self.conn.execute("UPDATE jobs SET status = 'packet_ready', decided_at = '2026-11-04T13:00:00Z', signals = ? "
                          "WHERE id = ?", (json.dumps({"packet": {"built_at": self.BUILT}}), self.job_id))

    def decide(self, decision=None, at=None):
        with tempfile.TemporaryDirectory() as d:
            if decision:
                Path(d, f"j{self.job_id}.json").write_text(json.dumps({"decision": decision, "at": at}))
            dashboard.apply_decisions(self.conn, Path(d))
        return status(self.conn, self.job_id)["status"]

    def test_apply_click_keeps_the_packet(self):
        self.assertEqual(self.decide("apply", "2026-11-04T13:00:00.000Z"), "packet_ready")

    def test_skip_after_the_build_moves_it(self):
        self.assertEqual(self.decide("skip", "2026-11-04T16:00:00.000Z"), "skipped")

    def test_choice_older_than_the_build_is_ignored(self):
        self.assertEqual(self.decide("maybe", "2026-11-04T14:00:00.000Z"), "packet_ready")

    def test_cleared_apply_goes_back_to_review_and_reapply_finds_the_packet(self):
        self.assertEqual(self.decide(), "scored")
        self.assertEqual(self.decide("apply", "2026-11-04T17:00:00.000Z"), "packet_ready")

    def test_missing_decisions_folder_changes_nothing(self):
        with self.assertRaises(SystemExit):
            dashboard.apply_decisions(self.conn, Path("/nonexistent/decisions"))
        self.assertEqual(status(self.conn, self.job_id)["status"], "packet_ready")


class SignIn(unittest.TestCase):
    def test_systems(self):
        cases = {
            ("https://job-boards.greenhouse.io/acme/jobs/1", None): ("Greenhouse", "none"),
            ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1", None): ("Workday", "account"),
            ("https://www.google.com/about/careers/applications/jobs/results/1", None): ("Google", "signed_in"),
            ("https://initech.io/job-detail/?gh_jid=1", "greenhouse"): ("Greenhouse", "none"),
            ("https://jobs.lever.co", None): ("Lever", "none"),
            ("https://jobs.silkroad.com/Kofax/careers/Apply/MultiForm/222489", None): ("SilkRoad", "none"),
        }
        for (url, name), want in cases.items():
            with self.subTest(url=url):
                got = ats.sign_in(url, name)
                self.assertEqual((got["system"], got["need"]), want)
        self.assertIsNone(ats.sign_in("https://www.google.com/search?q=jobs"))
        self.assertIsNone(ats.sign_in("https://www.capitalonecareers.com/job/1"))


class RemoteCheck(unittest.TestCase):
    def test_classify(self):
        cases = {
            "A caregiver in this role works remotely Monday through Friday. To be considered for this position, "
            "caregivers must reside in Ohio.": ("restricted", "only for people living in Ohio"),
            "This is a fully remote role with the option to work hybrid if near our Salem, OR office.": ("remote", None),
            "This position is hybrid, three days a week in the office. Some ability to work remotely.": ("not remote", None),
            "This is not a remote position. Work location: Austin, TX.": ("not remote", None),
            "Remote (US). We can hire in the following states: AZ, FL, IL, TX.": ("remote", None),
            "Location: Remote. We are unable to hire in California, Illinois or New York.":
                ("restricted", "not open to Illinois residents"),
            "Remote-first team. Candidates must live within 50 miles of Denver.":
                ("restricted", "you must live within 50 miles of Denver"),
            "Join our team in Boston. Great benefits.": ("not remote", None),
            "This role can be held from one of our US hubs or remotely in the United States.": ("remote", None),
            "Location: Denver, CO / Remote Duration: 12 months": ("remote", None),
            "Positions are assigned a designation of In Office, Hybrid or Remote.": ("not remote", None),
            "Offices in SF and NYC. Perks: Work from Home reimbursement, flexible schedules.": ("not remote", None),
            "Great benefits. #LI-Remote #LI-Hybrid #LI-Onsite": ("not remote", None),
        }
        for text, (verdict, why) in cases.items():
            with self.subTest(text=text[:40]):
                got = remote_check.classify(text)
                self.assertEqual(got["verdict"], verdict)
                if why:
                    self.assertEqual(got["why"], why)
        self.assertEqual(remote_check.classify("", "Remote - US")["verdict"], "remote")
        self.assertEqual(remote_check.classify("", "Hybrid Remote - Seattle")["verdict"], "not remote")
        self.assertEqual(remote_check.classify("", "Waltham, MA; Remote-US-MA; Remote-US-MD")["why"],
                         "only for people living in Maryland or Massachusetts")

    def test_run_reads_title_board_then_linkedin(self):
        conn = connect(":memory:")
        ids = {}
        for n, title in enumerate(["Program Manager (Remote)", "Senior Program Manager", "Director, Program Management"]):
            job = ParsedJob("linkedin", str(n), title, f"Acme {n}", "Austin, TX",
                            signals={"outside_search_area": ["Austin, TX"], "linkedin_listed": "2099-01-01"})
            ids[title], _ = upsert_job(conn, job)
        filters.run(conn, PREFS)
        board = ats.Posting("greenhouse", "Senior Program Manager", "https://x", "Hybrid - Austin, TX", "Hybrid. " * 100)

        def fake_board(conn, ids=None, matches=None, by_title=ids):
            matches[by_title["Senior Program Manager"]] = board
        with mock.patch.object(ats, "run", side_effect=fake_board), \
             mock.patch.object(remote_check.liveness, "check_linkedin",
                               return_value=("open", {"description": "Fully remote across the US. " * 30})) as linkedin:
            counts = remote_check.run(conn, limit=5, spacing=0)
        filters.run(conn, PREFS, recheck=True)
        self.assertEqual(linkedin.call_count, 1)
        self.assertEqual((counts["from title"], counts["from employer board"], counts["from LinkedIn"]), (1, 1, 1))
        self.assertEqual([status(conn, ids[t])["status"] for t in ids], ["candidate", "filtered", "candidate"])


class HistoryMatch(unittest.TestCase):
    def test_company_named_with_or_without_ai(self):
        conn = connect(":memory:")
        history.connect_schema(conn)
        conn.execute("INSERT INTO applications (company, company_key, role, role_key, status, status_at) "
                     "VALUES ('Northwind', 'northwind', 'Technical Program Manager', 'technical program manager', "
                     "'rejected', '2026-08-28')")
        northwind, _ = upsert_job(conn, ParsedJob("linkedin", "1", "Chief of Staff - CFO", "Northwind AI", "United States"))
        other, _ = upsert_job(conn, ParsedJob("linkedin", "2", "Chief of Staff", "Northwind Labs", "United States"))
        history.match_jobs(conn)
        signals = lambda job_id: json.loads(conn.execute("SELECT signals FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
        self.assertEqual(signals(northwind)["history"]["company_summary"], "Technical Program Manager: rejected 2026-08-28")
        self.assertNotIn("history", signals(other))


class NetworkMatch(unittest.TestCase):
    CSV = ("Notes:\n\"When exporting your connection data, you may notice that some of the email addresses are missing.\"\n\n"
           "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
           "Ana,Lee,https://www.linkedin.com/in/ana,,Northwind,Senior Engineer,01 Mar 2024\n"
           "Bo,Kim,https://www.linkedin.com/in/bo,,\"Northwind, Inc.\",Technical Recruiter,02 Mar 2024\n"
           "Cy,Ng,https://www.linkedin.com/in/cy,,Self-employed,Consultant,03 Mar 2024\n")

    def test_import_and_match(self):
        conn = connect(":memory:")
        northwind, _ = upsert_job(conn, ParsedJob("linkedin", "1", "Chief of Staff", "Northwind AI", "United States"))
        hidden, _ = upsert_job(conn, ParsedJob("linkedin", "2", "Program Manager", "Confidential", "Chicago, IL"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "Connections.csv")
            path.write_text(self.CSV + "Dee,Fox,https://www.linkedin.com/in/dee,,Confidential,Director,04 Mar 2024\n")
            self.assertEqual(network.import_file(conn, path), 4)
        self.assertEqual(network.match_jobs(conn), {"jobs": 1, "companies": 1})
        signals = lambda job_id: json.loads(conn.execute("SELECT signals FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
        net = signals(northwind)["network"]
        self.assertEqual(net["count"], 2)
        self.assertEqual([p["name"] for p in net["people"]], ["Bo Kim", "Ana Lee"])
        self.assertTrue(net["people"][0]["recruiting"])
        self.assertNotIn("network", signals(hidden))


class GoogleJobs(unittest.TestCase):
    CARDS = [
        {"i": 0, "lines": ["L", "Senior Program Manager, iQueue", "LeanTaaS, Inc.", "Chicago, IL • via Learn4Good",
                               "18 hours ago", "120K–150K a year", "Full-time", "No degree mentioned"]},
        {"i": 1, "lines": ["AI Program Manager", "Acme", "Austin, TX (+1 other) • via Dice", "2 days ago", "Full-time"]},
        {"i": 2, "lines": ["Senior Manager Supplier Quality", "DSJ Global", "Chicago, IL • via LinkedIn", "2 days ago"]},
    ]

    def test_due_every_few_hours(self):
        conn = connect(":memory:")
        self.assertEqual(google_jobs.due(conn, 3), (True, None))
        google_jobs.import_cards(conn, {"name": "q"}, [], PREFS)
        self.assertFalse(google_jobs.due(conn, 3)[0])
        conn.execute("UPDATE emails SET received_at = datetime('now', '-170 minutes') || '+00:00' WHERE source = 'google_jobs'")
        self.assertTrue(google_jobs.due(conn, 3)[0])   # 2h50m counts as due, so an hourly run isn't skipped by minutes

    def test_import_then_details(self):
        conn = connect(":memory:")
        query = {"name": "remote test", "keep_locations": ["chicago", "anywhere"]}
        counts = google_jobs.import_cards(conn, query, self.CARDS, PREFS)
        self.assertEqual((counts["listings"], counts["new"], counts["outside_area"]), (3, 3, 1))
        self.assertEqual(counts["need_details"], [0, 1])
        job = conn.execute("SELECT * FROM jobs WHERE title LIKE 'Senior Program Manager%'").fetchone()
        self.assertEqual((job["salary_min"], job["salary_max"], job["location"]), (120000, 150000, "Chicago, IL"))
        self.assertEqual(json.loads(job["signals"])["google_via"], "Learn4Good")

        details = [{"i": 0, "lines": self.CARDS[0]["lines"], "description": "Job description " + "Lead inpatient flow rollouts. " * 30,
                    "apply": ["https://www.learn4good.com/jobs/1", "https://www.linkedin.com/jobs/view/senior-pm-at-leantaas-4485090136",
                              "https://job-boards.greenhouse.io/leantaas/jobs/123"]}]
        self.assertEqual(google_jobs.save_details(conn, details),
                         {"described": 1, "linkedin": 1, "apply_links": 1, "unmatched": 0})
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        self.assertTrue(job["description"].startswith("Lead inpatient"))
        self.assertEqual(job["apply_url"], "https://job-boards.greenhouse.io/leantaas/jobs/123")
        self.assertEqual(conn.execute("SELECT source_job_id FROM sightings WHERE job_id = ? AND source = 'linkedin'",
                                      (job["id"],)).fetchone()[0], "4485090136")


class Watchlist(unittest.TestCase):
    def test_location(self):
        ok = ["United States", "Remote - USA", "Springfield, IL", "Remote, Canada; Remote, United States", "US-TX-Remote", ""]
        out = ["Salt Lake City, Utah, United States", "New York, New York, USA", "Canada - Remote (ON, AB, BC, or NS Only)",
               "San Francisco, CA | New York City, NY", "West Coast - United States"]
        for loc in ok:
            self.assertTrue(watchlist.location_ok(loc), loc)
        for loc in out:
            self.assertFalse(watchlist.location_ok(loc), loc)

    def test_run_keeps_fresh_fitting_titles(self):
        conn = connect(":memory:")
        fresh = "2099-01-01T00:00:00Z"
        board = [ats.Posting("ashby", "Senior Program Manager, AI", "https://jobs.ashbyhq.com/acme/1", "Remote - US",
                             "Lead programs. " * 60, 180000, 220000, fresh),
                 ats.Posting("ashby", "Program Manager, Ops", "https://jobs.ashbyhq.com/acme/2", "Austin, TX", "Ops. " * 200,
                             posted_at=fresh),
                 ats.Posting("ashby", "Senior Accountant", "https://jobs.ashbyhq.com/acme/3", "Remote - US", posted_at=fresh),
                 ats.Posting("ashby", "Program Manager, Old", "https://jobs.ashbyhq.com/acme/4", "Remote - US",
                             posted_at="2020-01-01T00:00:00Z")]
        cfg = {"companies": [{"name": "Acme", "ats": "ashby", "slug": "acme", "conflict": True}]}
        with mock.patch.dict(ats.FETCHERS, {"ashby": lambda slug: board}):
            counts = watchlist.run(conn, cfg, PREFS)
        self.assertEqual((counts["fit"], counts["too_old"], counts["new"]), (2, 1, 2))
        job = conn.execute("SELECT * FROM jobs WHERE title = 'Senior Program Manager, AI'").fetchone()
        self.assertEqual((job["status"], job["ats"], job["apply_url"], job["salary_max"]),
                         ("candidate", "ashby", "https://jobs.ashbyhq.com/acme/1", 220000))
        self.assertIn("conflict_of_interest", json.loads(job["signals"]))
        austin = conn.execute("SELECT status, filter_reason FROM jobs WHERE title = 'Program Manager, Ops'").fetchone()
        self.assertEqual(tuple(austin), ("filtered", "location Austin, TX: remote not checked yet"))

    def test_gem_board(self):
        posts = [{"title": "Technical Project Manager ", "absolute_url": "https://jobs.gem.com/acme/abc",
                  "location": {"name": "United States - Remote"}, "first_published_at": "2026-09-18T16:04:01Z",
                  "content_plain": "Backed by $100M in funding.\nEstimated salary range:\n $80,000 to $130,000\nLead installs."}]
        with mock.patch.object(ats, "_get_json", return_value=posts):
            [p] = ats.gem("acme")
        self.assertEqual((p.ats, p.title, p.url, p.location, p.salary_min, p.salary_max, p.posted_at),
                         ("gem", "Technical Project Manager", "https://jobs.gem.com/acme/abc", "United States - Remote",
                          80000, 130000, "2026-09-18T16:04:01Z"))
        self.assertEqual(ats._pay_in_text("$150K - $190K a year"), (150000, 190000))
        self.assertEqual(ats._pay_in_text("$45 - $60 an hour"), (None, None))
        self.assertEqual(ats.sign_in("https://jobs.gem.com/acme/abc")["system"], "Gem")


class VcBoards(unittest.TestCase):
    """Getro portfolio boards: newest first, read back to the age limit, one job per listing across firms."""

    def _job(self, jid, title, org, created, work_mode="remote", locations=("United States",), pay=None):
        j = {"id": jid, "title": title, "url": f"https://boards.greenhouse.io/{org.lower()}/jobs/{jid}",
             "created_at": created, "work_mode": work_mode, "locations": list(locations),
             "searchable_locations": list(locations), "organization": {"name": org, "stage": "series_b"}}
        if pay:
            j.update(compensation_amount_min_cents=pay[0] * 100, compensation_amount_max_cents=pay[1] * 100,
                     compensation_currency="USD", compensation_period="year")
        return j

    def test_where(self):
        remote = {"work_mode": "remote", "locations": ["United States"]}
        other_state = {"work_mode": "remote", "locations": ["California, USA"], "searchable_locations": ["California, USA"]}
        home_state = {"work_mode": "remote", "locations": ["Texas, USA"], "searchable_locations": ["Illinois, USA"]}
        office = {"work_mode": "hybrid", "locations": ["Springfield, IL, USA", "Austin, TX, USA"]}
        far = {"work_mode": "on_site", "locations": ["Austin, TX, USA"]}
        country = {"work_mode": "on_site", "locations": ["United States"]}
        self.assertEqual(vc_boards.where(remote, "Illinois"), "Remote, United States")
        self.assertIsNone(vc_boards.where(other_state, "Illinois"))
        self.assertEqual(vc_boards.where(home_state, "Illinois"), "Remote, United States")
        self.assertEqual(vc_boards.where(office, "Illinois"), "Springfield, IL, USA")
        self.assertIsNone(vc_boards.where(far, "Illinois"))
        self.assertEqual(vc_boards.where(country, "Illinois"), vc_boards.COUNTRY)
        self.assertTrue(vc_boards.other_company("https://careers.airbnb.com/positions/1/", "HotelTonight"))
        for url, company in [("https://boards.greenhouse.io/hoteltonight/jobs/1", "HotelTonight"),
                             ("https://jobs.paloaltonetworks.com/x", "Cortex by Palo Alto Networks"),
                             ("https://stripe.com/jobs/listing/1", "Stripe"),
                             ("https://www.comeet.com/jobs/vastdata/43.001/senior-manager", "VAST Data"),
                             ("https://careers.example.org/acme-robotics/123", "Acme Robotics")]:
            self.assertFalse(vc_boards.other_company(url, company), url)
        self.assertEqual(vc_boards.searches("Illinois")[1], {"searchable_locations": ["Illinois, USA"]})

    def test_run_pages_back_to_the_age_limit_and_merges_firms(self):
        from datetime import datetime, timezone
        t = datetime.now(timezone.utc).timestamp()
        fresh, old = int(t - 3600), int(t - 40 * 86400)
        page = [self._job(1, "Senior Program Manager, AI", "Acme", fresh, pay=(180000, 220000)),
                self._job(2, "Senior Accountant", "Acme", fresh),
                self._job(3, "Director, Program Management", "Beta", fresh, work_mode="on_site", locations=["Austin, TX, USA"]),
                dict(self._job(4, "Program Manager, Payments", "Venmo", fresh), organization={"name": "Venmo", "stage": "acquisition"})]
        page += [self._job(10 + i, "Program Manager", "Gamma", fresh) for i in range(vc_boards.PAGE_SIZE - len(page))]
        old_page = [self._job(500, "Program Manager, Old", "Delta", old)]
        calls = []

        us_only = [self._job(600, "Senior Program Manager, Remote-eligible", "Eta", fresh, work_mode="on_site"),
                   self._job(601, "Program Manager, Office", "Theta", fresh, work_mode="on_site"),
                   dict(self._job(602, "Lead Program Manager, Design", "HotelTonight", fresh, work_mode="on_site"),
                        url="https://careers.airbnb.com/positions/602/")]
        postings = {600: "This role is fully remote anywhere in the US. " * 20, 601: "You'll work on-site in Austin. " * 30}

        def fake_post(url, body):
            calls.append((url, body["page"], json.dumps(body["filters"], sort_keys=True)))
            if body["filters"] == vc_boards.ALL_US:
                return {"results": {"jobs": us_only if body["page"] == 0 else []}}
            if "searchable_locations" in body["filters"] and "work_mode" not in body["filters"]:
                return {"results": {"jobs": [], "count": 0}}       # the home-state search finds nothing here
            return {"results": {"jobs": page if body["page"] == 0 else old_page}}

        def fake_page(url):
            return {"text": postings.get(int(url.rstrip("/").split("/")[-1]), "")}

        cfg = {"boards": [{"name": "Accel", "id": "8672"}, {"name": "Menlo", "id": "767"}], "max_pages": 5}
        conn = connect(":memory:")
        with mock.patch.object(vc_boards, "_post_json", side_effect=fake_post), \
             mock.patch.object(vc_boards.describe, "fetch_url", side_effect=fake_page):
            counts = vc_boards.run(conn, cfg, PREFS)
        remote_calls = [c for c in calls if "work_mode" in c[2]]
        self.assertEqual(sorted(c[1] for c in remote_calls), [0, 0, 1, 1])     # page 1 was older than the limit: stop
        self.assertEqual((counts["boards"], counts["unique"], counts["outside"], counts["acquired"], counts["other_company"]),
                         (2, vc_boards.PAGE_SIZE + 3, 1, 1, 1))
        self.assertEqual((counts["country_remote"], counts["country_not_remote"]), (1, 1))
        eta = conn.execute("SELECT * FROM jobs WHERE company = 'Eta'").fetchone()
        self.assertEqual((eta["location"], eta["status"]), ("Remote, United States", "candidate"))
        self.assertEqual(json.loads(eta["signals"])["remote_check"]["verdict"], "remote")
        self.assertGreaterEqual(len(eta["description"]), 500)
        job = conn.execute("SELECT * FROM jobs WHERE title = 'Senior Program Manager, AI'").fetchone()
        self.assertEqual((job["company"], job["location"], job["salary_max"], job["apply_url"], job["status"]),
                         ("Acme", "Remote, United States", 220000, "https://boards.greenhouse.io/acme/jobs/1", "candidate"))
        signals = json.loads(job["signals"])
        self.assertEqual((signals["vc_boards"], signals["company_stage"]), (["Accel", "Menlo"], "series_b"))
        self.assertIsNotNone(job["posted_at"])
        titles = {r[0] for r in conn.execute("SELECT title FROM jobs")}
        self.assertNotIn("Senior Accountant", titles)
        self.assertNotIn("Director, Program Management", titles)          # an office in Austin: outside the search area
        self.assertNotIn("Program Manager, Old", titles)
        self.assertNotIn("Program Manager, Payments", titles)     # an acquired company's (the buyer's) job
        self.assertFalse(vc_boards.due(conn, 6))
        self.assertNotIn("Program Manager, Office", titles)     # its posting says on-site
        # The next run pages back only to the last read, not the whole age limit.
        seen_since = []
        with mock.patch.object(vc_boards, "getro_pages", side_effect=lambda bid, f, since, n: seen_since.append(since) or []):
            vc_boards.run(conn, cfg, PREFS)
            vc_boards.run(conn, cfg, PREFS, full=True)
        n = len(seen_since) // 2
        self.assertLess((datetime.now(timezone.utc) - min(seen_since[:n])).total_seconds(), 3 * 3600)
        self.assertGreater((datetime.now(timezone.utc) - max(seen_since[n:])).total_seconds(), 13 * 86400)


class WordOnlyPacket(unittest.TestCase):
    def test_builds_without_opening_word(self):
        resume = {"title_line": "Program and PMO Leader", "keywords_line": "Governance", "summary": "Leads programs.",
                  "roles": [{"key": "example_pmo", "bullets": [{"text": "Runs AI governance intake."}]}],
                  "skills": [{"label": "Program", "items": "Governance"}]}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(packet, "to_pdf", side_effect=AssertionError("opened Word")):
            Path(d, "resume.json").write_text(json.dumps(resume))
            Path(d, "ats_terms.json").write_text(json.dumps(["governance", "budgeting"]))
            report = packet.build(Path(d), "Acme Inc", word_only=True)
            self.assertEqual(report["files"], ["Alex Sample Resume Acme.docx"])
            self.assertEqual(report["ats"], {"found": ["governance"], "missing": ["budgeting"]})
            self.assertFalse(report["pdfs"])
            self.assertEqual(report["pages"], {})


if __name__ == "__main__":
    unittest.main()


class AfterYouApply(unittest.TestCase):
    """Follow-ups, the Sent it button, outcome linking and phone alerts."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.conn = connect(":memory:")
        history.connect_schema(self.conn)
        self.ago = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", "1", "Senior Program Manager", "Acme", "Remote"))
        self.conn.execute("UPDATE jobs SET status = 'applied', applied_at = ?, score = 82 WHERE id = ?", (self.ago(9), job_id))
        self.job_id = job_id

    def test_follow_up_due_after_a_week_without_reply(self):
        from radar import followup
        self.assertEqual([j["id"] for j in followup.followups_due(self.conn)], [self.job_id])
        # A rejection email linked to this application takes it off the list.
        self.conn.execute("INSERT INTO history_emails (gm_msgid, subject, received_at, kind, company, role, classified_at) "
                          "VALUES ('m1', 'Update', ?, 'rejection', 'Acme', 'Senior Program Manager', ?)", (self.ago(1), self.ago(1)))
        history.rebuild_applications(self.conn)
        history.match_jobs(self.conn)
        self.assertEqual(followup.outcome_of(self.conn.execute("SELECT * FROM jobs").fetchone()), "rejected")
        self.assertEqual(followup.followups_due(self.conn), [])

    def test_drafts_are_checked_and_sent_it_is_recorded(self):
        from radar import followup
        with mock.patch.object(packet, "claim_check", return_value=[]):
            bad = followup.save_followups(self.conn, [{"id": self.job_id, "message": "Hi there — checking in."}])
            self.assertTrue(any("em dash" in i for i in next(iter(bad.values()))))
            ok = followup.save_followups(self.conn, [{"id": self.job_id, "message": "Hi there, checking in on my application."}])
        self.assertEqual(ok, {})
        with tempfile.TemporaryDirectory() as d:
            dashboard.save_decisions(Path(d), json.dumps({"id": f"j{self.job_id}", "data": {"decision": "followed_up",
                                                                                          "at": "2026-11-12T15:00:00.000Z"}}))
            counts = dashboard.apply_decisions(self.conn, Path(d))
        self.assertEqual(counts["follow_ups"], 1)
        row = self.conn.execute("SELECT status, signals FROM jobs WHERE id = ?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(json.loads(row["signals"])["follow_up"]["status"], "sent")

    def test_alert_names_each_item_once(self):
        from radar import followup
        job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", "2", "Director, PMO", "Globex", "Remote"))
        self.conn.execute("UPDATE jobs SET status = 'scored', verdict = 'apply', score = 88, brief = ?, signals = ? WHERE id = ?",
                          (json.dumps({"tier": "strong"}), json.dumps({"age_days": 0}), job_id))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(followup, "ALERT_STATE", Path(d) / "state.json"):
            first = followup.alerts(self.conn)
            self.assertIn("Globex: Director, PMO (88)", first)
            self.assertLessEqual(len(first), followup.ALERT_MAX)
            self.assertIsNone(followup.alerts(self.conn))

    def test_one_referral_ask_per_company(self):
        from radar import followup
        people = {"network": {"people": [{"name": "Pat Lee", "position": "Recruiter", "url": "https://x"}]}}
        ids = []
        for n, (title, score) in enumerate((("Senior TPM", 84), ("Program Director", 78)), start=10):
            job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", str(n), title, "Oracle", "Remote"))
            self.conn.execute("UPDATE jobs SET status = 'queued', score = ?, signals = ? WHERE id = ?",
                              (score, json.dumps(people), job_id))
            ids.append(job_id)
        due = followup.referrals_due(self.conn)
        self.assertEqual([j["id"] for j in due], [ids[0]])
        self.assertEqual([o["id"] for o in due[0]["also_open"]], [ids[1]])
        with mock.patch.object(packet, "claim_check", return_value=[]):
            self.assertEqual(followup.save_referrals(self.conn, [{"id": ids[0], "contact": people["network"]["people"][0],
                                                                  "message": "Hi Pat, two roles caught my eye.",
                                                                  "also_open": due[0]["also_open"]}]), {})
        covered = json.loads(self.conn.execute("SELECT signals FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0])
        self.assertEqual(covered["referral"]["covered_by"], ids[0])
        self.assertEqual(followup.referrals_due(self.conn), [])


class MessagesToClaude(unittest.TestCase):
    """Messages typed on the dashboard: saved once, answered by a run, sent back in data.json, announced once."""

    def setUp(self):
        from radar import messages
        self.m = messages
        self.conn = connect(":memory:")
        history.connect_schema(self.conn)
        self.job_id = upsert_job(self.conn, ParsedJob("linkedin", "1", "Director, AI Product", "Acme", "Remote"))[0]

    def rows(self, *items):
        lines = ['{"docs": 3, "nextCursor": ""}', "<untrusted-document-abc>"]
        lines += [json.dumps({"id": i, "data": d, "version": 1}) for i, d in items]
        return "\n".join(lines + ["</untrusted-document-abc>"])

    def test_saved_once_and_read_again_from_a_day_before_the_newest(self):
        self.assertIsNone(self.m.since_filter(self.conn))
        text = self.rows(("ma1", {"text": "Why 58?", "at": "2026-11-19T20:00:00.000Z", "job_id": self.job_id}),
                         ("ma2", {"text": "  ", "at": "2026-11-19T20:01:00.000Z"}),
                         ("j5", {"decision": "apply"}),
                         ("ma3", {"text": "Skip staffing firms", "at": "2026-11-19T20:02:00.000Z", "job_id": "7"}))
        self.assertEqual(self.m.save_messages(self.conn, text), {"new": 2, "seen": 0, "skipped": 1})
        self.assertEqual(self.m.save_messages(self.conn, text)["seen"], 2)
        self.assertEqual(self.m.since_filter(self.conn), [{"field": "at", "op": ">=", "value": "2026-11-18T20:02:00.000Z"}])
        general = self.conn.execute("SELECT job_id FROM messages WHERE id = 'ma3'").fetchone()[0]
        self.assertIsNone(general)   # a job id that isn't a number is dropped, not trusted

    def test_waiting_messages_carry_the_job_and_the_thread(self):
        self.m.save_messages(self.conn, self.rows(
            ("ma1", {"text": "Is it remote?", "at": "2026-11-19T20:00:00.000Z", "job_id": self.job_id}),
            ("ma2", {"text": "Yes, re-score it", "at": "2026-11-19T21:00:00.000Z", "job_id": self.job_id})))
        self.m.save_replies(self.conn, [{"id": "ma1", "reply": "The posting says remote. Want a re-score?"}])
        waiting = self.m.waiting(self.conn)
        self.assertEqual([w["id"] for w in waiting], ["ma2"])
        self.assertEqual(waiting[0]["job"]["company"], "Acme")
        self.assertEqual(waiting[0]["earlier"][0]["reply"], "The posting says remote. Want a re-score?")

    def test_bad_replies_are_named_and_good_ones_reach_the_page(self):
        self.m.save_messages(self.conn, self.rows(("ma1", {"text": "Apply to this for me", "at": "2026-11-19T20:00:00.000Z"})))
        result = self.m.save_replies(self.conn, [{"id": "ma1", "reply": "That needs you in Claude Code.", "status": "needs_you"},
                                                 {"id": "zz", "reply": "x"}, {"id": "ma1", "reply": "x", "status": "done"}])
        self.assertEqual(result["saved"], ["ma1"])
        self.assertEqual(len(result["problems"]), 2)
        with tempfile.TemporaryDirectory() as d:
            data = read_export(dashboard.export(self.conn, Path(d, "dashboard"), {}))
        self.assertEqual([(m["id"], m["status"], m["job_id"]) for m in data["messages"]], [("ma1", "needs_you", None)])

    def test_the_phone_alert_mentions_a_reply_once(self):
        from radar import followup
        self.m.save_messages(self.conn, self.rows(("ma1", {"text": "Why 58?", "at": "2026-11-19T20:00:00.000Z"})))
        self.m.save_replies(self.conn, [{"id": "ma1", "reply": "Pay was below your floor."}])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(followup, "ALERT_STATE", Path(d) / "state.json"):
            self.assertIn("Claude answered your message", followup.alerts(self.conn))
            self.assertIsNone(followup.alerts(self.conn))


class AppliedRepostNoEmail(unittest.TestCase):
    """ADP sent no confirmation for an application, and an aggregator re-listed the same req under a new title with
    no apply link. The packet said "no applications on record"."""
    ADP = "https://myjobs.adp.com/acmetravel/cx/job-details?reqId=5001234567890"

    def setUp(self):
        self.conn = connect(":memory:")
        history.connect_schema(self.conn)

    def add(self, n, company, title, applied_at=None, apply_url=None, status="queued"):
        job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", str(n), title, company, "Remote"))
        self.conn.execute("UPDATE jobs SET status = ?, applied_at = ?, apply_url = ? WHERE id = ?",
                          ("applied" if applied_at else status, applied_at, apply_url, job_id))
        return job_id

    def history_of(self, job_id):
        history.match_jobs(self.conn)
        return json.loads(self.conn.execute("SELECT signals FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]).get("history")

    def test_job_radar_applications_count_as_history(self):
        applied = self.add(1, "Acme Travel", "Sr. Director, Global Technology PMO Leader", "2026-11-07T02:48:00+00:00", self.ADP)
        repost = self.add(2, "Acme Travel, Inc.", "Global Tech PMO Leader: Portfolio, AI & Delivery")
        past = self.history_of(repost)
        self.assertIsNone(past["same_role"])
        self.assertEqual(past["company_summary"], "Sr. Director, Global Technology PMO Leader: applied 2026-11-07")
        self.assertIsNone(self.history_of(applied), "an application isn't its own history")

    def test_only_an_identical_title_is_the_same_role(self):
        self.add(1, "Globex", "Staff Technical Program Manager", "2026-11-13T15:30:00+00:00")
        security = self.add(2, "Globex", "Staff Technical Program Manager, Security")
        again = self.add(3, "Globex Inc", "Placeholder")   # upsert would merge an identical title into job 1
        self.conn.execute("UPDATE jobs SET title = 'Staff Technical Program Manager' WHERE id = ?", (again,))
        self.assertIsNone(self.history_of(security)["same_role"])
        self.assertEqual(self.history_of(again)["same_role"], "applied 2026-11-13")

    def test_an_email_record_is_not_counted_twice(self):
        self.conn.execute("INSERT INTO applications (company, company_key, role, role_key, applied_at, status) VALUES "
                          "('Acme', 'acme', 'Sr Program Manager (Req 55)', 'sr program manager req 55', '2026-11-08', 'applied')")
        self.add(1, "Acme", "Senior Program Manager", "2026-11-07T20:00:00+00:00")
        other = self.add(2, "Acme", "Director, PMO")
        self.assertEqual(self.history_of(other)["company_apps"], 1)

    def test_packet_build_finds_the_posting_you_applied_to(self):
        applied = self.add(1, "Acme Travel", "Sr. Director, Global Technology PMO Leader", "2026-11-07T02:48:00+00:00", self.ADP)
        repost = self.add(2, "Acme Travel, Inc.", "Global Tech PMO Leader: Portfolio, AI & Delivery")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "answers.md").write_text(
                "# Acme Travel\n> Pay source: https://www.learn4good.com/jobs/123\n"
                f"**Apply at:** {self.ADP}\nNews: https://skift.com/2025/09/26/x\n")
            Path(d, "posting.txt").write_text(f"Title\nSource: ADP MyJobs, {self.ADP}.\n")
            links = packet.apply_links(Path(d))
        self.assertEqual(links, [self.ADP])
        found, key = store.applied_repeat(self.conn, repost, links)
        self.assertEqual((found["id"], key), (applied, "adp:acmetravel:5001234567890"))
        self.assertIsNone(store.applied_repeat(self.conn, applied, links), "a job doesn't repeat itself")
        self.assertIsNone(store.applied_repeat(self.conn, repost, ["https://acme.com/careers", None]))

    def test_an_applied_sighting_link_counts(self):
        applied = self.add(1, "Northwind AI", "Chief of Staff - CFO", "2026-11-05T10:00:00+00:00")
        gh = "https://job-boards.greenhouse.io/northwindsecurity/jobs/8000000001"
        self.conn.execute("INSERT INTO sightings VALUES (?, 'board', 'x1', ?, NULL, '2026-11-05')", (applied, gh))
        repost = self.add(2, "Northwindsecurity", "Chief of Staff")
        self.assertEqual(store.applied_repeat(self.conn, repost, [gh + "?gh_src=abc"])[0]["id"], applied)

    def test_extra_problems_keep_a_build_from_being_clean(self):
        resume = {"title_line": "Program and PMO Leader", "keywords_line": "Governance", "summary": "Leads programs.",
                  "roles": [{"key": "example_pmo", "bullets": [{"text": "Runs AI governance intake."}]}],
                  "skills": [{"label": "Program", "items": "Governance"}]}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(packet, "to_pdf", side_effect=AssertionError("opened Word")):
            Path(d, "resume.json").write_text(json.dumps(resume))
            report = packet.build(Path(d), "Acme Inc", word_only=True, extra_problems=["you already applied to this posting"])
        self.assertEqual(report["problems"][0], "you already applied to this posting")


class AutoApply(unittest.TestCase):
    """The apply task takes only Apply clicks on no-account sites with a packet, one run at a time, and leaves
    anything it can't finish for the user."""
    TEXT = " ".join(f"duty{i} for the program office" for i in range(60))
    ON = {"auto_apply": {"enabled": True}}

    def setUp(self):
        from radar import auto_apply, followup
        self.aa, self.followup = auto_apply, followup
        self.conn = connect(":memory:")
        history.connect_schema(self.conn)
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "decisions").mkdir()
        self.decisions = d / "decisions"
        for name, path in (("_lock_path", d / "lock"), ("_state_path", d / "state.json"),
                           ("_decisions_dir", self.decisions)):
            patcher = mock.patch.object(auto_apply, name, return_value=path)
            patcher.start()
            self.addCleanup(patcher.stop)
        alert_state = mock.patch.object(followup, "ALERT_STATE", d / "alerts.json")
        alert_state.start()
        self.addCleanup(alert_state.stop)
        self.addCleanup(self.dir.cleanup)
        self.n = 0

    def add(self, company, url="https://job-boards.greenhouse.io/acme/jobs/1", packet=True, decision="apply",
            status="packet_ready", text=None, **signals):
        self.n += 1
        job_id, _ = upsert_job(self.conn, ParsedJob("linkedin", str(self.n), "Director, PMO", company, "Remote"))
        if packet:
            signals["packet"] = {"folder": f"applications/{company} - Director PMO", "pdfs": False}
        self.conn.execute("UPDATE jobs SET status = ?, apply_url = ?, signals = ?, description = ?, decided_at = ? "
                          "WHERE id = ?", (status, url, json.dumps(signals), text or f"{company} " + self.TEXT[:200],
                                           f"2026-11-13T10:{self.n:02d}:00Z", job_id))
        if decision:
            (self.decisions / f"j{job_id}.json").write_text(json.dumps({"data": {"decision": decision}}))
        return job_id

    def test_takes_only_apply_clicks_on_no_account_sites_with_a_packet(self):
        ready = self.add("Acme")
        self.add("Globex", url="https://globex.wd1.myworkdayjobs.com/en-US/careers/job/1")
        self.add("Initech", packet=False, status="queued")
        self.add("Umbrella", decision="maybe")
        self.add("Hooli", paused=True)
        ok, text = self.aa.start(self.conn, self.ON)
        self.assertTrue(ok)
        self.assertIn("1 ready", text)
        self.assertIn("needs your account step (Workday)", text)
        self.assertIn("needs a packet", text)
        self.assertIn("no Apply click on the dashboard", text)
        self.assertIn("paused", text)
        job, _ = self.aa.next_job(self.conn, self.ON)
        self.assertEqual((job["id"], job["system"], job["pdfs"]), (ready, "Greenhouse", False))
        self.assertTrue(job["folder"].endswith("applications/Acme - Director PMO"))
        job, message = self.aa.next_job(self.conn, self.ON)
        self.assertIsNone(job)
        self.assertIn("nothing else", message)

    def test_off_unless_turned_on(self):
        self.add("Acme")
        ok, message = self.aa.start(self.conn, {})
        self.assertFalse(ok)
        self.assertIn("off", message)

    def test_one_run_at_a_time_and_a_dead_runs_lock_goes_stale(self):
        from datetime import datetime, timedelta, timezone
        self.add("Acme")
        self.assertTrue(self.aa.start(self.conn, self.ON)[0])
        ok, message = self.aa.start(self.conn, self.ON)
        self.assertFalse(ok)
        self.assertIn("another apply run", message)
        self.aa.end(self.conn)
        self.assertTrue(self.aa.start(self.conn, self.ON)[0])
        later = datetime.now(timezone.utc) + timedelta(minutes=self.aa.STALE_MINUTES + 1)
        self.assertTrue(self.aa.start(self.conn, self.ON, at=later)[0])

    def test_count_and_time_limits(self):
        from datetime import datetime, timedelta, timezone
        self.add("Acme")
        self.add("Globex", url="https://jobs.lever.co/globex/1")
        settings = {"auto_apply": {"enabled": True, "max_per_run": 1, "minutes": 45}}
        self.aa.start(self.conn, settings)
        self.assertIsNotNone(self.aa.next_job(self.conn, settings)[0])
        job, message = self.aa.next_job(self.conn, settings)
        self.assertIsNone(job)
        self.assertIn("1 applications are done", message)
        self.aa.end(self.conn)
        self.aa.start(self.conn, self.ON)
        job, message = self.aa.next_job(self.conn, self.ON, at=datetime.now(timezone.utc) + timedelta(minutes=46))
        self.assertIsNone(job)
        self.assertIn("minutes are up", message)

    def test_held_job_waits_for_clear_and_the_alert_names_it_once(self):
        job_id = self.add("Acme")
        self.aa.hold(self.conn, job_id, "CAPTCHA on Submit", "The filled form is open in Chrome.")
        ok, message = self.aa.start(self.conn, self.ON)
        self.assertFalse(ok)
        self.assertIn("waiting on you: CAPTCHA on Submit", message)
        self.assertIn("Finish applying: Acme (CAPTCHA on Submit)", self.followup.alerts(self.conn))
        self.assertIsNone(self.followup.alerts(self.conn))
        self.assertEqual([r["id"] for r in self.aa.held_jobs(self.conn)], [job_id])
        self.assertTrue(self.aa.clear(self.conn, job_id))
        self.assertTrue(self.aa.start(self.conn, self.ON)[0])
        self.assertEqual(self.aa.next_job(self.conn, self.ON)[0]["id"], job_id)

    def test_same_posting_as_an_application_is_held_not_sent_twice(self):
        applied = self.add("Northwind Labs", status="applied", text=self.TEXT)
        twin = self.add("Northwind", url="https://job-boards.greenhouse.io/northwind/jobs/2", text=self.TEXT + " Apply now.")
        other = self.add("Acme", url="https://jobs.lever.co/acme/3")
        self.aa.start(self.conn, self.ON)
        self.assertEqual(self.aa.next_job(self.conn, self.ON)[0]["id"], other)
        self.assertIsNone(self.aa.next_job(self.conn, self.ON)[0])
        reason = json.loads(self.conn.execute("SELECT signals FROM jobs WHERE id = ?", (twin,)).fetchone()[0])["auto_apply"]
        self.assertIn(f"same posting as Northwind Labs ({applied})", reason["reason"])

    def test_chrome_down_alerts_once_until_it_works_again(self):
        self.add("Acme")
        self.aa.start(self.conn, self.ON)
        self.assertIn("ALERT", self.aa.end(self.conn, chrome_down=True))
        self.aa.start(self.conn, self.ON)
        self.assertNotIn("ALERT", self.aa.end(self.conn, chrome_down=True))
        self.aa.start(self.conn, self.ON)
        self.aa.end(self.conn)
        self.aa.start(self.conn, self.ON)
        self.assertIn("ALERT", self.aa.end(self.conn, chrome_down=True))

    def test_nothing_ready_stops_before_chrome(self):
        # An empty hour never starts or connects to Chrome.
        self.add("Globex", url="https://globex.wd1.myworkdayjobs.com/en-US/careers/job/1")
        ok, message = self.aa.start(self.conn, self.ON)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("STOP: nothing is ready to apply"))
        self.assertIn("needs your account step (Workday)", message)
        self.assertIn("no apply run started", self.aa.next_job(self.conn, self.ON)[1])   # no lock was taken

    def test_an_empty_hour_ends_a_chrome_down_stretch(self):
        job_id = self.add("Acme")
        self.aa.start(self.conn, self.ON)
        self.assertIn("ALERT", self.aa.end(self.conn, chrome_down=True))
        self.aa.hold(self.conn, job_id, "CAPTCHA on Submit")
        self.assertFalse(self.aa.start(self.conn, self.ON)[0])                  # nothing ready: Chrome not needed
        self.aa.clear(self.conn, job_id)
        self.aa.start(self.conn, self.ON)
        self.assertIn("ALERT", self.aa.end(self.conn, chrome_down=True))       # needed again and down: alert again

    def test_end_sums_up_the_run(self):
        self.add("Acme")
        self.add("Globex", url="https://jobs.lever.co/globex/1")
        self.aa.start(self.conn, self.ON)
        sent = self.aa.next_job(self.conn, self.ON)[0]          # freshest Apply click first: Globex
        self.conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (sent["id"],))
        left = self.aa.next_job(self.conn, self.ON)[0]
        self.aa.hold(self.conn, left["id"], "2 questions for you")
        text = self.aa.end(self.conn)
        self.assertIn(f"1 submitted: {sent['company']} ({sent['id']})", text)
        self.assertIn(f"1 left for you: {left['company']} ({left['id']}): 2 questions for you", text)


class RunLock(unittest.TestCase):
    """One scheduled run at a time: a later run waits for the earlier one, and only the run that took the lock frees it."""

    def setUp(self):
        from radar import run_lock
        self.rl = run_lock
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(run_lock, "_lock_path", return_value=Path(self.dir.name) / "run.lock")
        patcher.start()
        self.addCleanup(patcher.stop)

    def token(self, message):
        self.assertTrue(message.startswith("STARTED "))
        return message.split()[1]

    def test_a_later_run_waits_then_gives_up_and_cant_free_the_lock(self):
        ok, message = self.rl.start(wait_minutes=0)
        self.assertTrue(ok)
        first = self.token(message)
        clock = [datetime.now(timezone.utc)]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)
        ok, message = self.rl.start(wait_minutes=1, now=lambda: clock[0], sleep=sleep)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("WAITING"))
        self.assertEqual(len(sleeps), 3)
        self.assertIn("Not released", self.rl.done("20000101T000000"))
        self.assertEqual(self.rl.done(first), "Released the run lock.")
        self.assertEqual(self.rl.done(first), "No run lock to release.")

    def test_the_waiting_run_starts_when_the_first_finishes(self):
        first = self.token(self.rl.start(wait_minutes=0)[1])
        ok, message = self.rl.start(wait_minutes=1, sleep=lambda s: self.rl.done(first))
        self.assertTrue(ok)
        self.assertNotEqual(self.token(message), first)

    def test_a_dead_runs_lock_goes_stale(self):
        self.rl.start(wait_minutes=0)
        later = datetime.now(timezone.utc) + timedelta(minutes=self.rl.STALE_MINUTES + 1)
        ok, message = self.rl.start(wait_minutes=0, now=lambda: later)
        self.assertTrue(ok)
        self.assertIn("stopped without releasing it", message)
        self.assertEqual(self.rl.done(self.token(message)), "Released the run lock.")


class ClaimCheckNumbers(unittest.TestCase):
    """A number is backed only by the same value and unit standing on its own in the truth file or master."""
    TRUTH = ("Co-founded the PMO across $20M+ and ~375 contributors. About 85% AI co-authored, about 4,700 commits.\n"
             "Reset accountability across 12+ teams. (Confirmed 2026-11-15.) B2B SaaS.\n")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        truth = Path(tmp.name) / "truth.md"
        truth.write_text(self.TRUTH)
        for patch in (mock.patch.object(packet, "TRUTH", truth),
                      mock.patch.object(packet.Master, "text", return_value="Cut reporting from 40 hours to 3.")):
            patch.start()
            self.addCleanup(patch.stop)

    def numbers(self, text, context=""):
        return [i.split(": ", 1)[1] for i in packet.claim_check(text, context) if i.startswith("number")]

    def test_a_number_inside_a_bigger_one_is_not_backed(self):
        self.assertEqual(self.numbers("Grew revenue 37%."), ["37%"])        # 375 contributors
        self.assertEqual(self.numbers("Cut it to 4 hours."), ["4"])          # 4,700 and 40
        self.assertEqual(self.numbers("Across 15 teams."), ["15"])           # only in a date

    def test_same_value_and_unit_is_backed_however_it_is_written(self):
        self.assertEqual(self.numbers("A $20M portfolio, $20 million, 20M+, ~375 people, 4,700 commits, 12 teams, "
                                      "85% of the code, 40 hours to 3."), [])

    def test_a_different_unit_is_not_backed(self):
        self.assertEqual(sorted(self.numbers("A $20B portfolio with 85 engineers.")), ["$20B", "85"])

    def test_dates_times_and_digits_inside_words_are_skipped(self):
        self.assertEqual(self.numbers("I applied on November 6 and Nov 2, 2026; the call is at 3:30 on 2026-11-16. "
                                      "B2B and ISO42001."), [])

    def test_a_letter_may_quote_the_posting(self):
        letter = "The role's 10% travel works for me."
        self.assertEqual(self.numbers(letter), ["10%"])
        self.assertEqual(self.numbers(letter, context="Travel: up to 10%."), [])

    def test_never_claim_terms_are_still_flagged(self):
        self.assertIn("never-claim term 'used by millions'", packet.claim_check("An app used by millions."))

    def test_brief_checks_numbers_only_where_it_tells_your_story(self):
        from radar import followup
        brief = ("# Interview prep: Acme\n## Their problem in one breath\nAbout 19% of orders come back.\n"
                 "## Your stories, matched to their challenges\nI cut returns 19%.\n"
                 "## Company context, in case it comes up\nFounded in 2001; $3B in sales.\n")
        self.assertEqual(followup.prep_claims(brief), ["number not in truth file or master résumé: 19%"])
        self.assertEqual(followup.prep_claims(brief.replace("I cut returns 19%.", "I cut returns.")), [])


class JobRadarChrome(unittest.TestCase):
    """./jr chrome-up hands the DevTools tools only Job Radar Chrome's own port, starting it when it isn't running."""
    S = {"job_radar_chrome": {"port": 9333, "profile": "/tmp/jr-test-profile"}}

    def run_up(self, ours, listening, version="Chrome/157", after_launch=None):
        from radar import chrome
        calls = {"launch": 0}
        state = {"ours": set(ours), "listening": set(listening)}

        def launch(port, profile):
            calls["launch"] += 1
            if after_launch:
                state["ours"], state["listening"] = after_launch

        with mock.patch.object(chrome, "profile_pids", side_effect=lambda p: state["ours"]), \
             mock.patch.object(chrome, "listener_pids", side_effect=lambda p: state["listening"]), \
             mock.patch.object(chrome, "browser_on", return_value=version), \
             mock.patch.object(chrome, "launch", side_effect=launch), \
             mock.patch.object(chrome.time, "sleep"):
            ok, message = chrome.up(self.S, wait=1)
        return ok, message, calls["launch"]

    def test_never_takes_a_port_another_program_owns(self):
        ok, message, launched = self.run_up(ours=set(), listening={726})       # e.g. your main Chrome
        self.assertFalse(ok)
        self.assertIn("another program is listening on port 9333", message)
        self.assertEqual(launched, 0)

    def test_up_when_its_own_process_answers(self):
        ok, message, launched = self.run_up(ours={500, 501}, listening={500})
        self.assertEqual((ok, launched), (True, 0))
        self.assertTrue(message.startswith("UP: Job Radar Chrome (Chrome/157) on port 9333"))

    def test_starts_it_when_it_isnt_running(self):
        ok, message, launched = self.run_up(ours=set(), listening=set(), after_launch=({600}, {600}))
        self.assertEqual((ok, launched), (True, 1))
        self.assertTrue(message.startswith("STARTED"))

    def test_open_without_its_port_says_to_quit_it(self):
        ok, message, launched = self.run_up(ours={500}, listening=set())
        self.assertEqual((ok, launched), (False, 0))
        self.assertIn("Quit it", message)

    def test_default_port_is_not_the_main_chromes(self):
        from radar import chrome
        self.assertNotEqual(chrome.options({})["port"], 9222)


class NeverClaim(unittest.TestCase):
    """Never-claim terms come from settings.yaml, so each person sets their own."""

    def test_terms_match_whole_words_with_star_endings(self):
        pattern = packet.never_claim_pattern(["strategy consult*", "p&l", "lean", "board presentation"])
        self.assertTrue(pattern.search("ran a strategy consulting practice"))
        self.assertTrue(pattern.search("Owned the P&L"))
        self.assertIsNone(pattern.search("leaning on the team"))
        self.assertIsNone(pattern.search("board presentations"))   # list both forms when both matter

    def test_no_terms_means_no_never_claim_issues(self):
        self.assertIsNone(packet.never_claim_pattern([]))


class Doctor(unittest.TestCase):
    """./jr doctor names each setup step that's left, in words a new user can act on."""
    SAMPLE = config.EXAMPLES_DIR / "Master Resume.docx"
    KEYS = ["example_pmo", "example_agency", "example_health"]

    def setUp(self):
        from radar import doctor
        self.doctor = doctor

    def fixes(self, lines):
        return [text for status, text in lines if status == self.doctor.FIX]

    def test_example_files_are_flagged_until_filled_in(self):
        fixes = self.fixes(self.doctor.check_config_files(config.EXAMPLES_DIR, config.EXAMPLES_DIR))
        self.assertTrue(any("config/truth.md is still the example" in t for t in fixes))

    def test_sample_facts_left_in_an_edited_truth_file_are_flagged(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            for f in config.EXAMPLES_DIR.glob("*"):
                shutil.copy(f, d)
            truth = Path(d) / "truth.md"
            truth.write_text(truth.read_text() + "\n- Led a team of five at My Real Company.\n")
            fixes = self.fixes(self.doctor.check_config_files(Path(d), config.EXAMPLES_DIR))
        self.assertTrue(any("sample person's facts" in t for t in fixes))
        self.assertFalse(any("truth.md is still the example" in t for t in fixes))

    def test_missing_files_point_to_init(self):
        with tempfile.TemporaryDirectory() as d:
            fixes = self.fixes(self.doctor.check_config_files(Path(d), config.EXAMPLES_DIR))
        self.assertEqual(len(fixes), 1)
        self.assertIn("./jr init", fixes[0])

    def test_sample_master_reads_but_is_flagged_as_the_sample(self):
        fixes = self.fixes(self.doctor.check_master(self.SAMPLE, self.KEYS, "(Metro area)"))
        self.assertEqual(len(fixes), 1)
        self.assertIn("still the sample", fixes[0])

    def test_role_keys_must_match_the_roles(self):
        fixes = self.fixes(self.doctor.check_master(self.SAMPLE, self.KEYS[:2], "(Metro area)"))
        self.assertTrue(any("has 3 roles but settings.yaml lists 2 role_keys" in t for t in fixes))

    def test_location_text_must_be_on_the_contact_line(self):
        fixes = self.fixes(self.doctor.check_master(self.SAMPLE, self.KEYS, "(Nowhere)"))
        self.assertTrue(any("location_text" in t for t in fixes))

    def test_missing_master_says_where_it_looked(self):
        fixes = self.fixes(self.doctor.check_master(Path("/nonexistent/Master.docx"), self.KEYS, ""))
        self.assertIn("/nonexistent/Master.docx", fixes[0])

    def test_gmail_checks_never_read_the_password(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=44)

        self.assertTrue(self.fixes(self.doctor.check_gmail({"gmail": {"address": "you@gmail.com"}}, run=fake_run)))
        self.assertEqual(calls, [])   # the placeholder address is caught before the Keychain is asked
        fixes = self.fixes(self.doctor.check_gmail({"gmail": {"address": "me@example.com"}}, run=fake_run))
        self.assertIn("security add-generic-password", fixes[0])
        self.assertNotIn("-w", calls[0])
        ok = self.doctor.check_gmail({"gmail": {"address": "me@example.com"}},
                                     run=lambda cmd, **kw: mock.Mock(returncode=0))
        self.assertEqual(ok[0][0], self.doctor.OK)

    def test_apply_task_needs_the_devtools_server(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "claude.json"
            empty.write_text("{}")
            fixes = self.fixes(self.doctor.check_apply_task({"auto_apply": {"enabled": True}}, claude_json=empty))
        self.assertTrue(any("chrome-devtools" in t for t in fixes))
        self.assertFalse(self.fixes(self.doctor.check_apply_task({"auto_apply": {"enabled": False}})))

    def test_report_exit_code(self):
        with mock.patch("builtins.print"):
            self.assertEqual(self.doctor.report([(self.doctor.OK, "fine")]), 0)
            self.assertEqual(self.doctor.report([(self.doctor.FIX, "not fine")]), 1)

    def test_example_searches_are_flagged(self):
        example = config.load("settings")   # the tests read config/examples
        self.assertTrue(self.fixes(self.doctor.check_searches(example)))
        mine = {"linkedin_searches": {"queries": [{"name": "Data Scientist", "keywords": "Data Scientist"}]}}
        self.assertFalse(self.fixes(self.doctor.check_searches(mine)))
