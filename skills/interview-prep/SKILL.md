---
name: interview-prep
description: Write an interview prep brief for a job in Job Radar: the call, their problem, your stories, questions to ask and answers to handle carefully. Use when the user has an interview coming up.
---

# Job Radar: interview prep

Follow `${CLAUDE_PLUGIN_ROOT}/prompts/interview_prep.md`.

## Where things are
- Job Radar's instructions and code are in `${CLAUDE_PLUGIN_ROOT}`. When the instructions name `prompts/<file>.md`,
  `config/examples/` or `dashboard/index.html`, they mean that path under `${CLAUDE_PLUGIN_ROOT}`.
- The workspace is the current folder: `./jr`, `config/`, `data/`, the master résumé and the applications folder
  are there. Wherever the instructions say "the jobradar folder", read "the workspace". Run every `./jr` command from
  it. If the current folder has no `config/settings.yaml`, Job Radar isn't set up here: say so, and point to the
  setup skill (or `JOBRADAR_HOME`).
