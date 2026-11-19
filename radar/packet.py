"""Builds an application packet: tailored résumé, cover letter, answers, PDFs and checks.

Claude writes the content as JSON (see prompts/packet.md). This module renders it by cloning paragraphs from
your master résumé's own Word file, so fonts, colors, links and spacing match exactly. Role titles, companies
and dates are copied from the master and can't be changed by a draft.

Checks: writing lints, claim check (every number and risky term must exist in your truth file or master résumé),
page count, and an ATS audit against the PDF's text layer (what screening software actually reads).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from docx import Document
from pypdf import PdfReader, PdfWriter

from . import config

logging.getLogger("pypdf").setLevel(logging.ERROR)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Your files, from settings.yaml `paths` (see config/examples/settings.yaml).
MASTER = config.setting_path("master_resume", "master/Master Resume.docx")
MASTER_DIR = config.setting_path("master_dir", "master")
TRUTH = config.CONFIG_DIR / "truth.md"
APPLICATIONS = config.setting_path("applications", "applications")
# Packet paths saved on a job are relative to this, e.g. "applications/Acme - Program Manager".
FOLDER_BASE = APPLICATIONS.parent
NAME = config.candidate_name()

_resume = config.load_optional("settings").get("resume") or {}
# Role keys, in master order. Title, company line and dates for each come only from the master.
ROLE_KEYS: List[str] = list(_resume.get("role_keys") or [])
# Text on the master's contact line that a draft's location_note replaces.
LOCATION_TEXT = _resume.get("location_text") or ""


# ---------- reading the master ----------

def _color(run_el) -> Optional[str]:
    c = run_el.find(f"{W}rPr/{W}color")
    return c.get(f"{W}val") if c is not None else None


def _is_numbered(p_el) -> bool:
    return p_el.find(f"{W}pPr/{W}numPr") is not None


def _bold_first(p_el) -> bool:
    r = p_el.find(f"{W}r")
    return r is not None and r.find(f"{W}rPr/{W}b") is not None


def _text(p_el) -> str:
    return "".join(t.text or "" for t in p_el.iter(f"{W}t"))


@dataclass
class Role:
    key: str
    header: object                 # role title + tab + dates (fixed)
    company: object                # company line, may hold a hyperlink (fixed)
    intro: Optional[object] = None
    bullets: List[object] = field(default_factory=list)


class Master:
    """Your master résumé, split into reusable paragraph templates."""

    def __init__(self, path: Optional[Path] = None):
        self.doc = Document(str(path or MASTER))
        paras = [p._p for p in self.doc.paragraphs]
        self.name, self.title, self.keywords, self.contact = paras[:4]
        self.headings: Dict[str, object] = {}
        self.roles: List[Role] = []
        self.skills: List[object] = []
        self.education: List[object] = []
        section = None
        for p in paras[4:]:
            text = _text(p).strip()
            r = p.find(f"{W}r")
            if r is not None and _color(r) == "1F3864" and _bold_first(p) and not _is_numbered(p):
                section = text.split()[0].lower()   # summary | experience | skills | education
                self.headings[section] = p
                continue
            if section == "summary":
                self.summary = p
            elif section == "experience":
                if "\t" in text and _bold_first(p) and not _is_numbered(p):
                    n = len(self.roles)
                    self.roles.append(Role(ROLE_KEYS[n] if n < len(ROLE_KEYS) else f"role{n + 1}", p, None))
                elif self.roles and self.roles[-1].company is None:
                    self.roles[-1].company = p
                elif _is_numbered(p):
                    self.roles[-1].bullets.append(p)
                else:
                    self.roles[-1].intro = p
            elif section == "skills":
                self.skills.append(p)
            elif section == "education":
                self.education.append(p)
        self.labeled_bullet = self.roles[0].bullets[0]
        self.plain_bullet = next(b for r in self.roles for b in r.bullets if len(b.findall(f"{W}r")) == 1)
        self.intro_template = next(r.intro for r in self.roles if r.intro is not None)

    def text(self) -> str:
        return "\n".join(p.text for p in self.doc.paragraphs)


# ---------- rendering ----------

def _set_runs(p_el, texts: List[str]) -> None:
    """Put texts into the paragraph's direct runs (keeping each run's formatting); drop extra runs."""
    runs = p_el.findall(f"{W}r")
    for run, text in zip(runs, texts):
        ts = run.findall(f"{W}t")
        for extra in ts[1:]:
            run.remove(extra)
        for tab in run.findall(f"{W}tab"):
            run.remove(tab)
        if not ts:
            t = copy.deepcopy(p_el.find(f".//{W}t"))
            run.append(t)
            ts = [t]
        ts[0].text = text
        ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for run in runs[len(texts):]:
        p_el.remove(run)


def _clone(el, *texts: str):
    el = copy.deepcopy(el)
    if texts:
        _set_runs(el, list(texts))
    return el


def render_resume(spec: dict, out: Path) -> None:
    m = Master()
    body = m.doc.element.body
    sect = body.find(f"{W}sectPr")
    for p in [p._p for p in m.doc.paragraphs]:
        body.remove(p)
    add = lambda el: body.insert(list(body).index(sect), el)

    add(copy.deepcopy(m.name))
    add(_clone(m.title, spec["title_line"]))
    add(_clone(m.keywords, spec["keywords_line"]))
    contact = copy.deepcopy(m.contact)
    if spec.get("location_note") and LOCATION_TEXT:
        for t in contact.iter(f"{W}t"):
            if t.text and LOCATION_TEXT in t.text:
                t.text = t.text.replace(LOCATION_TEXT, spec["location_note"])
    add(contact)

    add(copy.deepcopy(m.headings["summary"]))
    add(_clone(m.summary, spec["summary"]))
    add(copy.deepcopy(m.headings["experience"]))
    roles = {r.key: r for r in m.roles}
    for role_spec in spec["roles"]:
        role = roles[role_spec["key"]]
        add(copy.deepcopy(role.header))
        add(copy.deepcopy(role.company))
        if role_spec.get("intro"):
            add(_clone(role.intro if role.intro is not None else m.intro_template, role_spec["intro"]))
        for b in role_spec.get("bullets", []):
            if b.get("label"):
                add(_clone(m.labeled_bullet, b["label"].rstrip(": ") + ": ", b["text"]))
            else:
                add(_clone(m.plain_bullet, b["text"]))
    if spec.get("earlier"):
        add(_clone(m.headings["experience"], "Earlier Experience"))
        for line in spec["earlier"]:
            add(_clone(m.summary, line))

    add(copy.deepcopy(m.headings["skills"]))
    for s in spec["skills"]:
        add(_clone(m.skills[0], s["label"].rstrip(": ") + ":  ", s["items"]))
    add(copy.deepcopy(m.headings["education"]))
    for i, p in enumerate(m.education):
        if i == len(m.education) - 1 and not spec.get("include_courses", True):
            continue
        add(copy.deepcopy(p))
    m.doc.save(str(out))


def render_cover_letter(spec: dict, out: Path) -> None:
    m = Master()
    body = m.doc.element.body
    sect = body.find(f"{W}sectPr")
    for p in [p._p for p in m.doc.paragraphs]:
        body.remove(p)
    add = lambda el: body.insert(list(body).index(sect), el)
    add(copy.deepcopy(m.name))
    add(_clone(m.title, spec.get("title_line") or _text(m.title)))
    add(copy.deepcopy(m.contact))
    spacer = lambda: _clone(m.summary, "")
    add(spacer())
    for line in [spec["date"], spec.get("recipient") or f"{spec['company']} Hiring Team"]:
        add(_clone(m.summary, line))
    add(spacer())
    add(_clone(m.summary, spec.get("greeting", "Dear Hiring Team,")))
    for para in spec["paragraphs"]:
        el = _clone(m.summary, para)
        spacing = el.find(f"{W}pPr/{W}spacing")
        if spacing is not None:
            spacing.set(f"{W}after", "160")
        add(el)
    add(_clone(m.summary, spec.get("closing", "Sincerely,")))
    add(_clone(m.summary, NAME))
    m.doc.save(str(out))


# ---------- PDF ----------

def _hfs(path: Path) -> str:
    return "Macintosh HD" + str(path.resolve()).replace("/", ":")


# Word is sandboxed: opening files elsewhere pops a "Grant File Access" dialog per file. Its own container
# folder needs no permission, so documents are staged there, exported, and the PDF copied back.
WORD_STAGING = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/JobRadarExport"


def to_pdf(docx: Path) -> Path:
    """Export through Microsoft Word, touching only the document it opens."""
    import shutil
    WORD_STAGING.mkdir(parents=True, exist_ok=True)
    staged = WORD_STAGING / docx.name
    staged_pdf = staged.with_suffix(".pdf")
    shutil.copy2(docx, staged)
    staged_pdf.unlink(missing_ok=True)
    script = f'''
tell application "Microsoft Word"
  set theDoc to open file name "{_hfs(staged)}"
  if theDoc is missing value then set theDoc to document "{staged.name}"
  save as theDoc file name "{_hfs(staged_pdf)}" file format format PDF
  close theDoc saving no
end tell'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=120)
    pdf = docx.with_suffix(".pdf")
    shutil.move(str(staged_pdf), pdf)
    staged.unlink(missing_ok=True)
    return pdf


def pdf_text(pdf: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


def pdf_pages(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def combine(pdfs: List[Path], out: Path) -> None:
    writer = PdfWriter()
    for pdf in pdfs:
        for page in PdfReader(str(pdf)).pages:
            writer.add_page(page)
    with open(out, "wb") as f:
        writer.write(f)


# ---------- checks ----------

_INDEPENDENT_AFTER = re.compile(
    r",\s+(and|but|so)\s+(?:he|she|they|we|it|this|that|these|those|leadership|the\s+\w+|[A-Z][a-z]+)\s+"
    r"(?:\w+ed|\w+s|was|were|is|are|has|had|will|can|could|did|led|built|ran|made|took)\b")


def lint(text: str, kind: str) -> List[str]:
    """Your writing rules. kind: 'resume' or 'letter'."""
    issues = []
    for line in [l for l in text.splitlines() if l.strip()]:
        snippet = line.strip()[:90]
        if "—" in line:
            issues.append(f"em dash: {snippet}")
        if re.search(r",\s+[^,.;:]{1,40},\s+(and|or)\s+\w", line):
            issues.append(f"Oxford comma: {snippet}")
        if _INDEPENDENT_AFTER.search(line):
            issues.append(f"comma joining two clauses: {snippet}")
        if re.search(r"\bhonest(ly)?\b", line, re.I):
            issues.append(f"'honest': {snippet}")
        if kind == "resume" and re.search(r"\b(I|I'm|I've|I'd|me|my|mine)\b", line):
            issues.append(f"first person in résumé: {snippet}")
    first = text.strip().split(".")[0]
    if re.match(r"^(?:[A-Z][\w-]*\s+){1,5}(leader|manager|professional|executive|director|consultant|expert)\s+who\b", first, re.I):
        issues.append(f"'[role] who' opener: {first[:90]}")
    return issues


_NEVER = re.compile(r"\b(python|azure|azure devops|ms project|microsoft project|smartsheet|clarity|servicenow|power bi|"
                    r"six sigma|lean|mba|board presentations?|p&l|profit and loss|hiring|hired|recruited|"
                    r"strategy consult\w*|investment (industry|management)|used by millions|"
                    r"improved predictability|ecommerce|e-commerce|twenty years|20 years)\b", re.I)
# A number of its own (not the digits inside "B2B" or "ISO42001"), with its unit: %, K/M/B or the words, or x.
_NUMBER = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|[KMB]\b|x\b|thousand\b|million\b|billion\b))?\+?",
                     re.I)
_SCALE_WORDS = {"thousand": "k", "million": "m", "billion": "b"}
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# Dates and clock times aren't claims: "2026-11-15", "November 6", "Nov 16, 2026", "3:30". A time needs its colon,
# so "led 3 PM teams" is still checked.
_NOT_A_CLAIM = re.compile(r"\b\d{4}-\d{2}-\d{2}\b"
                          rf"|\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}}\b)?"
                          r"|\b\d{1,2}:\d{2}\b", re.I)


def _number_key(raw: str) -> Optional[tuple]:
    """("10", "m") for "$10M", "$10 million" or "10M+"; ("85", "%") for "85%". None if it isn't a number."""
    s = raw.lower().replace(",", "").replace("$", "").replace("+", "").strip()
    unit = ""
    for word, letter in _SCALE_WORDS.items():
        if s.endswith(word):
            s, unit = s[:-len(word)].strip(), letter
    if s[-1:] in ("%", "k", "m", "b", "x"):
        s, unit = s[:-1].strip(), s[-1]
    try:
        value = float(s)
    except ValueError:
        return None
    return (str(int(value)) if value.is_integer() else str(value), unit)


