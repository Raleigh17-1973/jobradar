"""Job Radar command line.  Run from the jobradar folder:  ./jr <command>

  run              collect + filter + resolve: everything before Claude scores
  collect          read new alert emails into the jobs database (labels them if label_processed is on)
  filter           apply the free title, company and pay rules to new jobs
  resolve          find candidates on employer job boards (description, pay, apply link)
  remote-check     read city-tagged listings from the remote searches to see if they're really remote (after run)
  export          candidates as JSON for Claude to score
  save-scores      store Claude's scores and research briefs
  decisions-since / save-decisions / apply-decisions / dashboard-export   sync with the dashboard (see radar/dashboard.py)
  packet-context / packet-build   application packet for one job (see prompts/packet.md)
  queue            your Apply queue: which jobs need a packet, and which need your account step
  apply-start / apply-next / apply-hold / apply-clear / apply-held / apply-end
                   the apply task's bookkeeping (see radar/auto_apply.py and prompts/auto_apply.md)
  chrome-up        start Job Radar Chrome, the separate Chrome the DevTools tools use (see radar/chrome.py)
  check            log in and count alert emails by sender (read-only)
  fetch-samples    save recent alert emails as .eml files for parser tests (read-only)
  network-import / network-match   people you know at each company (see radar/network.py)
  watch / watch-find   your company watchlist, read from each company's own job board (see radar/watchlist.py)
  vc-boards / vc-find  venture firms' portfolio job boards, every startup a firm funded at once (see radar/vc_boards.py)
  board-review list|show|keep|done NAME   a full review of one company's new postings (see radar/board_review.py)
  google-due / google-urls / google-js / google-import / google-details   Google for Jobs via Chrome (prompts/google_jobs.md)
  outcomes / followups-due / followups-save / prep-due / prep-context / prep-save / referrals-due / referrals-save / alerts
                   after you apply (see radar/followup.py)
  init             first-time setup: copy config/examples into config/, then create the database and folders
  doctor           check every setup step and say what to fix (read-only)
  init-db          create the jobs database
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from . import (ats, config, dashboard, describe, filters, followup, google_jobs, history, linkedin_search, liveness,
               network, parsers, remote_check, store, vc_boards, watchlist)
from . import auto_apply, board_review, chrome
from .gmail_imap import CredentialError, GmailIMAP


def source_of(sender: str) -> str:
    domain = sender.rsplit("@", 1)[-1].lower()
    for name in ("linkedin", "indeed", "glassdoor"):
        if name in domain:
            return name
    return "other"


def _open(settings: dict) -> GmailIMAP:
    g = settings["gmail"]
    return GmailIMAP(g["address"], g["keychain_service"], g["imap_host"])


def _alert_query(settings: dict, days: int) -> str:
    return settings["gmail"]["alert_query"].format(days=days)


def _close_duplicates(conn) -> None:
    """Close repeats of a posting already in the database under another company name or title."""
    pairs = store.mark_duplicates(conn)
    conn.commit()
    if pairs:
        print(f"Closed {len(pairs)} repeat listing(s) of jobs already here: "
              + ", ".join(f"job {dup} (same posting as job {keep})" for dup, keep in pairs) + ".")


def cmd_check(args, settings) -> None:
    with _open(settings) as gm:
        gm.select_all_mail(readonly=True)
        uids = gm.search(_alert_query(settings, args.days))
        print(f"Logged in as {gm.address}. {len(uids)} alert emails in the last {args.days} days.\n")
        by_sender = defaultdict(list)
        for msg in gm.fetch_headers(uids):
            m = msg.parsed()
            by_sender[parseaddr(m["From"])[1].lower()].append(m["Subject"] or "")
        for sender, subjects in sorted(by_sender.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(subjects):4}  {sender}  [{source_of(sender)}]")
            for subj in subjects[-3:]:
                print(f"        e.g. {subj[:90]}")


def cmd_fetch_samples(args, settings) -> None:
    out_dir = config.project_path(settings["paths"]["samples"] + "/x").parent
    saved = Counter()
    with _open(settings) as gm:
        gm.select_all_mail(readonly=True)
        uids = gm.search(_alert_query(settings, args.days))
        # Newest first, capped per sender so one noisy sender doesn't crowd out the rest.
        for msg in gm.fetch(list(reversed(uids))):
            m = msg.parsed()
            sender = parseaddr(m["From"])[1].lower()
            if saved[sender] >= args.per_sender:
                continue
            day = parsedate_to_datetime(m["Date"]).strftime("%Y-%m-%d") if m["Date"] else "undated"
            path = out_dir / source_of(sender) / f"{day}__{sender.split('@')[0]}__{msg.gm_msgid}.eml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(msg.raw)
            saved[sender] += 1
    print(f"Saved {sum(saved.values())} emails to {out_dir}")
    for sender, n in saved.most_common():
        print(f"{n:4}  {sender}")


def cmd_collect(args, settings) -> None:
    g = settings["gmail"]
    days = args.days or g["lookback_days"]
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    emails = sightings = created = misread = 0
    label = g.get("label_processed", False) and not args.no_label
    mark_read = g.get("mark_read_processed", False) and not args.no_label
    with _open(settings) as gm:
        gm.select_all_mail(readonly=not (label or mark_read))
        uids = gm.search(_alert_query(settings, days))
        ids = gm.gm_msgids(uids)
        new_uids = [u for u in uids if not store.email_seen(conn, ids.get(u, ""))]
        for msg in gm.fetch(new_uids):
            m = msg.parsed()
            sender = parseaddr(m["From"])[1].lower()
            jobs = parsers.parse_email(m, sender)
            received = parsedate_to_datetime(m["Date"]).isoformat() if m["Date"] else None
            store.record_email(conn, msg.gm_msgid, source_of(sender), sender, m["Subject"] or "", received, len(jobs))
            for job in jobs:
                _, is_new = store.upsert_job(conn, job, msg.gm_msgid)
                created += is_new
                misread += parsers.looks_misread(job)
            conn.commit()
            if label:
                gm.add_label(msg.uid, g["processed_label"])
                if g["archive_processed"]:
                    gm.archive(msg.uid)
            emails += 1
            sightings += len(jobs)
        # Every alert in the window is in the database now, including ones from earlier runs.
        if mark_read and uids:
            gm.mark_read(uids)
    print(f"{emails} new alert emails ({len(uids) - len(new_uids)} already processed) -> "
          f"{sightings} listings -> {created} new unique jobs.")
    if mark_read and uids:
        print(f"Marked {len(uids)} processed alert emails as read in Gmail.")
    if misread:
        print(f"WARNING: {misread} listings look misread; an alert email layout may have changed.")
    if label and emails:
        action = "labeled and archived" if g["archive_processed"] else "labeled"
        print(f"Emails {action} '{g['processed_label']}' in Gmail.")
    if not args.quiet:
        _print_db_summary(conn)


def _print_db_summary(conn) -> None:
    rows = conn.execute("SELECT salary_max, salary_min, signals FROM jobs").fetchall()
    signals = [json.loads(r["signals"] or "{}") for r in rows]
    cross_site = conn.execute("SELECT COUNT(*) FROM (SELECT job_id FROM sightings GROUP BY job_id "
                              "HAVING COUNT(DISTINCT source) > 1)").fetchone()[0]
    print(f"\nDatabase: {len(rows)} unique jobs ({cross_site} found on more than one site)")
    print(f"  with posted pay:        {sum(1 for r in rows if r['salary_max'] or r['salary_min'])}")
    target = (config.load_optional("profile").get("compensation") or {}).get("desired_base_salary") \
        or config.load_optional("preferences").get("min_base_salary") or 0
    if target:
        print(f"  pay reaching ${target // 1000}K+:    {sum(1 for r in rows if (r['salary_max'] or r['salary_min'] or 0) >= target)}")
    print(f"  saved by you:           {sum(1 for s in signals if s.get('saved_by_you'))}")
    print(f"  LinkedIn Easy Apply:    {sum(1 for s in signals if s.get('easy_apply'))}")
    print(f"  Indeed sponsored:       {sum(1 for s in signals if s.get('sponsored'))}")


def cmd_filter(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    outcomes = filters.run(conn, config.load("preferences"), recheck=args.recheck)
    kept = outcomes.pop("candidate", 0)
    print(f"Checked {kept + sum(outcomes.values())} jobs: {kept} candidates, {sum(outcomes.values())} filtered out.")
    for reason, n in outcomes.most_common():
        print(f"  {n:4}  {reason}")


def cmd_resolve(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    counts = ats.run(conn)
    print(f"Looked up {counts.pop('checked')} candidates on employer job boards: {counts.pop('matched')} found "
          f"({counts.pop('board_but_no_title_match')} more companies have a board but not this title).")
    for system, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}  {system}")


def cmd_remote_check(args, settings) -> None:
    """Remote-search listings in some other city: read each posting, then re-filter them."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    prefs = config.load("preferences")
    c = remote_check.run(conn, limit=prefs.get("remote_checks_per_run", 20) if args.limit is None else args.limit,
                         spacing=args.spacing, max_age_days=prefs.get("max_posting_age_days", 14), patient=args.patient)
    filters.run(conn, prefs, recheck=True)
    read = c["from title"] + c["from employer board"] + c["from saved posting"] + c["from LinkedIn"]
    print(f"Remote check on city-tagged listings: {read} read ({c['from title']} by title, "
          f"{c['from employer board'] + c['from saved posting']} from employer boards, {c['from LinkedIn']} from LinkedIn) -> "
          f"{c['remote']} remote, {c['restricted']} remote but not open to you, {c['not remote']} not remote, "
          f"{c['closed']} closed; {c['waiting']} still waiting"
          + (" (LinkedIn rate-limited)" if c["rate_limited"] else "") + (f", {c['errors']} unreadable" if c["errors"] else "")
          + (f", {c['too old']} skipped as too old" if c["too old"] else "") + ".")


