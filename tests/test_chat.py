"""Dashboard chat bookkeeping (radar/chat.py).

Run from the jobradar folder:  .venv/bin/python -m unittest discover -s tests
"""
import os
from pathlib import Path

os.environ["JOBRADAR_CONFIG_DIR"] = str(Path(__file__).resolve().parent.parent / "config" / "examples")

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from radar import chat
from radar.store import connect

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(chat, "_path", lambda name: Path(self.dir.name, name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()


class Lock(ChatTestCase):
    def test_one_session_at_a_time_and_only_its_token_releases(self):
        first = chat.start(lambda: T0)
        self.assertTrue(first.startswith("STARTED "))
        self.assertTrue(chat.start(lambda: T0 + timedelta(minutes=5)).startswith("BUSY"))
        self.assertTrue(chat.done("someone-else").startswith("Not released"))
        self.assertEqual(chat.done(first.split()[1]), "Released the chat lock.")
        self.assertTrue(chat.start(lambda: T0 + timedelta(minutes=6)).startswith("STARTED"))

    def test_a_stale_lock_is_taken_over(self):
        chat.start(lambda: T0)
        later = T0 + timedelta(minutes=chat.STALE_MINUTES + 1)
        self.assertTrue(chat.start(lambda: later).startswith("STARTED"))


class Pace(unittest.TestCase):
    def test_quick_while_talking_then_slower_then_ends(self):
        self.assertEqual(chat.next_wait(timedelta(minutes=1), 45), 20)
        self.assertEqual(chat.next_wait(timedelta(minutes=10), 45), 60)
        self.assertEqual(chat.next_wait(timedelta(minutes=30), 45), chat.SLOW_SECONDS)
        self.assertIsNone(chat.next_wait(timedelta(minutes=45), 45))

    def test_every_wait_fits_in_one_bash_call(self):
        for minutes in range(0, 45):
            self.assertLessEqual(chat.next_wait(timedelta(minutes=minutes), 45), chat.MAX_WAIT_SECONDS)


class Wait(ChatTestCase):
    def test_wait_checks_until_quiet_and_a_message_resets_the_clock(self):
        slept = []
        chat.touch(T0)
        self.assertEqual(chat.wait(lambda: T0 + timedelta(minutes=2), slept.append), "CHECK")
        self.assertEqual(slept, [20])
        self.assertTrue(chat.wait(lambda: T0 + timedelta(minutes=50), slept.append).startswith("QUIET"))
        chat.touch(T0 + timedelta(minutes=50))
        self.assertEqual(chat.wait(lambda: T0 + timedelta(minutes=51), slept.append), "CHECK")

    def test_wait_says_when_facts_are_waiting_for_approval(self):
        conn = connect(":memory:")
        chat.propose(conn, [{"message_id": "m1", "section": "Beta Co", "text": "A fact."}])
        chat.touch(T0)
        self.assertIn("approvals", chat.wait(lambda: T0, lambda s: None, conn=conn))


class Facts(unittest.TestCase):
    def test_only_an_approved_fact_comes_back_to_add(self):
        conn = connect(":memory:")
        rows = chat.propose(conn, [{"message_id": "m1", "section": "Acme: PMO role", "text": "Fact one.",
                                    "note": "Can I add this?"},
                                   {"message_id": "m1", "section": "Beta Co", "text": "Fact two."}])
        self.assertEqual([r["data"]["kind"] for r in rows], ["fact", "fact"])
        self.assertEqual(rows[0]["data"]["fact"], "Fact one.")
        a, b = rows[0]["doc_id"], rows[1]["doc_id"]
        result = "\n".join([
            "=== BEGIN ARTIFACT DB ===",
            json.dumps({"id": a, "data": {"decision": "approve", "at": "2026-11-24T15:05:00Z"}, "version": 1}),
            json.dumps({"id": b, "data": {"decision": "reject", "at": "2026-11-24T15:06:00Z"}, "version": 1}),
            json.dumps({"id": "fake", "data": {"decision": "approve"}}),
            "=== END ===",
        ])
        todo, rejected = chat.read_approvals(conn, result)
        self.assertEqual([f["id"] for f in todo], [a])
        self.assertEqual(todo[0]["text"], "Fact one.")
        self.assertEqual(rejected, [b])
        self.assertIn("Marked", chat.mark_added(conn, a))
        self.assertEqual(chat.read_approvals(conn, result), ([], []))
        self.assertIn("isn't an approved fact", chat.mark_added(conn, b))

    def test_approved_facts_land_under_their_heading_or_bullet(self):
        with tempfile.TemporaryDirectory() as d:
            truth = Path(d, "truth.md")
            truth.write_text("# Truth\n\n## Roles\n\n### Acme: PM (2020 – 2024)\n- Billing system rollout.\n"
                             "  - Ran UAT.\n- Led a PMO.\n\n### Beta Co\n- Other work.\n\n## Tools used\nJira.\n")
            where = chat.add_to_truth(truth, "Acme: PM > Billing system", "Trained users.", "2026-11-24")
            self.assertIn("Billing system rollout", where)
            chat.add_to_truth(truth, "Acme", "Wrote the runbook.", "2026-11-24")
            chat.add_to_truth(truth, "Somewhere new", "A stray fact.", "2026-11-24")
            text = truth.read_text()
            self.assertIn("  - Ran UAT.\n  - Trained users. (Confirmed 2026-11-24 on the dashboard.)\n- Led a PMO.", text)
            self.assertIn("- Led a PMO.\n- Wrote the runbook. (Confirmed 2026-11-24 on the dashboard.)\n\n### Beta Co", text)
            self.assertTrue(text.rstrip().endswith("## Added from the dashboard\n- A stray fact. (Confirmed 2026-11-24 on the dashboard.)"))

    def test_apply_approved_adds_and_marks_each_fact_once(self):
        conn = connect(":memory:")
        rows = chat.propose(conn, [{"message_id": "m1", "section": "Tools used", "text": "Used Asana."}])
        fid = rows[0]["doc_id"]
        approval = json.dumps({"id": fid, "data": {"decision": "approve", "at": "2026-11-24T15:05:00Z"}})
        with tempfile.TemporaryDirectory() as d:
            truth = Path(d, "truth.md")
            truth.write_text("## Tools used\nJira.\n")
            todo, _ = chat.read_approvals(conn, approval)
            self.assertEqual(len(chat.apply_approved(conn, truth, "2026-11-24", todo)), 1)
            self.assertEqual(chat.read_approvals(conn, approval), ([], []))
            self.assertEqual(truth.read_text().count("Used Asana."), 1)

    def test_a_fact_needs_a_section_and_text(self):
        with self.assertRaises(SystemExit):
            chat.propose(connect(":memory:"), [{"message_id": "m1", "section": "", "text": "x"}])


class DriveUpload(ChatTestCase):
    def test_upload_fields_point_at_the_file_by_hash(self):
        f = Path(self.dir.name, "Resume.docx")
        f.write_bytes(b"word bytes")
        fields = chat.drive_upload(str(f))
        self.assertEqual(fields["title"], "Resume.docx")
        self.assertTrue(fields["contentMimeType"].endswith("wordprocessingml.document"))
        self.assertTrue(fields["base64Content"].startswith("sha256:"))
        self.assertTrue(fields["base64Content"].endswith(":" + f.resolve().as_posix()))
        self.assertNotIn("parentId", fields)
        chat.folder("folder123")
        self.assertEqual(chat.drive_upload(str(f))["parentId"], "folder123")


if __name__ == "__main__":
    unittest.main()
