// Reads a Google for Jobs results page (google.com/search?...&udm=8) in Chrome. See radar/google_jobs.py.
// "list": every card's text lines, numbered, after scrolling once for the second page.
// "detail": finds the cards by title and company (Google re-renders the list, so positions shift), opens each,
// expands "Show full description", and reads the description and apply links (query strings dropped).
// Results are saved as a JSON download that `./jr google-import` / `google-details` read.
await (async () => {
  const MODE = "__MODE__", TARGETS = __IDS__, NAME = "__NAME__", PAGES = 2, MAX_TEXT = 8000;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cards = () => [...document.querySelectorAll("[data-share-url]")];
  const lines = (el) => el.innerText.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 12);
  const visible = (el) => el.getClientRects().length > 0;
  const emit = (data) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(data)], { type: "application/json" }));
    a.download = NAME;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return `saved ${data.length} rows to Downloads/${NAME}`;
  };

  if (MODE === "list") {
    for (let p = 1; p < PAGES; p++) {
      const before = cards().length;
      const last = cards().pop();
      if (last) last.scrollIntoView();
      window.scrollTo(0, document.body.scrollHeight);
      for (let i = 0; i < 12 && cards().length === before; i++) await sleep(500);
    }
    return emit(cards().map((c, i) => ({ i, lines: lines(c) })));
  }

  // The detail panel for the open job: the visible "Job description" block whose container names the job
  // but doesn't hold the results list.
  const panelFor = (title) => {
    for (const h of document.querySelectorAll("div,span,h2,h3")) {
      if (h.childElementCount || h.textContent.trim() !== "Job description" || !visible(h)) continue;
      let p = h.parentElement;
      while (p && !p.querySelector("[data-share-url]") && !p.textContent.includes(title)) p = p.parentElement;
      if (p && !p.querySelector("[data-share-url]")) {
        let d = h.parentElement;
        for (let k = 0; k < 4 && d.textContent.length < 400; k++) d = d.parentElement;
        return { panel: p, text: d.textContent.replace(/\s+/g, " ").trim() };
      }
    }
    return null;
  };
  const out = [];
  for (const t of TARGETS) {
    const card = cards().find((c) => { const l = lines(c); return l.includes(t.title) && l.includes(t.company); });
    if (!card) { out.push({ i: t.i, missing: true }); continue; }
    card.scrollIntoView({ block: "center" });
    const cardLines = lines(card);
    (card.querySelector("[role=button]") || card).click();
    let found = null;
    for (let k = 0; k < 15 && !found; k++) { await sleep(400); found = panelFor(t.title); }
    if (!found) { out.push({ i: t.i, lines: cardLines, missing: true }); continue; }
    const more = [...found.panel.querySelectorAll("[role=button],button,div")]
      .find((b) => b.childElementCount <= 2 && /^show full description$/i.test(b.textContent.trim()) && visible(b));
    if (more) { more.click(); await sleep(600); found = panelFor(t.title) || found; }
    // Apply options sit in a row beside the panel, not inside it. Only the open job's are visible, so take
    // every visible "Apply on ..." link (Meta's LinkedIn option was missed when this looked inside the panel).
    const apply = [...document.querySelectorAll("a")]
      .filter((a) => visible(a) && /^apply/i.test((a.innerText || a.getAttribute("aria-label") || "").trim()))
      .map((a) => { try { const u = new URL(a.href); return u.origin + u.pathname; } catch (e) { return ""; } })
      .filter(Boolean);
    out.push({ i: t.i, lines: cardLines, description: found.text.slice(0, MAX_TEXT), apply: [...new Set(apply)] });
  }
  return emit(out);
})()