def _numbers(text: str) -> List[tuple]:
    """(as written, key) for every number in the text, skipping dates and clock times."""
    text = _NOT_A_CLAIM.sub(" ", text)
    found = [(m.group(0).strip().rstrip(","), _number_key(m.group(0))) for m in _NUMBER.finditer(text)]
    return [(raw, key) for raw, key in found if key]


def job_context(folder: Path) -> str:
    """The job's own text in its folder (posting and research), for claim_check's context."""
    parts = []
    try:
        ctx = json.loads((folder / "context.json").read_text())
        parts += [str(ctx.get("description") or ""), json.dumps(ctx.get("research") or "", ensure_ascii=False)]
    except (OSError, ValueError):
        pass
    try:
        parts.append((folder / "posting.txt").read_text())
    except OSError:
        pass
    return "\n".join(parts)


def claim_check(text: str, context: str = "") -> List[str]:
    """Numbers and risky terms must be backed by your truth file or master résumé.

    A number is backed only when the same value with the same unit appears there as a number of its own, so
    "37%" isn't backed by "375 contributors" and "$10B" isn't backed by "$10M". context is the job's own text
    (its posting and research, see job_context): a number quoted from it, like "the role's 10% travel", is about
    the job. Dates and clock times are skipped. Never-claim terms are flagged wherever they appear.
    """
    backed = {key for _, key in _numbers(TRUTH.read_text() + "\n" + Master().text() + "\n" + context)}
    issues = [f"never-claim term '{m.group(0)}'" for m in _NEVER.finditer(text)]
    where = "truth file, master résumé or the posting" if context else "truth file or master résumé"
    issues += [f"number not in {where}: {raw}" for raw, key in _numbers(text) if key not in backed]
    return sorted(set(issues))


