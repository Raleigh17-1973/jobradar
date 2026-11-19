# Your own rules

The prompts in `prompts/` are general. These rules are yours: they add to them, and they win wherever the two
differ. Add one each time a draft or a score gets something wrong, so it doesn't happen again. Only you change
this file. Delete the examples below that don't fit you.

## Everywhere
- Say "more than ten years" if experience length comes up.
- People management is "about three years" leading a team of three. Never more, never hiring.

## Scoring (prompts/score.md)
- **Requirements that sank roles before.** Downgrade hard only when one of these is *required*:
  - hands-on Python or ML engineering
  - board-level reporting

## Packets (prompts/packet.md and prompts/package_rules.md)
- Truthful framing for borderline items: "supported the vendor selection", not "led the vendor selection".

## Applying (prompts/apply.md and prompts/auto_apply.md)
- In chat, Claude stops before Submit so I can review.
- If you turn on the apply task (settings.yaml `auto_apply`), say here how it should answer what forms ask that a
  person usually decides case by case, for example:
  - **Salary:** "use the salary rules in profile.yaml without asking", or "hold the job and ask me".
  - **Someone I know there:** "apply right away; the referral ask stays a draft", or "hold the job".

## Follow-ups and referral asks (prompts/followup.md)
- Sign messages "Alex".
