"""Loads the YAML files in your workspace's config/ folder and resolves paths.

Two places matter:
- CODE_ROOT: where Job Radar's code lives. A git checkout, or the plugin's install folder, which Claude Code
  replaces on every update.
- ROOT: your workspace, which holds everything about you: config/*.yaml and *.md, data/, your master résumé and your
  packets. None of it goes in the repo, and none of it lives in the plugin's folder.

The workspace is JOBRADAR_HOME when that's set; otherwise the nearest folder, from the current one up, that has
config/settings.yaml; otherwise a checkout's own folder when it has one (the layout before the plugin); otherwise the
current folder, where `jr init` creates a new workspace. config/examples/ (in the code) holds blank copies, and
`jr init` copies any missing ones into the workspace. Set JOBRADAR_CONFIG_DIR to read another config folder instead
(the tests use config/examples).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

import yaml

CODE_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = CODE_ROOT / "config" / "examples"


def _is_workspace(folder: Path) -> bool:
    return (folder / "config" / "settings.yaml").is_file()


def find_home(env: Optional[str] = None, cwd: Optional[Path] = None, code_root: Path = CODE_ROOT) -> Path:
    """Your workspace folder (see the module docstring for the order)."""
    if env:
        return Path(env).expanduser().resolve()
    cwd = (cwd or Path.cwd()).resolve()
    stop = Path.home().resolve()
    for folder in (cwd, *cwd.parents):
        if _is_workspace(folder):
            return folder
        if folder == stop:
            break
    return code_root if _is_workspace(code_root) else cwd


ROOT = find_home(os.environ.get("JOBRADAR_HOME"))
CONFIG_DIR = Path(os.environ.get("JOBRADAR_CONFIG_DIR") or ROOT / "config").expanduser().resolve()


def load(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f) or {}


def load_optional(name: str) -> dict:
    """Like load, but {} when the file doesn't exist yet (a fresh copy before `./jr init`)."""
    return load(name) if (CONFIG_DIR / f"{name}.yaml").exists() else {}


def project_path(rel: str) -> Path:
    """A path inside your workspace, with its parent directory created."""
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def setting_path(key: str, default: str) -> Path:
    """A path from settings.yaml `paths`, relative to your workspace unless absolute (~ works too)."""
    value = (load_optional("settings").get("paths") or {}).get(key) or default
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def candidate_name() -> str:
    """Your name as profile.yaml has it. Packet file names and letter signatures use it."""
    contact = load_optional("profile").get("contact") or {}
    name = " ".join(str(contact.get(k) or "").strip() for k in ("legal_first_name", "legal_last_name")).strip()
    return name or "Candidate"


def home_area() -> dict:
    """preferences.yaml `home_area`: name, state and places where you can work on site."""
    return load_optional("preferences").get("home_area") or {}


def area_pattern(places: List[str]) -> Optional[re.Pattern]:
    """Matches a location naming one of your places. Short ones like "IL" must be whole words."""
    parts = [rf"\b{re.escape(p)}\b" if len(p) <= 3 else re.escape(p) for p in (str(x).strip() for x in places) if p]
    return re.compile("|".join(parts), re.I) if parts else None


def conflict_note() -> str:
    """The note jobs at watchlist companies marked `conflict: true` get (preferences.yaml `side_business`)."""
    side = load_optional("preferences").get("side_business") or {}
    if not side.get("name"):
        return "watchlist marks this company as a conflict of interest: disclose it"
    return side.get("conflict_note") or f"competes with {side['name']}: use the stronger disclosure"
