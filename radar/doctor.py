"""./jr doctor: checks each setup step and says exactly what to fix.

Read-only. It never prints or reads your Gmail password: the Keychain check only asks whether the item exists.
Each line is OK (done), FIX (needed before Job Radar works) or NOTE (optional, or something to check yourself).
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

OK, FIX, NOTE = "OK", "FIX", "NOTE"
Line = Tuple[str, str]

# Files `./jr init` copies. Job Radar can't work until the first group is your own; the second is optional.
_MUST_EDIT = ["settings.yaml", "profile.yaml", "preferences.yaml", "truth.md", "candidate.md"]
_OPTIONAL = ["my_rules.md", "history.yaml", "contacts.yaml", "watchlist.yaml", "vc_boards.yaml"]
_SAMPLE_NAME = "Alex Sample"
_WORD = Path("/Applications/Microsoft Word.app")


def check_python() -> List[Line]:
    lines: List[Line] = []
    if sys.version_info < (3, 9):
        lines.append((FIX, f"Python {sys.version.split()[0]} is too old; Job Radar needs 3.9 or newer."))
    missing = []
    for module in ("yaml", "bs4", "docx", "pypdf"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        lines.append((FIX, "Missing Python packages (" + ", ".join(missing) + "). Run: "
                           ".venv/bin/pip install -r requirements.txt"))
    return lines or [(OK, f"Python {sys.version.split()[0]} and the required packages")]


def check_config_files(config_dir: Path = None, examples_dir: Path = None) -> List[Line]:
    config_dir = config_dir or config.CONFIG_DIR
    examples_dir = examples_dir or config.EXAMPLES_DIR
    missing = [n for n in _MUST_EDIT + _OPTIONAL if not (config_dir / n).exists()]
    if missing:
        return [(FIX, "Missing config files (" + ", ".join(missing) + "). Run ./jr init (README step 2).")]
    lines: List[Line] = []
    same = [n for n in _MUST_EDIT if (examples_dir / n).exists()
            and (config_dir / n).read_bytes() == (examples_dir / n).read_bytes()]
    for n in same:
        lines.append((FIX, f"config/{n} is still the example. Fill in your own (README step 4, or ask Claude to "
                           f"follow prompts/setup.md with you)."))
    if not same:
        lines.append((OK, "Your settings, profile, preferences, truth file and scoring summary are filled in"))
    truth = (config_dir / "truth.md").read_text()
    if "truth.md" not in same and ("Example Software" in truth or "Example University" in truth):
        lines.append((FIX, "config/truth.md still has the sample person's facts (Example Software, Example "
                           "University). Packets may only use facts that are yours."))
    return lines


def check_profile() -> List[Line]:
    name = config.candidate_name()
    if name in ("Candidate", _SAMPLE_NAME):
        return [(FIX, "config/profile.yaml has no name of yours yet (contact: legal_first_name and "
                      "legal_last_name). Packet files and letters are signed with it.")]
    return [(OK, f"Your name for packets: {name}")]


def check_gmail(settings: dict, run=subprocess.run) -> List[Line]:
    gmail = settings.get("gmail") or {}
    address = str(gmail.get("address") or "").strip()
    service = gmail.get("keychain_service") or "job-radar-gmail"
    if not address or address == "you@gmail.com":
        return [(FIX, "Put the Gmail address that gets your job alerts in config/settings.yaml, gmail: address "
                      "(README step 3).")]
    # Without -w, `security` reports whether the item exists and never reads the password itself.
    found = run(["security", "find-generic-password", "-s", service, "-a", address],
                capture_output=True, text=True).returncode == 0
    if not found:
        return [(FIX, f"No Gmail app password in the Keychain for {address}. In Terminal, run this yourself "
                      f"(it asks for the password): security add-generic-password -a \"{address}\" -s \"{service}\" -w")]
    return [(OK, f"Gmail app password is in the Keychain for {address}. ./jr check tests the login.")]


def check_master(path: Path, role_keys: List[str], location_text: str) -> List[Line]:
    """The master résumé must be in the layout radar/packet.py reads (README step 4, "The master résumé layout")."""
    from . import packet
    if not path.exists():
        return [(FIX, f"No master résumé at {path}. Run ./jr init for the sample, then make it yours "
                      f"(README step 4).")]
    layout = "See \"The master résumé layout\" in the README, or start again from config/examples/Master Resume.docx."
    try:
        master = packet.Master(path)
    except StopIteration:
        return [(FIX, "The master résumé needs at least one role with a one-line intro under its company line, "
                      "and at least one bullet without a bold label. " + layout)]
    except Exception as e:  # the parser assumes the layout, so any slip shows up as an error here
        return [(FIX, f"Job Radar can't read the master résumé's layout ({type(e).__name__}: {e}). " + layout)]
    lines: List[Line] = []
    sections = [s for s in ("summary", "experience", "skills", "education") if s not in master.headings]
    if sections:
        lines.append((FIX, "The master résumé is missing these section headings: " + ", ".join(sections)
                           + ". Headings must be bold, in the color 1F3864. " + layout))
    if not role_keys:
        lines.append((FIX, "config/settings.yaml has no resume: role_keys. List one short key per role in your "
                           "master résumé's Experience section, top to bottom."))
    elif len(master.roles) != len(role_keys):
        lines.append((FIX, f"The master résumé has {len(master.roles)} roles but settings.yaml lists "
                           f"{len(role_keys)} role_keys ({', '.join(role_keys)}). They must match, top to bottom. "
                           "A role starts with a bold line holding the title, a tab, then the dates."))
    for role in master.roles:
        if role.company is None or not role.bullets:
            what = "company line" if role.company is None else "bullets"
            lines.append((FIX, f"Role '{role.key}' in the master résumé has no {what}. " + layout))
    contact = packet._text(master.contact)
    if location_text and location_text not in contact:
        lines.append((FIX, f"settings.yaml resume: location_text is \"{location_text}\", but the master's contact "
                           f"line doesn't contain it (it reads: {contact[:80]}). Use the exact text from that line."))
    if _SAMPLE_NAME in packet._text(master.name):
        lines.append((FIX, "The master résumé is still the sample (Alex Sample). Put your own résumé in this layout."))
    if not lines:
        lines.append((OK, f"Master résumé reads cleanly: {len(master.roles)} roles matching your role_keys"))
    return lines


def check_never_claim(settings: dict) -> List[Line]:
    terms = (settings.get("resume") or {}).get("never_claim") or []
    if not terms:
        return [(NOTE, "settings.yaml has no resume: never_claim list, so drafts aren't checked for claims you "
                       "can't make. Copy your truth file's \"Never claim\" items there.")]
    return [(OK, f"{len(terms)} never-claim terms are checked in every draft")]


def check_word() -> List[Line]:
    if _WORD.exists():
        return [(OK, "Microsoft Word is installed, for PDFs. The first build may ask you to let Word open "
                     "files in the applications folder; allow it, or later unattended builds stop there.")]
    return [(NOTE, "Microsoft Word isn't in /Applications, so packets stop at Word files (no PDFs or page counts).")]


def check_database(settings: dict) -> List[Line]:
    db = config.ROOT / ((settings.get("paths") or {}).get("db") or "data/jobs.db")
    return [(OK, "Jobs database exists")] if db.exists() else [(FIX, "No jobs database yet. Run ./jr init.")]


def check_dashboard(settings: dict) -> List[Line]:
    url = str(settings.get("dashboard_url") or "").strip()
    if not url:
        return [(FIX, "No dashboard yet. Publish it and save its link as dashboard_url (README step 5).")]
    if not url.startswith("https://claude.ai/"):
        return [(FIX, f"dashboard_url should be a claude.ai link; it is {url[:60]}")]
    return [(OK, "Dashboard link is set. Open it once in Claude to check it loads.")]


def check_searches(settings: dict, examples_dir: Path = None) -> List[Line]:
    queries = ((settings.get("linkedin_searches") or {}).get("queries")) or []
    try:
        import yaml
        example = yaml.safe_load(((examples_dir or config.EXAMPLES_DIR) / "settings.yaml").read_text())
        example = ((example.get("linkedin_searches") or {}).get("queries")) or []
    except (OSError, AttributeError):
        example = []
    if queries and queries == example:
        return [(FIX, "Your LinkedIn searches are still the examples. Change them to your titles in "
                      "config/settings.yaml, or remove linkedin_searches to use alert emails only.")]
    return [(OK, f"{len(queries)} LinkedIn searches")] if queries else [
        (NOTE, "No LinkedIn searches; Job Radar uses your alert emails and watchlist only.")]


def check_permissions(root: Path = None) -> List[Line]:
    root = root or config.ROOT
    restore = ("Recreate it with: jobradar init" if plugin_install() else
               "Restore it with: git checkout .claude/settings.json")
    if not (root / ".claude" / "settings.json").exists():
        return [(FIX, ".claude/settings.json is missing. It lets scheduled runs use Job Radar's commands "
                      f"without stopping for approval. {restore}")]
    return [(OK, ".claude/settings.json pre-approves Job Radar's commands for scheduled runs"),
            (NOTE, "Make this folder the scheduled tasks' folder, so they use those approvals, then run each task "
                   "once by hand and approve anything it still asks for (README step 7).")]


def plugin_install(code_root: Path = None) -> bool:
    """True when this code is the Claude Code plugin's copy rather than a git checkout."""
    code_root = code_root or config.CODE_ROOT
    parts = code_root.parts   # installed plugins live under ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>
    in_cache = any(parts[i:i + 2] == ("plugins", "cache") for i in range(len(parts) - 1))
    return in_cache and (code_root / ".claude-plugin" / "plugin.json").exists()