def ats_audit(text: str, terms: List[str]) -> Dict[str, List[str]]:
    text = re.sub(r"\s+", " ", text).lower()
    found, missing = [], []
    for term in terms:
        # drop one plural "s", but not the "ss" in "readiness" or "process"
        word = term.lower()
        stem = re.escape(word[:-1] if word.endswith("s") and not word.endswith("ss") else word)
        (found if re.search(rf"\b{stem}s?\b", text) else missing).append(term)
    return {"found": found, "missing": missing}


# ---------- packet ----------

def folder_for(job: dict) -> Path:
    company = re.sub(r"[^\w&.\- ]", "", job["company"]).strip()
    title = re.sub(r"[^\w&.\- ]", "", job["title"]).strip()[:60]
    return APPLICATIONS / f"{company} - {title}"


def short_company(company: str) -> str:
    return re.sub(r"\b(Corporation|Corp|Inc|LLC|Ltd|Technologies|Company|Co)\b\.?", "", company).replace(",", "").strip()


def spec_text(resume: dict) -> str:
    parts = [resume.get("title_line", ""), resume.get("keywords_line", ""), resume.get("summary", "")]
    for role in resume.get("roles", []):
        parts.append(role.get("intro") or "")
        parts += [f"{b.get('label', '')}: {b['text']}" if b.get("label") else b["text"] for b in role.get("bullets", [])]
    parts += resume.get("earlier", [])
    parts += [f"{s['label']}: {s['items']}" for s in resume.get("skills", [])]
    return "\n".join(p for p in parts if p)


