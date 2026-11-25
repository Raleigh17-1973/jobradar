---
name: apply
description: Fill a job application in Chrome for a job in Job Radar, stopping before Submit and for sign-ins, CAPTCHAs and codes. Use when asked to apply to a job, e.g. "apply to Acme".
---

# Job Radar: fill an application

Follow `${CLAUDE_PLUGIN_ROOT}/prompts/apply.md`, with `${CLAUDE_PLUGIN_ROOT}/prompts/apply_tips.md` for the
applicant system the page uses. Build the packet first (the resume skill) if the job doesn't have one.

## Where things are
- Job Radar's instructions and code are in `${CLAUDE_PLUGIN_ROOT}`. When the instructions name `prompts/<file>.md`,
  `config/examples/` or `dashboard/index.html`, they mean that path under `${CLAUDE_PLUGIN_ROOT}`.
- The workspace is the current folder: `./jr`, `config/`, `data/`, the master résumé and the applications folder
  are there. Wherever the instructions say "the jobradar folder", read "the workspace". Run every `./jr` command from
  it. If the current folder has no `config/settings.yaml`, Job Radar isn't set up here: say so, and point to the
  setup skill (or `JOBRADAR_HOME`).