def cmd_search(args, settings) -> None:
    """Run the LinkedIn searches in settings.yaml; new jobs join the alert-email candidates."""
    if not settings.get("linkedin_searches"):
        print("No linkedin_searches in settings.yaml.")
        return
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    c = linkedin_search.run(conn, settings["linkedin_searches"])
    print(f"LinkedIn searches: {c['searches']} run -> {c['listings']} listings (+{c['outside_area']} outside your area, "
          f"saved as filtered) -> {c['new']} new unique jobs"
          + (", rate-limited" if c["rate_limited"] else "") + (f", {c['errors']} errors" if c["errors"] else "") + ".")


def cmd_run(args, settings) -> None:
    """The scheduled pipeline up to the point where Claude takes over."""
    cmd_collect(argparse.Namespace(days=args.days, no_label=args.no_label, quiet=True), settings)
    cmd_search(args, settings)
    watch = config.load("watchlist")
    if watch.get("enabled") and watchlist.due(store.connect(config.project_path(settings["paths"]["db"])),
                                              watch.get("every_hours", 3)):
        cmd_watch(args, settings)
    vc = config.load_optional("vc_boards")
    if vc.get("enabled") and vc_boards.due(store.connect(config.project_path(settings["paths"]["db"])),
                                           vc.get("every_hours", 6)):
        cmd_vc_boards(args, settings)
    cmd_filter(argparse.Namespace(recheck=False), settings)
    cmd_resolve(args, settings)
    # The lookup can add pay that was missing from the alert, so re-check the pay floor.
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    filters.run(conn, config.load("preferences"), recheck=True)
    cmd_verify(argparse.Namespace(limit=None), settings)
    c = describe.run(conn)
    network.match_jobs(conn)
    _close_duplicates(conn)
    print(f"Descriptions from employer pages: {c['described']} fetched, {c['closed']} closed; "
          f"{describe.missing(conn)} live jobs still without one.")
    waiting = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'candidate'").fetchone()[0]
    print(f"\n{waiting} candidates waiting for Claude to score (./jr export).")


