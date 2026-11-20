# Job Radar: guided setup

Use this when someone asks you to set up Job Radar with them (README, step 4). You'll interview them and draft
the files Job Radar works from, then walk them through the steps only they can do. Work in the jobradar folder and
run every `./jr` command there.

Ground rules:
- Facts about them come only from them: their résumé, their LinkedIn profile, or what they tell you. Never add a
  fact, number, tool, title or date they haven't given you. When a line is vague ("led a large program"), ask for
  the real scope and numbers instead of estimating.
- Write `config/truth.md` only after they've read your draft and said yes. Every edit to it asks them first
  (`.claude/settings.json`).
- Ask in batches of up to four questions, with the question tool if you have one. Put a sensible default first
  when there is one. Never guess an answer; "skip for now" is a fine answer.
- Never ask for, type or store a password. They put the Gmail app password in the Keychain themselves, in their
  own Terminal (step 8).
- Don't send email, sign up for anything or create accounts for them.
- After each part, run `./jr doctor` and tell them in a line what's left.

## 0. Start
1. If `config/settings.yaml` doesn't exist yet, run `./jr init`.
2. Run `./jr doctor`. Its FIX lines are the to-do list.
3. Ask what they have: their current résumé (a Word file is best, a PDF works), their LinkedIn profile (on
   LinkedIn: their profile, then More, then Save to PDF) and any list of past applications or recruiters.

## 1. Their facts: config/truth.md
Everything Job Radar writes for them must be backed by this file, so it matters most.
1. Read their résumé and profile. Draft the file in the layout of `config/examples/truth.md`: roles with dates,
   outcomes, people management, tools used, credentials, "Never claim without new confirmation" and
   "Overstatements caught before".
2. For each role, ask what the résumé leaves out: team size, budget, scope, results with numbers, and whether each
   number is exact or approximate. Write approximate numbers the way they say them ("about 40 programs").
3. Ask what they haven't done that their target roles often ask for (P&L, hiring, a tool, a degree). Put those
   under "Never claim without new confirmation".
4. Show the draft. Write it only after their yes, and change anything they correct.

## 2. Scoring summary: config/candidate.md
Draft it from truth.md only, in the layout of `config/examples/candidate.md`: headline, career arc, people
management, where they win, gaps, target level and pay. Show it, then write it.

## 3. Preferences: config/preferences.yaml
Ask about:
- Target titles, and titles to drop.
- Their pay floor, and the pay that earns full marks.
- Home area: city, state and the nearby places they'd commute to.
- Remote, hybrid and on-site limits, including the most office days a week they'd accept.
- Travel limit.
- Industries or companies to avoid, and staffing agencies to drop.
- A side business, if they have one, that applications should disclose.

Change the example's values and keep its comments.

## 4. Form answers: config/profile.yaml
Ask for contact details (name, email, phone, city, LinkedIn URL), work authorization and sponsorship, their salary
answer, start date or notice period, whether they'd relocate, and their choices for the voluntary
self-identification questions (declining every one is fine). Leave out Social Security number, date of birth and
government ID: Job Radar never enters them.

## 5. Settings: config/settings.yaml
- `gmail: address`: the inbox that gets their job alerts.
- `linkedin_searches` and `google_jobs`: replace the example searches with their titles. For a metro-area search,
  ask them to run a LinkedIn job search for their city and paste the URL; its `geoId=` number goes in the query.
- `resume: never_claim`: the words and phrases from truth.md's "Never claim" list. Each matches as a whole word, so
  list both forms when both matter ("board presentation", "board presentations"). A trailing * matches any ending.
- `paths`, only if they keep their résumé or packets somewhere else.

## 6. Master résumé: master/Master Resume.docx
Every packet is cloned from this file, so it has to keep the layout `radar/packet.py` reads (README, "The master
résumé layout").
1. Ask whether they want the sample's look or their own résumé's fonts and style.
   - **The sample's look:** copy `config/examples/Master Resume.docx` to `master/Master Resume.docx`, then replace
     its text with theirs using python-docx (`.venv/bin/python`). Keep each paragraph's formatting: for an extra
     role or bullet, copy a paragraph of the same kind; delete the ones left over.
   - **Their own file:** copy it to `master/Master Resume.docx` and bring it into the layout. The first four
     paragraphs are name, title line, keywords line and contact line. The section headings are bold, in color
     1F3864, and named Summary, Experience, Skills and Education. Each role is a bold line holding the title, a
     tab, then the dates; then the company line; then an optional one-line intro; then bullets in a Word list.
     Each Skills line is a bold label followed by the skills.
2. Use only facts from truth.md and their résumé.
3. At least one role needs a one-line intro, and at least one bullet must have no bold label: the packet builder
   copies those as templates.
4. Set `resume: role_keys` to one short key per role, top to bottom (like `acme_pm`), and `resume: location_text`
   to the part of the contact line a draft may swap out, such as "(Denver area)".
5. Run `./jr doctor` until the master résumé line says OK. Then open the file so they can look at it:
   `open "master/Master Resume.docx"`.

## 7. The optional files
Mention each and fill in the ones they want now:
- `config/my_rules.md`: their own rules for scoring and writing. Tell them Job Radar's built-in writing checks
  reject em dashes and the Oxford comma, among other things, so their drafts follow that style.
- `config/history.yaml`: past applications and recruiter submissions, so Job Radar warns before a duplicate.
- `config/contacts.yaml`: people they know at companies, for referral asks.
- `config/watchlist.yaml`: companies whose own job boards to read. `./jr watch-find "Company"` finds a board.
- `config/vc_boards.yaml`: venture firms' portfolio job boards.

## 8. The steps only they can do
Walk them through these one at a time, with the README step for each:
1. **Gmail app password (step 3).** In their Google account, with 2-Step Verification on: Security, then App
   passwords. Then, in their own Terminal, not this chat, they run the `security add-generic-password` command
   that `./jr doctor` prints. It asks for the password. Then run `./jr check`, which only reads, to test the login.
2. **Job alerts:** on LinkedIn, Indeed and Glassdoor, for their target titles, sent to that inbox.
3. **The dashboard (step 5).** With their OK, publish `dashboard/index.html` as a private artifact called Job
   Radar with the capabilities in README step 5 (the db, with only the owner writing `messages`), and put its link
   in settings.yaml as `dashboard_url`.
4. **Chrome (step 6, optional):** only if they want Google for Jobs or the apply task.
5. **The hourly run (step 7).** In the Claude desktop app, they create a scheduled task with this jobradar folder
   as its folder and `prompts/hourly_task.md` as its prompt, with the path filled in. Then they run it once by
   hand and approve anything it asks for.

## 9. Finish
Run `./jr doctor`. When it says Ready, tell them what Job Radar will do on its first run and that the dashboard
fills in after it.