def check_apply_task(settings: dict, claude_json: Path = None) -> List[Line]:
    if not (settings.get("auto_apply") or {}).get("enabled"):
        return [(NOTE, "The apply task is off (auto_apply: enabled: false). That's the default.")]
    lines: List[Line] = []
    port = (settings.get("job_radar_chrome") or {}).get("port")
    if port == 9222:
        lines.append((FIX, "job_radar_chrome: port is 9222, which your main Chrome uses. Pick another, like 9333."))
    claude_json = claude_json or Path.home() / ".claude.json"
    try:
        registered = plugin_install() or "chrome-devtools" in claude_json.read_text()   # the plugin brings its own
    except OSError:
        registered = False
    if not registered:
        lines.append((FIX, "The apply task is on, but the chrome-devtools MCP server isn't added to Claude Code "
                           "(README step 6)."))
    return lines or [(OK, "Apply task is on, with Job Radar Chrome and the chrome-devtools server")]


def run_checks(settings: Optional[dict]) -> List[Line]:
    lines = check_python()
    lines += check_config_files()
    if settings is None:   # before ./jr init: nothing else can be checked yet
        return lines
    from . import packet   # loads settings.yaml's paths and résumé keys
    lines += check_profile()
    lines += check_gmail(settings)
    lines += check_master(packet.MASTER, packet.ROLE_KEYS, packet.LOCATION_TEXT)
    lines += check_never_claim(settings)
    lines += check_word()
    lines += check_database(settings)
    lines += check_dashboard(settings)
    lines += check_searches(settings)
    lines += check_permissions()
    lines += check_apply_task(settings)
    return lines


def report(lines: List[Line]) -> int:
    for status, text in lines:
        print(f"{status:<5}{text}")
    fixes = sum(1 for s, _ in lines if s == FIX)
    print("\n" + (f"{fixes} thing{'s' if fixes != 1 else ''} to fix." if fixes else "Ready. Next: README step 7, "
                  "the hourly run."))
    return 1 if fixes else 0