def cmd_describe(args, settings) -> None:
    """Full descriptions for live jobs: employer pages first, then LinkedIn's public page."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    conn.execute("UPDATE jobs SET description_source = ats WHERE description_source IS NULL "
                 "AND ats IS NOT NULL AND length(COALESCE(description, '')) >= 500")
    c = describe.run(conn)
    print(f"Employer pages: {c['described']} descriptions fetched, {c['closed']} closed, "
          f"{c['unreachable'] + c['no text']} unreadable.")
    if args.linkedin_limit:
        lc = liveness.run_checks(conn, limit=args.linkedin_limit, spacing=args.spacing, patient=args.patient)
        print(f"LinkedIn: {lc['described']} descriptions, {lc['closed']} closed, {lc['open']} open"
              + (", rate-limited" if lc["rate_limited"] else "") + ".")
    print(f"{describe.missing(conn)} live jobs still have no description (./jr export --missing-descriptions).")


def cmd_save_postings(args, settings) -> None:
    """Postings Claude found: {id, apply_url?, description?, source?} per job."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    with open(args.file) as f:
        items = json.load(f)
    urls = texts = 0
    for it in items:
        if it.get("apply_url"):
            conn.execute("UPDATE jobs SET apply_url = COALESCE(apply_url, ?) WHERE id = ?", (it["apply_url"], it["id"]))
            urls += 1
        if len(it.get("description") or "") >= describe.MIN_DESCRIPTION:
            conn.execute("UPDATE jobs SET description = ?, description_source = ? WHERE id = ?",
                         (it["description"][:describe.MAX_DESCRIPTION], it.get("source", "indeed"), it["id"]))
            store.rescore_when_described(conn, it["id"])
            texts += 1
        if it.get("note"):
            # "not found on employer site" is a strong closed / ghost-job signal for scoring.
            row = conn.execute("SELECT signals FROM jobs WHERE id = ?", (it["id"],)).fetchone()
            signals = json.loads(row["signals"] or "{}")
            signals["posting_note"] = it["note"][:200]
            conn.execute("UPDATE jobs SET signals = ? WHERE id = ?", (json.dumps(signals, sort_keys=True), it["id"]))
    conn.commit()
    _close_duplicates(conn)
    print(f"Saved {urls} employer URLs and {texts} descriptions. Run ./jr describe to fetch the new URLs.")


def _job_row(conn, job_id: int):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        sys.exit(f"No job with id {job_id}")
    return row


def cmd_packet_context(args, settings) -> None:
    from . import packet
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    r = _job_row(conn, args.job_id)
    folder = packet.folder_for(dict(r))
    folder.mkdir(parents=True, exist_ok=True)
    signals = json.loads(r["signals"] or "{}")
    context = {
        "id": r["id"], "title": r["title"], "company": r["company"], "apply_url": r["apply_url"], "ats": r["ats"],
        "pay": {"min": r["salary_min"], "max": r["salary_max"], "source": r["salary_source"]},
        "locations": signals.get("locations"), "work_setting": signals.get("work_setting"),
        "history": signals.get("history"), "network": signals.get("network"),
        "description_source": r["description_source"],
        "description": r["description"] or "(no description: build from the research brief and flag it)",
        "research": {k: v for k, v in json.loads(r["brief"] or "{}").items()},
        "candidate": {
            "name": packet.NAME, "role_keys": packet.ROLE_KEYS, "location_text": packet.LOCATION_TEXT,
            # Everything a packet may draw on: your rules, your facts, your master résumé as text, past tailoring.
            "read_first": [str(f) for f in [config.CONFIG_DIR / "my_rules.md", packet.TRUTH,
                                            packet.MASTER_DIR / "truth_review.md",
                                            *sorted((packet.MASTER_DIR / "text").glob("*.txt"))] if f.exists()],
        },
    }
    (folder / "context.json").write_text(json.dumps(context, indent=1, ensure_ascii=False))
    past = context["history"] or {}
    if past.get("same_role") or past.get("company_summary"):
        print(f"History at {r['company']}: " + "; ".join(filter(None, [
            past.get("same_role") and f"this role: {past['same_role']}", past.get("company_summary")])) +
              ". If one is this posting under another title, compare req ids before writing.")
    print(f"Folder: {folder}\nNext: write resume.json, cover.json, ats_terms.json and answers.md there "
          f"(prompts/packet.md), then ./jr packet-build {r['id']}")


def cmd_packet_build(args, settings) -> None:
    from . import packet
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    r = _job_row(conn, args.job_id)
    folder = packet.folder_for(dict(r))
    # The employer's posting the packet writer found goes on answers.md's "Apply at:" line. Record it when the job
    # has no link, and stop if it's a posting you've already applied to (e.g. a repost under a new title).
    links = packet.apply_links(folder)
    employer = [u for u in links if store.posting_key(u) and not store.posting_key(u).startswith("linkedin:")]
    if not r["apply_url"] and employer:
        conn.execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (employer[0], r["id"]))
        conn.commit()
        print(f"Saved the employer link from answers.md: {employer[0]}")
    sightings = [s["url"] for s in conn.execute("SELECT url FROM sightings WHERE job_id = ?", (r["id"],))]
    repeat = store.applied_repeat(conn, r["id"], [r["apply_url"], *links, *sightings])
    extra = []
    if repeat:
        a, key = repeat
        extra.append(f"you already applied to this posting: job {a['id']} ({a['company']}: {a['title']}), "
                     f"applied {a['applied_at'][:10]} (same {key})")
    report = packet.build(folder, r["company"], word_only=args.word_only, extra_problems=extra)
    if repeat:
        store.close_as_repeat(conn, conn.execute("SELECT id, signals FROM jobs WHERE id = ?", (r["id"],)).fetchone(),
                              repeat[0]["id"])
        conn.commit()
        print(f"STOP: {extra[0]}. Closed job {r['id']} as a repeat of job {repeat[0]['id']}.")
        return
    ats_note = ""
    if report["ats"]:
        f, m = len(report["ats"]["found"]), len(report["ats"]["missing"])
        ats_note = (f"ATS terms in the {'Word file' if args.word_only else 'PDF'}: {f} of {f + m}; "
                    f"missing: {', '.join(report['ats']['missing']) or 'none'}")
    print(f"Pages: {report['pages'] or 'counted when the PDFs are made'}\n{ats_note}")
    if report["problems"]:
        print(f"{len(report['problems'])} problems to fix:")
        for p in report["problems"]:
            print(f"  - {p}")
        return
    signals = json.loads(r["signals"] or "{}")
    signals["packet"] = {"folder": str(folder.relative_to(packet.FOLDER_BASE)), "built_at": store.now(),
                         "files": report["files"], "pdfs": report["pdfs"]}
    conn.execute("UPDATE jobs SET signals = ?, status = CASE WHEN status IN ('scored', 'queued', 'maybe') "
                 "THEN 'packet_ready' ELSE status END WHERE id = ?", (json.dumps(signals, sort_keys=True), r["id"]))
    conn.commit()
    print(f"Clean. Packet ready in {folder.relative_to(packet.FOLDER_BASE)}:")
    for name in report["files"]:
        print(f"  {name}")
    if args.word_only:
        print(f"Word files only. Before applying, run ./jr packet-build {r['id']} to make the PDFs and check page counts.")


