"""Job Radar Chrome: a Chrome of its own for the apply task and any other Chrome DevTools work.

Your main Chrome asks you to allow remote debugging every time a new DevTools connection is made, and every
Claude session (each hourly apply run, each chat) makes its own. Unattended runs stall until you click Allow.
Job Radar Chrome is a separate Chrome profile started with a fixed debugging port, which Chrome lets a program
connect to without asking. That is also the trade: while it's open, any program on this Mac can control it. Keep
personal accounts (email, banking) out of it and use it only for job sites. Your main Chrome is never connected.

settings.yaml `job_radar_chrome`: `port` (default 9333) and `profile` (the profile folder). The Chrome DevTools
MCP server connects with `--browserUrl http://127.0.0.1:<port>` (README, step 6). `./jr chrome-up` starts it if
it isn't running; runs call it before their first DevTools tool.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional, Set, Tuple

# Not 9222: that is the port your main Chrome listens on when remote debugging is on in chrome://inspect.
DEFAULTS = {"port": 9333, "profile": "~/Library/Application Support/JobRadarChrome"}
WAIT_SECONDS = 20


def options(settings: dict) -> dict:
    opts = {**DEFAULTS, **(settings.get("job_radar_chrome") or {})}
    opts["port"] = int(opts["port"])
    opts["profile"] = Path(str(opts["profile"])).expanduser()
    return opts


def browser_on(port: int) -> Optional[str]:
    """The browser answering DevTools on this port ("Chrome/157..."), or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            return json.load(r).get("Browser") or "a browser"
    except (OSError, ValueError):
        return None


def _pids(cmd: list) -> Set[int]:
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return {int(p) for p in out.split() if p.isdigit()}


def profile_pids(profile: Path) -> Set[int]:
    """The Chrome processes using this profile folder: Job Radar Chrome's."""
    return _pids(["pgrep", "-f", "--", f"--user-data-dir={profile}"])   # "--": the pattern starts with dashes


def listener_pids(port: int) -> Set[int]:
    """The processes listening on 127.0.0.1:port, the address the MCP server connects to."""
    return _pids(["lsof", "-nP", "-t", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN"])


def launch(port: int, profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", "-na", "Google Chrome", "--args", f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"], check=False)


def up(settings: dict, wait: int = WAIT_SECONDS) -> Tuple[bool, str]:
    """Make sure Job Radar Chrome is running. (ok, message): UP, STARTED, or DOWN and why."""
    opts = options(settings)
    port, profile = opts["port"], opts["profile"]
    ours, listening = profile_pids(profile), listener_pids(port)
    if listening and not listening & ours:
        # Never hand the DevTools tools another browser, above all your main Chrome.
        return False, (f"DOWN: another program is listening on port {port}, not Job Radar Chrome. Set "
                       f"job_radar_chrome: port in config/settings.yaml, and the MCP server's --browserUrl, to a "
                       f"free port.")
    if listening:
        version = browser_on(port)
        if version:
            return True, f"UP: Job Radar Chrome ({version}) on port {port}."
    if ours:
        return False, (f"DOWN: Job Radar Chrome is open but not answering on 127.0.0.1:{port}. Quit it (Chrome > "
                       f"Quit while its window is in front) and run ./jr chrome-up.")
    launch(port, profile)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if listener_pids(port) & profile_pids(profile):
            version = browser_on(port)
            if version:
                return True, f"STARTED: Job Radar Chrome ({version}) on port {port}."
        time.sleep(0.5)
    return False, f"DOWN: Job Radar Chrome didn't answer on 127.0.0.1:{port} within {wait} seconds of starting."
