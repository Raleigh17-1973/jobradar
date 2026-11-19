# Job Radar

A personal job-search agent that runs inside [Claude Code](https://claude.com/claude-code). Every hour it collects
new job postings, drops the ones that can't fit, and has Claude score the rest against your real background, from
each job's full description. Strong fits show up on a private dashboard where you click Apply, Maybe or Skip. For
the jobs you pick, it drafts a tailored résumé and cover letter that may only use facts you've written down. After
you apply, it tracks replies in your Gmail and drafts follow-ups, referral asks and interview prep for you to send.

It was built by one job seeker for their own search, then made configurable so others can use it. Expect to
spend an afternoon on setup, mostly writing down your own background and rules.

## What it does

- **Collects jobs** from LinkedIn, Indeed and Glassdoor alert emails in your Gmail, LinkedIn's public job search,
  the job boards of companies you list (Greenhouse, Lever, Ashby, SmartRecruiters, Gem), and Google for Jobs (read
  in your own Chrome).
- **Filters for free**, before Claude sees anything: title words, pay floor, location and remote checks, posting
  age, staffing firms and aggregators, duplicates and reposts of jobs you've already applied to.
- **Scores with Claude** from the full description: hard filters, required versus preferred gaps, a 0–100 fit
  score, and for the best ones a research brief (company news, ratings, pay, whether the posting is live).
- **Shows a dashboard**, a private claude.ai page, with your Apply / Maybe / Skip choices synced back each hour.
- **Builds application packets**: a résumé and cover letter cloned from your own Word master résumé (same fonts and
  layout), with three checks. A claim check rejects any number or claim that isn't in your truth file or master
  résumé. Writing lints catch the usual problems. An ATS audit reads the PDF's text layer, which is what screening
  software sees.
- **Fills applications** in Chrome when you ask, stopping for anything only you should do.
- **Follows through**: reads application, rejection and interview emails, drafts follow-up notes and referral asks
  (never sent for you), writes interview prep briefs, and can send a phone alert when something needs you.

## What it never does

- Send a message, email or connection request for you. Drafts wait on the dashboard with a Copy button.
- Enter a password, create an account, or get past a CAPTCHA or an emailed code. It stops and asks you.
- Enter a Social Security number, date of birth, bank details or government ID.
- Claim anything about you that isn't in `config/truth.md` or your master résumé.
- Change Gmail beyond marking processed job alerts as read, and labeling or archiving them if you turn that on.

## Requirements

- **macOS.** Your Gmail app password lives in the Keychain, and PDFs are exported through Microsoft Word.
- **Python 3.9 or newer** (the one macOS ships with works).
- **Claude Code**, in the Claude desktop app for scheduled tasks, artifacts and phone notifications.
- **A Gmail inbox** that receives your job alerts, with 2-Step Verification on so you can make an app password.
- **Microsoft Word for Mac**, for PDFs and page counts. Without it, packets stop at Word files.
- Optional: the **Claude in Chrome** extension (Google for Jobs and filling applications), and the **Indeed**
  connector in Claude (company ratings and salary data).

## Setup

### 1. Get the code
```bash
git clone https://github.com/<you>/jobradar.git
cd jobradar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Create your files
```bash
./jr init
```
This copies everything in `config/examples/` into `config/`, puts a sample master résumé in `master/`, and creates
the database. Nothing it creates is ever committed (see `.gitignore`).

### 3. Connect Gmail
1. In your Google account, create an app password (Security → 2-Step Verification → App passwords).
2. Put your address in `config/settings.yaml` under `gmail.address`.
3. Store the app password in the Keychain (it prompts for it):
   ```bash
   security add-generic-password -a "you@gmail.com" -s "job-radar-gmail" -w
   ```
4. Check it works. This only reads: it counts alert emails by sender.
   ```bash
   ./jr check
   ```
Set up job alerts on LinkedIn, Indeed and Glassdoor for your target titles if you haven't already.

### 4. Describe yourself
These files are the heart of it. Claude scores and writes only from them.

| File | What goes in it |
|---|---|
| `config/truth.md` | Every fact a résumé, letter or answer may use: roles, outcomes, numbers, tools, credentials, and what never to claim. |
| `master/Master Resume.docx` | Your master résumé, in the layout below. Packets are cloned from it. |
| `config/candidate.md` | A one-page scoring view: your arc, where you win, what you lack, target level and pay. |
| `config/preferences.yaml` | Home area, titles, pay floor, commute and travel rules, companies and agencies to drop. |
| `config/profile.yaml` | Standard form answers: contact details, work authorization, salary answer, EEO choices. |
| `config/my_rules.md` | Your own rules on top of the prompts' general ones. Add one every time Claude gets something wrong. |
| `config/settings.yaml` | Searches, file paths and your résumé's role keys. |
| `config/watchlist.yaml` | Companies whose own job boards to read (`./jr watch-find "Company"` finds them). |
| `config/history.yaml`, `config/contacts.yaml` | Past applications and agency submissions, and people you know at companies. |

**The master résumé layout.** `radar/packet.py` reads the Word file by its structure, so start from the sample
`config/examples/Master Resume.docx` and keep its shape:
- The first four paragraphs are your name, a title line, a keywords line and a contact line.
- Section headings are bold, in the color 1F3864, and named Summary, Experience, Skills and Education.
- In Experience, each role starts with a bold line holding the title, a tab, then the dates. The next line is the
  company, then an optional one-line intro, then bulleted lines (Word's numbered or bulleted list). A bullet can
  start with a bold label ("Delivery: ").
- Each Skills line is a bold label followed by the skills.
- List one key per role in `settings.yaml` → `resume.role_keys`, top to bottom. Put the bit of the contact line a
  draft may swap out (for "(remote)", say) in `resume.location_text`.

Your fonts, colors and links carry over to every packet.

### 5. Publish your dashboard
Open Claude Code in the jobradar folder and ask:

> Publish `dashboard/index.html` as a private artifact called Job Radar, with the db capability, and put its link
> in `config/settings.yaml` as `dashboard_url`.

### 6. Chrome (optional)
For Google for Jobs and form filling, install the Claude in Chrome extension. The Google sweep saves its results as
downloads in `~/Downloads/jr_google_*.json`, so allow automatic downloads for google.com when Chrome asks.

### 7. The hourly run
In the Claude desktop app, create a scheduled task that runs hourly during the day (for example
`30 6-21 * * *`). Use `prompts/hourly_task.md` as its prompt, with your folder's path filled in. Run it once by
hand and read its summary.

### 8. Day to day
- **Dashboard:** click Apply, Maybe or Skip. The next run picks up your clicks and builds packets for your
  Apply picks.
- **Applying:** in Claude Code, ask "apply to Acme" (`prompts/apply.md`). Claude fills the form and stops before
  Submit, and for sign-ins and CAPTCHAs.
- **After you submit:** `./jr mark-applied <job_id> --salary "<number>"` (Claude runs it when it applied for you).
- **Your network (optional):** export your LinkedIn connections (Settings → Data privacy → Get a copy of your
  data → Connections) and run `./jr network-import Connections.csv`.

`./jr --help` lists every command.

## Where your data lives

All of it stays on your computer, in places `.gitignore` keeps out of the repo:

| Path | What |
|---|---|
| `config/*.yaml`, `config/*.md` | Your settings, background, rules and form answers. |
| `data/` | The jobs database, dashboard decisions, fetched email samples and working files. |
| `master/` | Your master résumé (settings.yaml `paths` can point elsewhere). |
| `applications/` | One folder per packet. |

The dashboard is a private claude.ai artifact that only you can open unless you share it. Job data passes through
Claude as it scores and writes.

## Prompts

`prompts/` holds the instructions Claude follows. They're written for anyone and read your config. Your own rules
go in `config/my_rules.md`, which wins where it differs.

| Prompt | Used for |
|---|---|
| `scheduled_run.md` | The hourly run, step by step. |
| `score.md` | Scoring and research briefs. |
| `packet.md`, `package_rules.md` | Résumé, cover letter and answers. |
| `apply.md` | Filling an application in Chrome. |
| `followup.md`, `interview_prep.md` | Follow-ups, referral asks and interview briefs. |
| `google_jobs.md` | The Google for Jobs sweep. |

## Tests
```bash
.venv/bin/python -m unittest discover -s tests
```
The tests use `config/examples/` and the made-up person in the sample résumé, never your own config. The alert
emails in `tests/samples/` are real messages with personal details and tracking tokens removed. If you add one
(`./jr fetch-samples` saves recent alerts in `data/samples/`), scrub it the same way first.

## A note on site terms

Job Radar reads LinkedIn's public (logged-out) job pages, slowly and in small numbers, and reads Google for Jobs in
your own browser. LinkedIn's and Google's terms restrict automated access. Decide for yourself whether to use those
parts. To turn LinkedIn reading off, remove `linkedin_searches` from `config/settings.yaml`, set
`linkedin_status_check: false` and `remote_checks_per_run: 0` in `config/preferences.yaml`, and change
`--linkedin-limit 25` to `0` in step 3a of `prompts/scheduled_run.md`. The Google sweep runs only when Chrome is
connected, and never once you remove `google_jobs` from `config/settings.yaml`.

## License

MIT. See [LICENSE](LICENSE).