def cmd_queue(args, settings) -> None:
    """Your Apply queue: packet state and what signing in takes, freshest Apply click first."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    rows = conn.execute("SELECT * FROM jobs WHERE status IN ('queued', 'packet_ready') "
                        "ORDER BY decided_at DESC, id DESC").fetchall()
    if not rows:
        print("Your Apply queue is empty.")
        return
    account_text = {"none": "no account needed", "account": "you create an account or sign in",
                    "signed_in": "you need to be signed in"}
    counts = Counter()
    for r in rows:
        built = json.loads(r["signals"] or "{}").get("packet")
        if not built:
            packet_state = "needs packet"
        else:
            packet_state = "packet ready" if built.get("pdfs", True) else "packet ready, Word files only"
        si = ats.sign_in(r["apply_url"], r["ats"])
        account = f"{si['system']}: {account_text[si['need']]}" if si else "applicant system not known yet"
        counts[packet_state] += 1
        counts[si["need"] if si else "unknown"] += 1
        print(f"{r['id']:>5}  {r['company']}: {r['title']}\n"
              f"       {packet_state} · {account} · {r['apply_url'] or 'no apply link yet'}")
    print(f"\n{len(rows)} in the queue: {counts['needs packet']} need a packet; {counts['account']} need your account "
          f"step, {counts['signed_in']} need you signed in, {counts['unknown']} on sites not identified yet.")


def cmd_mark_applied(args, settings) -> None:
    """Record a submission: status, date and the salary answer given (so later conversations stay consistent)."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    r = _job_row(conn, args.job_id)
    signals = json.loads(r["signals"] or "{}")
    if args.salary:
        signals["salary_answer_given"] = args.salary
    signals.pop("auto_apply", None)   # a job you finished after the apply task held it
    conn.execute("UPDATE jobs SET status = 'applied', applied_at = ?, signals = ? WHERE id = ?",
                 (store.now(), json.dumps(signals, sort_keys=True), r["id"]))
    conn.commit()
    print(f"Marked applied: {r['company']}: {r['title']}" + (f" (salary answer {args.salary})" if args.salary else ""))


def cmd_chrome_up(args, settings) -> None:
    ok, message = chrome.up(settings)
    print(message)
    if not ok:
        sys.exit(1)


def cmd_apply_start(args, settings) -> None:
    print(auto_apply.start(_db(settings), settings)[1])


def cmd_apply_next(args, settings) -> None:
    job, message = auto_apply.next_job(_db(settings), settings)
    print(json.dumps(job, indent=1, ensure_ascii=False) if job else message)


def cmd_apply_hold(args, settings) -> None:
    conn = _db(settings)
    r = _job_row(conn, args.job_id)
    auto_apply.hold(conn, r["id"], args.reason, args.details)
    print(f"Held for you: {r['company']}: {r['title']} ({args.reason}). Later runs skip it until ./jr apply-clear {r['id']}.")


def cmd_apply_clear(args, settings) -> None:
    conn = _db(settings)
    r = _job_row(conn, args.job_id)
    done = auto_apply.clear(conn, r["id"])
    print(f"{'Cleared' if done else 'No hold on'} {r['company']}: {r['title']}."
          + (" The next apply run will take it." if done else ""))


def cmd_apply_held(args, settings) -> None:
    rows = auto_apply.held_jobs(_db(settings))
    if not rows:
        print("No jobs are waiting on you.")
    for r in rows:
        h = json.loads(r["signals"])["auto_apply"]
        print(f"{r['id']:>6}  {r['company']}: {r['title']}\n        {h['reason']} (since {h['at'][:16]})"
              + (f"\n        {h['details']}" if h.get("details") else ""))


def cmd_apply_end(args, settings) -> None:
    print(auto_apply.end(_db(settings), chrome_down=args.chrome_down))


def cmd_verify(args, settings) -> None:
    """Closed on LinkedIn? Older than the age limit? Both take jobs out of review."""
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    prefs = config.load("preferences")
    if prefs.get("linkedin_status_check", True):
        c = liveness.run_checks(conn, limit=args.limit or prefs.get("linkedin_checks_per_run", 40))
        note = " (LinkedIn rate-limited; the rest wait for the next run)" if c["rate_limited"] else ""
        print(f"LinkedIn status: {c['open']} open, {c['closed']} no longer accepting applications, {c['error']} unreadable{note}.")
    e = liveness.expire_old(conn, prefs.get("max_posting_age_days", 14))
    print(f"Age limit ({prefs.get('max_posting_age_days', 14)} days): {e['expired']} dropped from review, "
          f"{e['flagged']} of your queued/held jobs flagged as old.")


def cmd_export(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    statuses = args.status.split(",")
    where = f"status IN ({','.join('?' * len(statuses))})"
    params: list = list(statuses)
    if args.verdict:
        verdicts = args.verdict.split(",")
        where += f" AND verdict IN ({','.join('?' * len(verdicts))})"
        params += verdicts
    if args.ids:
        ids = [int(i) for i in args.ids.split(",")]
        where = f"id IN ({','.join('?' * len(ids))})"
        params = ids
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY COALESCE(score, 0) DESC, first_seen DESC LIMIT ?",
                        params + [args.limit]).fetchall()
    if args.unresearched:
        rows = [r for r in rows if not json.loads(r["brief"] or "{}").get("researched_at")]
    if args.missing_descriptions:
        rows = [r for r in rows if len(r["description"] or "") < describe.MIN_DESCRIPTION]
    if args.described_only:
        rows = [r for r in rows if len(r["description"] or "") >= describe.MIN_DESCRIPTION]
    out = []
    for r in rows:
        signals = json.loads(r["signals"] or "{}")
        item = {
            "id": r["id"], "title": r["title"], "company": r["company"],
            "locations": signals.pop("locations", [r["location"]]),
            "pay": ({"min": r["salary_min"], "max": r["salary_max"], "source": r["salary_source"],
                     "text": signals.pop("salary_text", None)} if (r["salary_min"] or r["salary_max"]) else None),
            "signals": signals, "ats": r["ats"], "apply_url": r["apply_url"], "posted_at": r["posted_at"],
            "description_source": r["description_source"] if len(r["description"] or "") >= describe.MIN_DESCRIPTION else None,
            "links": [dict(s) for s in conn.execute("SELECT source, url FROM sightings WHERE job_id = ?", (r["id"],))],
        }
        if r["score"] is not None:
            item["current"] = {"score": r["score"], "verdict": r["verdict"]}
        research = {k: v for k, v in json.loads(r["brief"] or "{}").items() if k not in ("why", "flags", "tier")}
        if research.get("researched_at"):
            item["research"] = research
        if args.descriptions and r["description"]:
            item["description"] = describe.excerpt(r["description"], args.desc_chars)
        out.append(item)
    text = json.dumps(out, indent=1, ensure_ascii=False)
    if args.out:
        config.project_path(args.out).write_text(text)
        print(f"Wrote {len(out)} jobs to {args.out}")
    else:
        print(text)


