# Outlines: add or remove a company (CP), 8-K ingestion (EK), internet search (WS)

Status: **OUTLINE, not a spec.** Each becomes a full spec, with a Status block, when its turn comes. INTERNAL document. All three are full path.

## A. CP: add or remove a company

### What exists (research note, section 4)

No one-trigger flow. Each stage is a local command joined by hand (docs/RUNBOOK.md:599-620). The company list is code, not data: `FILERS` (13, universe.py:12-26), `UNIVERSE`, `canonical_entities.json` (two copies; the packaged one is read), the generated `web/src/lib/pages.generated.ts`. About 22 files mention the count 13, several as hard test pins (the investigators' count). `ingest` refuses tickers outside FILERS. Extraction is paid, with a dry run and a cap. Production cannot run the pipeline: the data lake is local and the serve image lacks the parsing and model libraries.

### Scope

**`add-company`.** Look up the name, show ticker and CIK, ask to confirm; run the free stages (download, parse, chunk); print an exact extraction quote and wait for approval; run the paid stages under the cap; build the graph locally and run the checks; stop before the deploy. The deploy stays the manual dump/seed sequence, with the corrected kill-switch steps from M2a.

**`remove-company`.** Default for approval: the company stops being a filer (its filings, metrics and pages go) and stays an entity that other filers mention. Same shape: confirm, rebuild locally, checks, stop before the deploy. No paid stage.

**The trigger** is the local command. A trigger on the site is not possible without a build worker off the laptop.

Likely work: the company list as one data source; the count pins derived from it; the name lookup with a refreshed `company_tickers.json`; the orchestrator with resume after a failed stage; a full eval rerun and new examples per change.

Carried in from M2a: older XBRL metadata; paid direction and tense re-adjudication of relation edges, dry-run estimate first; per-id edge quotes; Wistron and Fabrinet as entities, if not done in M2a; negative cases in the extraction eval.

### Security points for the spec

LLM extraction over new filing text (indirect injection); a company name typed by the operator (direct input into lookups and file paths); file I/O on downloaded documents; SEC fair-access limits and identity; the cap, dry run and confirmation on every paid loop, with retry and checkpoint; no pipeline code in the serve image.

### Open owner decisions (recommendation first)

1. **Removal semantics.** Recommended: (a) not a filer any more, still an entity others mention. Others: (b) purge every mention; (c) hide on the site only.
2. **Which company first.** Recommended criterion: a domestic 10-K filer with a standard filing layout, to keep the first run clear of parser fallbacks. The owner picks the name, from the buyer view.
3. **A web admin console, ever?** Recommended: no for now. It needs a build worker off the laptop, real admin authentication and a dump-swap; none is planned.
4. **Budget per company** before the first run (derived today: $0.3-2.3 likely, $0.5-3.5 worst, plus the eval rerun).
5. **Upload workspaces.** Each add or remove ends in a graph deploy that deletes them. Recommended: accept, and announce the deploy time. The alternative is to bring back the dump-swap work that was dropped.

## B. EK: 8-K ingestion

Earnings exhibits (EX-99.1) for the 11 domestic filers. The exhibit list is not in the submissions JSON; it needs the Archives filing index, which answered 403 to a generic User-Agent on 2026-11-20. The fetch must send the declared identity; the monitor's fallback agent would fail. 6-K content for TSM and ASML is unverified and is checked before it is scoped in. Extraction is paid per filing; the cost is unmeasured. It reuses CP's stages and its cap, dry run and approval.

Open owner decisions: **scope**, recommended item 2.02 earnings releases only at first; **a budget per filing**, after a dry run on three filings.

## C. WS: internet search

### What exists

No web search. The agent has 7 closed tools, none web (agent/tools.py:41-42). After M2a the monitor names new 8-Ks and BIS rules, with no LLM.

### WS1: official sources (candidate ids are the research note's)

1. **F3 Cross-filer mentions** through EDGAR full-text search, as a page block with no LLM. The endpoint (`efts.sec.gov/LATEST/search-index`) answered on 2026-11-20 but is not a documented API; stability and limits are unverified. The untracked `efts.json` in the repo root is a leftover of that check: delete it or ignore it, never commit it.
2. **F4 BIS press-release watch.** No official RSS was found; scraping is brittle. Optional.
3. **F5 Check a pasted claim against filings.** No web. One ask within today's caps. "Not in the filings" must never read as "false". Pasted text is user input into a prompt, so it needs its own injection cases.
4. **Linking new rules to companies** (the label left out of the M2a panel). It needs the linker's matching without importing ingestion code into the serve image.

### WS2: open web, not before a council

- **F6 Open-web news panel.** High credibility risk beside filing-grade evidence. Display and storage terms of the search providers are unverified.
- **F7 Agent web tool.** The largest injection surface. Its paid floors ($1.3317, $1.3357) exceed the per-address ceiling of $1.3272, so it also needs a cap redesign.

### Security points for the spec

External input validation as in M2a S6-S9; no request amplification (poll and cache, never per user request); no fetched text into a prompt without the sanitiser and adversarial cases; no personal data from fetched pages stored or shown; links open in the reader's browser, never fetched by the server.

### Open owner decisions (recommendation first)

1. **Is an undocumented SEC endpoint acceptable for F3?** Recommended: yes, as a clearly optional block that degrades to nothing, after a stability check over some weeks.
2. **F5 in or out?** Recommended: in. It closes a buyer question without any web risk.
3. **Open web (F6, F7).** Recommended: convene the llm-council when WS1's blocks are live, not before. The verdict goes to `docs/decisions/`.
4. **F4.** Recommended: skip unless an official feed appears.