_APPLY_LINE = {"answers.md": re.compile(r"^apply(?:\s+at)?\s*:", re.I),
               "posting.txt": re.compile(r"^(?:apply(?:\s+at)?|source)\s*:", re.I)}
_URL = re.compile(r"https?://[^\s)>\]]+")


def apply_links(folder: Path) -> List[str]:
    """Links on the "Apply at:" / "Apply:" lines of answers.md and the "Source:" line of posting.txt.

    The packet writer often finds the employer's own posting while writing (an aggregator repost has none)
    and puts it there, so packet-build can record it and check it against postings you've applied to.
    """
    links = []
    for name, pattern in _APPLY_LINE.items():
        path = folder / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            plain = line.replace("*", "").strip().lstrip("->").strip()
            if pattern.match(plain):
                links += [u.rstrip(".,;") for u in _URL.findall(plain)]
    return list(dict.fromkeys(links))


def build(folder: Path, company: str, word_only: bool = False, extra_problems: List[str] = ()) -> dict:
    """Render everything in a job folder from resume.json / cover.json and run every check.

    word_only (scheduled runs) makes just the Word files and never opens Word: page counts wait for the PDFs,
    and the ATS audit reads the Word text instead of the PDF's text layer. extra_problems (e.g. "you already
    applied to this posting") keep the build from counting as clean.
    """
    resume = json.loads((folder / "resume.json").read_text())
    company_short = short_company(company)
    report = {"lint": [], "claims": [], "pages": {}, "ats": None, "files": [], "pdfs": not word_only}
    posting = job_context(folder)  # a letter may quote the posting's numbers; a résumé may not

    resume_docx = folder / f"{NAME} Resume {company_short}.docx"
    render_resume(resume, resume_docx)
    report["lint"] += [f"résumé: {i}" for i in lint(spec_text(resume), "resume")]
    report["claims"] += [f"résumé: {i}" for i in claim_check(spec_text(resume))]
    report["files"].append(resume_docx.name)
    pdfs = []
    if not word_only:
        resume_pdf = to_pdf(resume_docx)
        report["pages"]["résumé"] = pdf_pages(resume_pdf)
        report["files"].append(resume_pdf.name)
        pdfs.append(resume_pdf)

    cover_path = folder / "cover.json"
    if cover_path.exists():
        cover = json.loads(cover_path.read_text())
        cover.setdefault("title_line", resume["title_line"])  # letter header matches the résumé
        letter_docx = folder / f"{NAME} Cover Letter {company_short}.docx"
        render_cover_letter(cover, letter_docx)
        letter_text = "\n".join(cover["paragraphs"])
        report["lint"] += [f"cover letter: {i}" for i in lint(letter_text, "letter")]
        report["claims"] += [f"cover letter: {i}" for i in claim_check(letter_text, posting)]
        report["files"].append(letter_docx.name)
        if not word_only:
            letter_pdf = to_pdf(letter_docx)
            report["pages"]["cover letter"] = pdf_pages(letter_pdf)
            report["files"].append(letter_pdf.name)
            pdfs.insert(0, letter_pdf)

    if pdfs:
        combined = folder / f"{NAME} Application {company_short}.pdf"
        combine(pdfs, combined)
        report["files"].append(combined.name)

    terms_path = folder / "ats_terms.json"
    if terms_path.exists():
        text = pdf_text(pdfs[-1]) if pdfs else "\n".join(p.text for p in Document(str(resume_docx)).paragraphs)
        report["ats"] = ats_audit(text, json.loads(terms_path.read_text()))

    problems = list(extra_problems) + list(report["lint"]) + list(report["claims"])
    if report["pages"].get("résumé", 0) > 2:
        problems.append(f"résumé is {report['pages']['résumé']} pages (limit 2)")
    if report["pages"].get("cover letter", 0) > 1:
        problems.append(f"cover letter is {report['pages']['cover letter']} pages (limit 1)")
    report["problems"] = problems
    (folder / "check_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report
