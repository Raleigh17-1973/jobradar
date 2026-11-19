# Application package rules

Every rule here exists because a draft broke it once. `config/my_rules.md` adds the candidate's own rules and wins
where the two differ.

## Source of truth
- Draw only from `config/truth.md` and the master résumé (settings.yaml `paths.master_resume`). The candidate
  writes both.
- Open questions may be listed in `truth_review.md` in the master folder (`paths.master_dir`). Until they're
  answered, follow what my_rules.md says about them.
- Run a claim check: compare every bullet to the truth file. Flag anything not supported for confirmation, and never
  write it in silently.
- Use the truthful framing for borderline items, as my_rules.md lists them.

## Format
- The master résumé's own fonts and colors (packets are cloned from it).
- Two-page résumé, one-page cover letter.
- One folder per job. Readable filenames ending in the company name, e.g. "<Name> Resume Acme.pdf" and
  "<Name> Cover Letter Acme.pdf", plus one combined PDF.
- Reverse chronology, in the master's role order.
- Labeled bullets that mirror the posting's sections, for ATS-heavy employers.

## Writing lints (checked by code, not by eye)
- No em dashes.
- No Oxford commas.
- No comma joining two independent clauses with "and", "but" or "so". This was broken in almost every first draft.
- No "honest" as an adjective.
- No "[role] who…" summary openers.
- No first person in résumés.

## ATS audit
1. Extract the posting's key terms.
2. Check them against the **PDF text layer**, not the Word file. A font ligature once turned "Platform" into "PlaVorm".
3. Add missing terms only where they're true.
4. Report what stays out deliberately. Example: 46 of 55 terms found, and the 9 misses were things the candidate
   hasn't done.

## Cover letter
- Open with a sharp insight tied to one specific line in the posting.
- Give evidence.
- Name gaps plainly.
- Disclose the side business, if there is one (preferences.yaml `side_business`).
- Close with location.
- One page.

## Side-business disclosure scales with conflict risk
Only when preferences.yaml names a `side_business`:
- **Employers in its field** (the job has a `conflict_of_interest` signal, or my_rules.md names the field): commit
  to stepping back from related decisions.
- **Everyone else:** one line saying it doesn't compete.
- **Never claim "doesn't compete"** when the employer sells products in the side business's field.

## Agency submissions
Send the Word version plus a short candidate summary. No cover letter.
Check `config/history.yaml` first: a recruiter submission where the candidate applied directly can disqualify them.

## Guardrails
- The candidate approves every submission and every message sent on their behalf.
- Never fill in Social Security numbers, bank details or other personal identifiers.
- Only the candidate edits the truth file.
