---
name: setup
description: Set up Job Radar in the current folder: workspace, interview, config files, dashboard and scheduled tasks. Use when someone asks to set up, install or start Job Radar.
---

# Set up Job Radar

The current folder becomes the workspace: everything about this person lives here, apart from the plugin's code.
Ask before using it if it isn't empty and doesn't already hold a Job Radar workspace (`config/settings.yaml`). A
`.claude` folder alone counts as empty: installing the plugin for one folder puts its setting there.

1. Run `jobradar init` here (the plugin puts `jobradar` on the Bash PATH). It copies blank config files, a sample
   master résumé, `./jr` and the permission rules (`.claude/settings.json`), creates the database, and installs
   Job Radar's Python packages the first time (Python 3.10 or newer must be installed; if it says so, stop and tell
   them where to get it). When `.claude/settings.json` already exists, init adds its rules and keeps what's there.
2. Follow `${CLAUDE_PLUGIN_ROOT}/prompts/setup.md` with them, from its interview through the steps only they can do.

## Where things are
- Job Radar's instructions and code are in `${CLAUDE_PLUGIN_ROOT}`. When the instructions name `prompts/<file>.md`,
  `config/examples/` or `dashboard/index.html`, they mean that path under `${CLAUDE_PLUGIN_ROOT}`.
- The workspace is the current folder: `./jr`, `config/`, `data/`, the master résumé and the applications folder
  are there. Wherever the instructions say "the jobradar folder", read "the workspace". Run every `./jr` command from
  it. If the current folder has no `config/settings.yaml`, Job Radar isn't set up here: say so, and point to the
  setup skill (or `JOBRADAR_HOME`).
