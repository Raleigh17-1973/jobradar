---
name: resume
description: Build a tailored résumé, cover letter and application answers (a Job Radar packet) for a job in Job Radar or a posting the user pastes. Use when asked for a résumé or cover letter for a job.
---

# Job Radar: build an application packet

Follow `${CLAUDE_PLUGIN_ROOT}/prompts/packet.md` and `${CLAUDE_PLUGIN_ROOT}/prompts/package_rules.md`. For a
pasted posting that isn't in Job Radar yet, add it as a job first (`./jr --help` lists the commands), then build
its packet. Every claim comes from config/truth.md and the master résumé; when a draft needs a fact that isn't
there, ask the user instead of writing it.

## Where things are
- Job Radar's instructions and code are in `${CLAUDE_PLUGIN_ROOT}`. When the instructions name `prompts/<file>.md`,
  `config/examples/` or `dashboard/index.html`, they mean that path under `${CLAUDE_PLUGIN_ROOT}`.
- The workspace is the current folder: `./jr`, `config/`, `data/`, the master résumé and the applications folder
  are there. Wherever the instructions say "the jobradar folder", read "the workspace". Run every `./jr` command from
  it. If the current folder has no `config/settings.yaml`, Job Radar isn't set up here: say so, and point to the
  setup skill (or `JOBRADAR_HOME`).
