"""Loads the YAML files in jobradar/config and resolves project paths.

Everything about you lives outside the code: config/*.yaml and *.md, data/, your master résumé and your packets.
None of it goes in the repo. config/examples/ holds blank copies, and `./jr init` copies any missing ones into
config/. Set JOBRADAR_CONFIG_DIR to read another folder instead (the tests use config/examples).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "config" / "examples"
CONFIG_DIR = Path(os.environ.get("JOBRADAR_CONFIG_DIR") or ROOT / "config").expanduser().resolve()


def load(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f) or {}


def load_optional(name: str) -> dict:
    """Like load, but {} when the file doesn't exist yet (a fresh copy before `./jr init`)."""
    return load(name) if (CONFIG_DIR / f"{name}.yaml").exists() else {}


def project_path(rel: str) -> Path:
    """A path inside jobradar/, with its parent directory created."""
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def setting_path(key: str, default: str) -> Path:
    """A path from settings.yaml `paths`, relative to jobradar/ unless absolute (~ works too)."""
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