def cmd_save_scores(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    with open(args.file) as f:
        items = json.load(f)
    counts = Counter()
    for it in items:
        row = conn.execute("SELECT brief, apply_url FROM jobs WHERE id = ?", (it["id"],)).fetchone()
        if row is None:
            print(f"  skipping unknown job id {it['id']}")
            continue
        brief = json.loads(row["brief"] or "{}")
        brief.update({"why": it.get("why", ""), "flags": it.get("flags", []), "tier": it.get("tier")})
        if it.get("brief"):
            brief.update(it["brief"])
            brief["researched_at"] = store.now()
        apply_url = row["apply_url"] or ((it.get("brief") or {}).get("apply") or {}).get("url")
        conn.execute(
            "UPDATE jobs SET score = ?, verdict = ?, brief = ?, apply_url = ?, "
            "status = CASE WHEN status IN ('candidate', 'scored') THEN 'scored' ELSE status END WHERE id = ?",
            (it["score"], it["verdict"], json.dumps(brief, ensure_ascii=False), apply_url, it["id"]),
        )
        counts[it["verdict"]] += 1
    conn.commit()
    _close_duplicates(conn)
    print(f"Saved {sum(counts.values())} scores: " + ", ".join(f"{n} {v}" for v, n in counts.most_common()))


def cmd_dashboard_export(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    _close_duplicates(conn)   # last check before the page shows anything
    path = dashboard.export(conn, config.project_path("data/dashboard/x").parent,
                            dashboard.summary(conn, settings["gmail"]["lookback_days"]))
    data = json.loads(path.read_text())
    files = [{"as": dashboard.DATA_FILE, "path": str(path)}]
    lists = path.parent / dashboard.LISTS_FILE
    if data.get("lists_file"):
        counts = data["list_counts"]
        files.append({"as": dashboard.LISTS_FILE, "path": str(lists)})
    else:
        counts = {"skipped": len(data["skipped"]), "filtered": len(data["filtered"])}
        lists.unlink(missing_ok=True)
    print(f"Wrote {path} ({sum(Path(f['path']).stat().st_size for f in files) // 1024} KB in {len(files)} file(s): "
          f"{len(data['jobs'])} jobs, {counts['skipped']} skips, {counts['filtered']} filtered).")
    print("Publish it: publish_files with slug, base_version and files " + json.dumps(files, ensure_ascii=False))


def _db(settings):
    return store.connect(config.project_path(settings["paths"]["db"]))


def cmd_outcomes(args, settings) -> None:
    report = followup.outcomes(_db(settings))
    print(json.dumps(report, indent=1) if args.json else followup.outcomes_text(report))


def _write_due(items: list, out: str, what: str, nxt: str) -> None:
    path = config.project_path(out)
    path.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    print(f"{len(items)} {what}" + (f" written to {path}. Next: {nxt}" if items else "."))


def cmd_followups_due(args, settings) -> None:
    _write_due(followup.followups_due(_db(settings)), args.out, "follow-ups due",
               "draft each following prompts/followup.md, then ./jr followups-save FILE")


def _report_problems(problems: dict, saved_what: str, total: int) -> None:
    print(f"Saved {total - len(problems)} {saved_what}.")
    for who, issues in problems.items():
        print(f"  NOT SAVED {who}: " + "; ".join(issues))
    if problems:
        sys.exit("Fix those drafts and run the save again (only the fixed ones are needed).")


def cmd_followups_save(args, settings) -> None:
    items = json.loads(Path(args.file).read_text())
    _report_problems(followup.save_followups(_db(settings), items), "follow-up drafts", len(items))


def cmd_prep_due(args, settings) -> None:
    d = followup.prep_due(_db(settings))
    for j in d["due"]:
        print(f"{j['id']}  {j['company']}: {j['title']}  (latest: {j['latest_event'] or 'unknown'}"
              + (f"; {'; '.join(j['notes'])}" if j["notes"] else "") + ")")
    for a in d["no_job_on_file"]:
        print(f"NO JOB ON FILE  {a['company']}: {a['role'] or 'role not named'} ({a['status']} {a['at']})")
    if not d["due"] and not d["no_job_on_file"]:
        print("No interview prep due.")


def cmd_prep_context(args, settings) -> None:
    path = followup.prep_context(_db(settings), args.job_id)
    print(f"Wrote {path}. Next: write interview_prep.md in that folder following prompts/interview_prep.md, "
          f"then ./jr prep-save {args.job_id}")


def cmd_prep_save(args, settings) -> None:
    issues = followup.save_prep(_db(settings), args.job_id)
    if issues:
        sys.exit("Not saved. Fix these in interview_prep.md: " + "; ".join(issues))
    print(f"Interview prep attached to job {args.job_id}; it shows on the dashboard after the next export.")


def cmd_referrals_due(args, settings) -> None:
    _write_due(followup.referrals_due(_db(settings)), args.out, "referral asks due",
               "draft each following prompts/followup.md (Referral asks), then ./jr referrals-save FILE")


def cmd_referrals_save(args, settings) -> None:
    items = json.loads(Path(args.file).read_text())
    _report_problems(followup.save_referrals(_db(settings), items), "referral asks", len(items))


def cmd_alerts(args, settings) -> None:
    print(followup.alerts(_db(settings), mark=not args.dry_run) or "NO ALERT")


def cmd_decisions_since(args, settings) -> None:
    since = dashboard.decisions_since(config.project_path("data/dashboard_decisions/x").parent)
    if since:
        print(json.dumps([{"field": "at", "op": ">=", "value": since}]))
    else:
        print("FULL: no earlier read. Use db_list (or ArtifactData list) on `decisions` (limit 1000) and save with --full.")


def cmd_save_decisions(args, settings) -> None:
    counts = dashboard.save_decisions(config.project_path("data/dashboard_decisions/x").parent,
                                      Path(args.file).read_text(), full=args.full)
    print(f"Saved {counts['saved']} decisions, cleared {counts['cleared']}"
          + (f", kept {counts['kept_local']} newer decisions made in chat" if counts["kept_local"] else "") + ".")


def cmd_apply_decisions(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    counts = dashboard.apply_decisions(conn, config.project_path("data/dashboard_decisions/x").parent)
    print("Dashboard decisions applied: " + ", ".join(f"{n} {k}" for k, n in counts.items()))


def cmd_history_collect(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    with _open(settings) as gm:
        gm.select_all_mail(readonly=True)
        added = history.collect(conn, gm, args.days)
    waiting = len(history.export_unclassified(conn))
    print(f"{added} new application-related emails; {waiting} waiting for Claude to classify (./jr history-export).")


def cmd_history_export(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    items = history.export_unclassified(conn)
    config.project_path(args.out).write_text(json.dumps(items, indent=1, ensure_ascii=False))
    print(f"Wrote {len(items)} emails to {args.out}")


def cmd_history_save(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    if args.file:
        with open(args.file) as f:
            print(f"Saved {history.save_classifications(conn, json.load(f))} classifications.")
    counts = history.rebuild_applications(conn)
    print("Applications: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    matched = history.match_jobs(conn)
    print(f"Matched to radar jobs: {matched['same_role']} you've already applied to, "
          f"{matched['same_company']} at companies where you've applied to other roles.")


def cmd_network_import(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    print(f"Imported {network.import_file(conn, Path(args.file).expanduser())} connections.")
    cmd_network_match(args, settings)


def cmd_network_match(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    counts = network.match_jobs(conn)
    print(f"People you know at {counts['companies']} companies in the radar ({counts['jobs']} jobs).")


def _google_query(settings: dict, n: int) -> dict:
    queries = settings.get("google_jobs", {}).get("queries", [])
    if not 1 <= n <= len(queries):
        sys.exit(f"Search {n} isn't in settings.yaml google_jobs (1 to {len(queries)}).")
    return queries[n - 1]


def _google_dir(settings: dict) -> Path:
    from datetime import date
    return config.project_path("data/google") / date.today().isoformat()


def cmd_google_urls(args, settings) -> None:
    cfg = settings.get("google_jobs", {})
    for n, q in enumerate(cfg.get("queries", []), 1):
        print(f"{n}\t{q['name']}\t{google_jobs.search_url(cfg, q)}")


def cmd_google_due(args, settings) -> None:
    if not (settings.get("google_jobs") or {}).get("queries"):
        print("NOT DUE: no google_jobs searches in settings.yaml")
        return
    every = settings.get("google_jobs", {}).get("every_hours", 3)
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    is_due, hours = google_jobs.due(conn, every)
    since = "never swept" if hours is None else f"last sweep {hours:.1f} hours ago"
    print(("DUE" if is_due else "NOT DUE") + f": {since} (every {every} hours)")


def cmd_google_js(args, settings) -> None:
    _google_query(settings, args.n)
    if args.mode == "list":
        print(google_jobs.list_js(args.n))
    else:
        print(google_jobs.detail_js(args.n, args.cards, google_jobs.last_cards(args.n, _google_dir(settings))))


def cmd_google_import(args, settings) -> None:
    """Reads Downloads/jr_google_N.json, saved by `google-js list N`."""
    query = _google_query(settings, args.n)
    cards = google_jobs.take_download(args.n, False, _google_dir(settings))
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    c = google_jobs.import_cards(conn, query, cards, config.load("preferences"))
    print(f"Google Jobs, {query['name']}: {c['listings']} listings, {c['new']} new"
          + (f", {c['outside_area']} outside your area" if c["outside_area"] else "") + ".")
    print("NEED_DETAILS " + " ".join(str(i) for i in c["need_details"]))


def cmd_google_details(args, settings) -> None:
    """Reads Downloads/jr_google_N_details.json, saved by `google-js detail N ...`."""
    details = google_jobs.take_download(args.n, True, _google_dir(settings))
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    c = google_jobs.save_details(conn, details)
    filters.run(conn, config.load("preferences"), recheck=True)
    _close_duplicates(conn)
    print(f"Google Jobs details: {c['described']} descriptions, {c['linkedin']} LinkedIn postings, "
          f"{c['apply_links']} employer apply links" + (f", {c['unmatched']} not found" if c["unmatched"] else "") + ".")


def cmd_watch(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    prefs = config.load("preferences")
    c = watchlist.run(conn, config.load("watchlist"), prefs, prefs.get("max_posting_age_days", 14))
    print(f"Company watchlist: {c['boards']} boards, {c['postings']} postings -> {c['fit']} in your target roles "
          f"(+{c['too_old']} older than the age limit) -> {c['new']} new jobs"
          + (f"; unreachable: {', '.join(c['unreachable_names'])}" if c["unreachable_names"] else "") + ".")


def cmd_vc_boards(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    prefs = config.load("preferences")
    c = vc_boards.run(conn, config.load_optional("vc_boards"), prefs, prefs.get("max_posting_age_days", 14),
                      full=getattr(args, "full", False))
    print(f"VC portfolio boards: {c['boards']} boards, {c['unique']} US listings since {c['since'][:16]} "
          f"-> {c['fit']} in your target roles (+{c['outside']} outside your area, {c['acquired']} at acquired "
          f"companies, {c['other_company']} filed under the wrong company) -> {c['new']} new jobs. 'United States'-only "
          f"postings: {c['country_remote']} say remote, {c['country_not_remote']} don't, {c['country_unread']} unread"
          + (f"; unreachable: {', '.join(c['unreachable_names'])}" if c["unreachable_names"] else "") + ".")


def cmd_vc_find(args, settings) -> None:
    for domain in args.domains:
        platform, info = vc_boards.find_board(domain)
        if platform == "getro":
            print(f"{domain}: Getro board, add to config/vc_boards.yaml:\n"
                  f"  - {{name: {info['name']}, platform: getro, id: \"{info['id']}\", domain: {info['domain']}}}")
        elif platform == "consider":
            print(f"{domain}: a Consider board; Job Radar can't read these (they need a session token)")
        else:
            print(f"{domain}: {platform}" + (f" ({info['error']})" if info and info.get("error") else ""))


def cmd_watch_find(args, settings) -> None:
    found = watchlist.find_boards(args.names)
    for name, boards in found.items():
        if not boards:
            print(f"{name}: no Greenhouse, Lever, Ashby, SmartRecruiters or Gem board found")
        for system, slug, n, sample in boards:
            print(f"{name}: {system}/{slug} ({n} postings) e.g. {'; '.join(sample)}")


def cmd_board_review(args, settings) -> None:
    conn = store.connect(config.project_path(settings["paths"]["db"]))
    c = board_review.company(args.name)
    if args.action == "list":
        rows = board_review.list_new(conn, c, config.load("preferences").get("max_posting_age_days", 14))
        print(f"{c['name']}: {len(rows)} new postings since the last review (posted within the age limit).")
        for p in rows:
            print(f"{p['n']:>3}. {p['title']} | {p['team'] or '-'} | {p['location'] or '-'} | posted {(p['posted'] or '?')[:10]}")
    elif args.action == "show":
        for p in board_review.show(c, [int(x) for x in args.args]):
            print(f"===== {p['n']}. {p['title']} | {p['team']} | {p['location']} | {p['url']}\n{p['description']}\n")
    elif args.action == "keep":
        picks = json.loads(Path(args.args[0]).read_text()) if args.args else []
        print(f"Kept {board_review.keep(conn, c, picks)} {c['name']} postings for scoring.")
        _close_duplicates(conn)
    else:
        print(f"Marked {board_review.done(conn, c)} {c['name']} postings as reviewed.")


def cmd_init_db(args, settings) -> None:
    db_path = config.project_path(settings["paths"]["db"])
    store.connect(db_path).close()
    print(f"Database ready at {db_path}")


def cmd_init(args, settings) -> None:
    """Copies any config/examples file you don't have yet into config/ (never overwrites), plus the sample
    master résumé, then creates the database and the packets folder."""
    import shutil
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in sorted(config.EXAMPLES_DIR.glob("*")):
        dst = config.CONFIG_DIR / src.name
        if src.suffix in (".yaml", ".md") and not dst.exists():
            shutil.copy(src, dst)
            copied.append(f"config/{src.name}")
    settings = config.load("settings")
    master = config.setting_path("master_resume", "master/Master Resume.docx")
    if not master.exists():
        master.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.EXAMPLES_DIR / "Master Resume.docx", master)
        copied.append(str(master.relative_to(config.ROOT) if config.ROOT in master.parents else master))
    config.setting_path("applications", "applications").mkdir(parents=True, exist_ok=True)
    cmd_init_db(args, settings)
    print("Copied: " + (", ".join(copied) if copied else "nothing (you already have every file)"))
    print("Next: fill in config/ and your master résumé (or ask Claude Code to follow prompts/setup.md with you),\n"
          "then follow the README from step 3. ./jr doctor checks each step and says what's left.")


def cmd_doctor(args, settings) -> None:
    from . import doctor
    sys.exit(doctor.report(doctor.run_checks(settings)))


def main() -> None:
    parser = argparse.ArgumentParser(prog="jr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="collect, filter and look up: everything before Claude scores")
    p.add_argument("--days", type=int, help="how far back to look (default: lookback_days in settings)")
    p.add_argument("--no-label", action="store_true", help="leave Gmail untouched even if labeling is on")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("search", help="run the LinkedIn searches in settings.yaml")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("collect", help="read new alert emails into the jobs database")
    p.add_argument("--days", type=int, help="how far back to look (default: lookback_days in settings)")
    p.add_argument("--no-label", action="store_true", help="leave Gmail untouched even if labeling is on")
    p.add_argument("--quiet", action="store_true", help="skip the database summary")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("export", help="candidates as JSON for Claude to score")
    p.add_argument("--status", default="candidate", help="comma-separated statuses (default: candidate)")
    p.add_argument("--verdict", help="only these verdicts, e.g. apply,maybe")
    p.add_argument("--ids", help="comma-separated job ids (overrides --status)")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--unresearched", action="store_true", help="only jobs without a research brief yet")
    p.add_argument("--missing-descriptions", action="store_true", help="only jobs with no full description yet")
    p.add_argument("--described-only", action="store_true", help="only jobs with a full description (for scoring)")
    p.add_argument("--descriptions", action="store_true", help="include job descriptions")
    p.add_argument("--desc-chars", type=int, default=4000)
    p.add_argument("--out", help="write to this file (relative to jobradar/) instead of printing")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("save-scores", help="store Claude's scores and briefs from a JSON file")
    p.add_argument("file")
    p.set_defaults(func=cmd_save_scores)

    p = sub.add_parser("filter", help="apply the free title, company and pay rules")
    p.add_argument("--recheck", action="store_true", help="re-run on every job not yet scored (after editing preferences)")
    p.set_defaults(func=cmd_filter)

    p = sub.add_parser("resolve", help="find candidates on employer job boards (description, pay, apply link)")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("remote-check", help="read city-tagged remote-search listings to see if they're really remote")
    p.add_argument("--limit", type=int, help="max LinkedIn pages to read (default: remote_checks_per_run in preferences)")
    p.add_argument("--spacing", type=int, default=liveness.SPACING_SECONDS, help="seconds between LinkedIn pages")
    p.add_argument("--patient", action="store_true", help="wait out LinkedIn rate-limits (for long background runs)")
    p.set_defaults(func=cmd_remote_check)

    p = sub.add_parser("dashboard-export", help="write data/dashboard/data.json for the dashboard page")
    p.set_defaults(func=cmd_dashboard_export)

    p = sub.add_parser("decisions-since", help="the db_query filter for dashboard decisions changed since the last read")
    p.set_defaults(func=cmd_decisions_since)

    p = sub.add_parser("save-decisions", help="store decision rows saved from a db_query / db_list result")
    p.add_argument("file", help="text file holding the result's document lines")
    p.add_argument("--full", action="store_true", help="the rows are every decision: replace all saved ones")
    p.set_defaults(func=cmd_save_decisions)

    p = sub.add_parser("apply-decisions", help="turn dashboard Apply/Maybe/Skip clicks into job statuses")
    p.set_defaults(func=cmd_apply_decisions)

    p = sub.add_parser("history-collect", help="pull application, rejection and interview emails from Gmail")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_history_collect)

    p = sub.add_parser("history-export", help="unclassified history emails as JSON for Claude")
    p.add_argument("--out", default="data/history_in.json")
    p.set_defaults(func=cmd_history_export)

    p = sub.add_parser("history-save", help="store classifications, rebuild applications, match jobs")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_history_save)

    p = sub.add_parser("describe", help="fetch full job descriptions for live jobs")
    p.add_argument("--linkedin-limit", type=int, default=0, help="also read up to N LinkedIn public pages")
    p.add_argument("--spacing", type=int, default=10, help="seconds between LinkedIn pages")
    p.add_argument("--patient", action="store_true", help="wait out LinkedIn rate-limits (for long background runs)")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("save-postings", help="store employer URLs or descriptions Claude found")
    p.add_argument("file")
    p.set_defaults(func=cmd_save_postings)

    p = sub.add_parser("packet-context", help="create a job's application folder and context.json")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_packet_context)

    p = sub.add_parser("packet-build", help="render résumé, cover letter and PDFs, then run every check")
    p.add_argument("job_id", type=int)
    p.add_argument("--word-only", action="store_true", help="Word files and checks only, without opening Word (scheduled runs)")
    p.set_defaults(func=cmd_packet_build)

    p = sub.add_parser("queue", help="your Apply queue: packet state and what signing in takes")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("mark-applied", help="record that you submitted an application")
    p.add_argument("job_id", type=int)
    p.add_argument("--salary", help="the salary answer you gave")
    p.set_defaults(func=cmd_mark_applied)

    p = sub.add_parser("chrome-up", help="start Job Radar Chrome if it isn't running (the DevTools tools connect to it)")
    p.set_defaults(func=cmd_chrome_up)

    p = sub.add_parser("apply-start", help="apply task: take the run lock and list the queue as it sees it")
    p.set_defaults(func=cmd_apply_start)

    p = sub.add_parser("apply-next", help="apply task: the next job to apply to (JSON), or STOP and why")
    p.set_defaults(func=cmd_apply_next)

    p = sub.add_parser("apply-hold", help="apply task: leave a job for you; later runs skip it until apply-clear")
    p.add_argument("job_id", type=int)
    p.add_argument("--reason", required=True, help="a few words for the phone alert, e.g. 'CAPTCHA on Submit'")
    p.add_argument("--details", help="what you need to do, e.g. the questions and where the filled form is")
    p.set_defaults(func=cmd_apply_hold)

    p = sub.add_parser("apply-clear", help="let the apply task take a held job again")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_apply_clear)

    p = sub.add_parser("apply-held", help="queued jobs the apply task left for you, and why")
    p.set_defaults(func=cmd_apply_held)

    p = sub.add_parser("apply-end", help="apply task: release the run lock and sum up the run")
    p.add_argument("--chrome-down", action="store_true", help="the run couldn't reach Chrome")
    p.set_defaults(func=cmd_apply_end)

    p = sub.add_parser("verify", help="drop jobs that are closed on LinkedIn or older than the age limit")
    p.add_argument("--limit", type=int, help="max LinkedIn pages to check this run")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("check", help="log in and count alert emails by sender")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("fetch-samples", help="save recent alert emails for parser tests")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--per-sender", type=int, default=6)
    p.set_defaults(func=cmd_fetch_samples)

    p = sub.add_parser("network-import", help="load LinkedIn's Connections.csv (or export .zip) and match it")
    p.add_argument("file")
    p.set_defaults(func=cmd_network_import)

    p = sub.add_parser("network-match", help="attach the people you know to each job at their company")
    p.set_defaults(func=cmd_network_match)

    p = sub.add_parser("google-urls", help="the Google for Jobs searches in settings.yaml, as URLs")
    p.set_defaults(func=cmd_google_urls)

    p = sub.add_parser("google-due", help="whether the scheduled run should sweep Google for Jobs now")
    p.set_defaults(func=cmd_google_due)

    p = sub.add_parser("google-js", help="JavaScript that reads a Google for Jobs page in Chrome")
    p.add_argument("mode", choices=["list", "detail"])
    p.add_argument("n", type=int, help="the search's number in google-urls")
    p.add_argument("cards", nargs="*", type=int, help="card numbers for detail (google-import prints them)")
    p.set_defaults(func=cmd_google_js)

    p = sub.add_parser("google-import", help="store search N's cards from Downloads/jr_google_N.json")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_google_import)

    p = sub.add_parser("google-details", help="store Google's descriptions from Downloads/jr_google_N_details.json")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_google_details)

    p = sub.add_parser("watch", help="read the job boards of the companies in config/watchlist.yaml")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("watch-find", help="find companies' job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Gem)")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_watch_find)

    p = sub.add_parser("vc-boards", help="read the VC portfolio job boards in config/vc_boards.yaml")
    p.add_argument("--full", action="store_true", help="read back the whole posting-age limit, not just since the last run")
    p.set_defaults(func=cmd_vc_boards)

    p = sub.add_parser("vc-find", help="check whether a venture firm's job board can be read, e.g. jobs.accel.com")
    p.add_argument("domains", nargs="+")
    p.set_defaults(func=cmd_vc_find)

    p = sub.add_parser("board-review", help="review every new posting on one watchlist company's board")
    p.add_argument("action", choices=["list", "show", "keep", "done"])
    p.add_argument("name", help="a company in config/watchlist.yaml, e.g. Anthropic")
    p.add_argument("args", nargs="*", help="posting numbers for show; a JSON file of picks for keep")
    p.set_defaults(func=cmd_board_review)

    p = sub.add_parser("outcomes", help="how your Job Radar applications are doing, by score, source and more")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_outcomes)

    p = sub.add_parser("followups-due", help="applications 7+ days old with no reply: context for follow-up drafts")
    p.add_argument("--out", default="data/followups_in.json")
    p.set_defaults(func=cmd_followups_due)

    p = sub.add_parser("followups-save", help="check and store follow-up drafts")
    p.add_argument("file")
    p.set_defaults(func=cmd_followups_save)

    p = sub.add_parser("prep-due", help="interviews that need a prep brief")
    p.set_defaults(func=cmd_prep_due)

    p = sub.add_parser("prep-context", help="write prep_context.json into the job's application folder")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_prep_context)

    p = sub.add_parser("prep-save", help="check the folder's interview_prep.md and attach it to the job")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_prep_save)

    p = sub.add_parser("referrals-due", help="good open jobs where you know someone: context for referral asks")
    p.add_argument("--out", default="data/referrals_in.json")
    p.set_defaults(func=cmd_referrals_due)

    p = sub.add_parser("referrals-save", help="check and store referral asks")
    p.add_argument("file")
    p.set_defaults(func=cmd_referrals_save)

    p = sub.add_parser("alerts", help="one line for a phone notification, or NO ALERT")
    p.add_argument("--dry-run", action="store_true", help="show the line without marking anything as announced")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("init", help="first-time setup: copy the example config, create the database and folders")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("init-db", help="create the jobs database")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("doctor", help="check every setup step and say what to fix (read-only)")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    have_settings = (config.CONFIG_DIR / "settings.yaml").exists()
    if args.func is cmd_doctor and not have_settings:
        return cmd_doctor(args, None)
    if args.func is not cmd_init and not have_settings:
        sys.exit(f"No {config.CONFIG_DIR / 'settings.yaml'} yet. Run ./jr init first (see README).")
    try:
        args.func(args, {} if args.func is cmd_init else config.load("settings"))
    except CredentialError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
