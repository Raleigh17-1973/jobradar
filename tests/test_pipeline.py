"""Filter and dashboard round trips on an in-memory database.

Run from the jobradar folder:  .venv/bin/python -m unittest discover -s tests
"""
import os
from pathlib import Path

# The tests read config/examples, never your own config.
os.environ["JOBRADAR_CONFIG_DIR"] = str(Path(__file__).resolve().parent.parent / "config" / "examples")

import json
import tempfile
import unittest
from unittest import mock

from radar import ats, config, dashboard, filters, store, google_jobs, history, network, packet, remote_check, watchlist
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
