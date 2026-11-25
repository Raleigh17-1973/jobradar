---
name: apply-queue
description: Job Radar's apply task: submit the applications the user clicked Apply on where no account or sign-in is needed. For Job Radar's apply scheduled task.
---

# Job Radar: the apply task

Follow `${CLAUDE_PLUGIN_ROOT}/prompts/auto_apply.md` exactly. It uses the Chrome DevTools tools this plugin
provides (named `mcp__plugin_jobradar_chrome-devtools__*`, or `mcp__chrome-devtools__*` where the user added the
server themselves) and Job Radar Chrome (`./jr chrome-up`).

## Where things are
- Job Radar's instructions and code are in `${CLAUDE_PLUGIN_ROOT}`. When the instructions name `prompts/<file>.md`,
  `config/examples/` or `dashboard/index.html`, they mean that path under `${CLAUDE_PLUGIN_ROOT}`.
- The workspace is the current folder: `./jr`, `config/`, `data/`, the master résumé and the applications folder
  are there. Wherever the instructions say "the jobradar folder", read "the workspace". Run every `./jr` command from
  it. If the current folder has no `config/settings.yaml`, Job Radar isn't set up here: say so, and point to the
  setup skill (or `JOBRADAR_HOME`).
