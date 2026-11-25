"""Where Job Radar looks for your workspace (radar/config.py find_home).

Run from the jobradar folder:  .venv/bin/python -m unittest discover -s tests
"""
import os
from pathlib import Path

os.environ["JOBRADAR_CONFIG_DIR"] = str(Path(__file__).resolve().parent.parent / "config" / "examples")

import tempfile
import unittest

from radar import config


def workspace(folder: Path) -> Path:
    (folder / "config").mkdir(parents=True)
    (folder / "config" / "settings.yaml").write_text("{}\n")
    return folder


class FindHome(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name).resolve()
        self.code = self.tmp / "plugin-install"
        self.code.mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def test_jobradar_home_wins(self):
        ws = workspace(self.tmp / "ws")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(config.find_home(str(ws), elsewhere, self.code), ws)

    def test_the_current_folder_or_a_parent_with_config(self):
        ws = workspace(self.tmp / "Job Search")
        inside = ws / "applications" / "Acme - PM"
        inside.mkdir(parents=True)
        self.assertEqual(config.find_home(None, ws, self.code), ws)
        self.assertEqual(config.find_home(None, inside, self.code), ws)

    def test_a_checkout_with_its_own_config_still_works(self):
        workspace(self.code)
        other = self.tmp / "other"
        other.mkdir()
        self.assertEqual(config.find_home(None, other, self.code), self.code)

    def test_a_new_folder_becomes_the_workspace_for_init(self):
        new = self.tmp / "new"
        new.mkdir()
        self.assertEqual(config.find_home(None, new, self.code), new)

    def test_examples_come_from_the_code(self):
        self.assertEqual(config.EXAMPLES_DIR, config.CODE_ROOT / "config" / "examples")
        self.assertTrue((config.EXAMPLES_DIR / "settings.yaml").is_file())


class PermissionRules(unittest.TestCase):
    def test_rules_merge_into_settings_an_install_already_wrote(self):
        import json
        from radar.__main__ import merge_permission_rules
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, ".claude", "settings.json")
            path.parent.mkdir()
            path.write_text(json.dumps({"enabledPlugins": {"jobradar@jobradar": True},
                                        "permissions": {"allow": ["WebSearch"]}}))
            src = {"$schema": "x", "permissions": {"allow": ["Bash(./jr:*)", "WebSearch"], "ask": ["Edit(./config/truth.md)"]}}
            self.assertTrue(merge_permission_rules(path, src))
            got = json.loads(path.read_text())
            self.assertEqual(got["enabledPlugins"], {"jobradar@jobradar": True})
            self.assertEqual(got["permissions"]["allow"], ["WebSearch", "Bash(./jr:*)"])
            self.assertEqual(got["permissions"]["ask"], ["Edit(./config/truth.md)"])
            self.assertFalse(merge_permission_rules(path, src))   # nothing new the second time


if __name__ == "__main__":
    unittest.main()
