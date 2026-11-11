# M5b redesign: a frontend a newcomer understands and an analyst trusts ("B combined")

Status: **APPROVED by the owner 2026-11-10** ("all as recommended": the spec r2 and D-R1 to D-R9 as recommended), after an Opus
review of r1 (section 14). Path: full (new feature
on the sensitive list: input handling, a vendored runtime asset with a new licence, the file-serving path, a possible
database query change). Amends `docs/specs/m5b-frontend.md` (the M5b spec); everything in it that this document does not
name stays in force.

Owner decisions already taken (2026-11-10): direction "B combined" (`docs/v2/research/m5b-ux/decision.md`); replace the
character-for-character copy parity with a new copy deck while the factual statements stay exact; allow self-hosted fonts;
cost line "Free to use, up to 5 live questions every 10 minutes"; fix the Company page in this round (not hide it).

Inputs: research `docs/v2/research/m5b-ux/research-brief.md`; mockups `docs/v2/research/m5b-ux/mockups/dir-b` (base),
`dir-a` and `dir-c` (borrowed parts); judging `decision.md`, `judges.json`; evidence of the problem
`docs/v2/M5B_PERSONA_WALKTHROUGH_RESULTS.md` sections 4-5. Plan by `fable-architect` (2026-11-10), adapted here.

## 1. Why

On the live `/v2/` (release v20) three first-time-visitor readings scored 2, 3 and 2 of 5 for "understood quickly": no
sentence says what the site is, the question box sits below 51 long examples, the cost is unclear, and the wording is
insider language ("Benchmark questions (instant, cached)", "served from cache - no model call", "numbers not checked",
"filings pending"). The Company page shows raw integers, internal metric names, decade-old rows first and relationship rows
that read backwards. The owner's bar: "even a noob should be able to quickly grasp this website", while an advanced user
keeps workspaces, uploads, depth of search and filing comparison.

## 2. Scope

In: the look and first-run experience of every `/v2/` page (shell, Ask, answer view, evidence panel, Company, Workspace,
Freshness, Method & Evidence, not-found), a new copy deck, self-hosted fonts and the one server line that serves them with
the right type, the Company page fixes, and the UI rule for each known data defect (section 9).

Out: the legacy page (`/legacy`, byte-identical), the SSE, Turnstile, workspace, upload and evidence contracts, every LLM
prompt and the answer pipeline (no prompt change this round), the data fixes themselves (section 9), an "In short" summary
(decision D-R1), the hover preview and `?q=` prefill (cut), and the `UI_V2` flip (after this spec, by the owner, as M5b I5).

## 3. Requirements

Numbered RD (redesign). Section 7 maps each to its test.

**Shell and theme**
- RD1 Design tokens: B's palette in light and dark (prefers-color-scheme), keeping the shadcn variable names so
  `components/ui/*` keeps working; a 3 px focus colour; the highlighter trio (`--mark`, `--mark-ink`, `--mark-edge`: dark ink
  on yellow in both schemes). The government-rule stamp uses its own hue that is not yellow and not the highlighter (so a
  highlighted quote and a rule are never confused), plus its words and shape. Type scale 13/15/17/21/28/44; tabular figures
  for every number.
- RD2 Fonts: Source Sans 3 version 3.052R (Regular, Semibold, Bold) and Source Serif 4 version 4.005R (Regular, the
  `SourceSerif4-Regular` face, not an optical-size variant), the unmodified `WOFF2/TTF/*.ttf.woff2` files from Adobe's
  GitHub releases (about 405 KB for the four), vendored under `web/src/assets/fonts/` with a README giving source
  URL, version and sha256 of every file; each repository's licence text (the `LICENSE.md` at the same tag, saved as
  `OFL-SourceSans3.txt` and `OFL-SourceSerif4.txt`) sits in `web/public/licenses/`, so it ships in `dist` unchanged (SR5). No npm font package, no subsetting (the OFL reserves the name for unmodified files),
  no `data:` font. `font-display: optional`; `SourceSans3-Regular` and `SourceSans3-Semibold` are preloaded (owner 2026-11-11, so headings draw in
  Source Sans 3 on a slow first visit too; `<link rel="preload" as="font" type="font/woff2" crossorigin>`); a size-adjusted fallback `@font-face` with `src: local(...)` keeps the fallback's
  metrics close. Serif is used only for words copied from a source; everything semigraph writes, and every number, is sans.
- RD3 Header: brand; Ask, Companies, Workspace; the freshness line as a link to Freshness, reading "Data as of <date>,
  <n> newer filings being added" (or the unavailable and failed forms in plain words); "How it works" to Method. All links
  inside `<nav aria-label="Main">`. One date style across the site ("Sep 24, 2026"), dates formatted in UTC.

**Ask, first screen (before any question)**
- RD4 Order: h1 "Ask about 13 chip and AI companies." (the count from `FILERS.length`); a grounding sentence with three
  live source stamps (filing passage, reported number, government rule), each opening a click popover with a real example
  pinned to a recorded evidence fixture; the labelled question box and Ask button; the cost line (RD4a); "Not financial
  advice" and a link to How it works; "More options"; the starter questions. At 1440x900 and at 390x844 the headline, the
  box and the cost line are on the first screen without scrolling.
- RD4a Cost line: "Free to use, up to {n} live questions every {m} minutes ({d} a day)." with n, m and d read from
  `/api/stats.limits` (`per_ip` and `per_ip_per_day`); until stats load, or if they fail, the approved defaults (5, 10, 20)
  are shown. Example questions are described as instant and free. Never "unlimited". The wording in brackets is decision
  D-R5.
- RD5 Starters: six questions grouped by job (understand a company, check a number, track export rules, see what changed),
  each group with a one-line "You get ..." naming the kind of proof; drawn as ruled rows, not cards. The six come from a
  curated id list (decision D-R6) intersected with `GET /api/examples`; D1 and X4 are never starters (their saved answers
  contradict a detail of their own source). "Show all examples" (a disclosure) lists the rest by company.
- RD6 "What an answer looks like": beside the box on desktop (below it on a phone), a static sample with one verbatim
  sentence and its source line; the sentence and source come from a recorded evidence fixture and a test checks the
  sentence is in that fixture's text. Amended 2026-11-11 by `docs/decisions/003-evidence-highlight.md`: the sample shows no
  highlight and no legend (real answers rarely produce one); the highlight and its legend stay in the evidence panel (RD18).
- RD7 More options: a disclosure holding "Depth" as a labelled radio group: Standard (recommended; `hybrid`), Deep research
  (slower; `agent`, shown only when `/api/stats.agent_enabled`), Basic text search (comparison only; `vector`). R23 holds:
  with a workspace open only Standard is offered.
- RD8 Empty question: pressing Ask with an empty box shows an inline message next to the box and in the status region; no
  request is sent.
- RD9 The company list moves below the starters.

**Answer view**
- RD10 A compact question bar on top; a plain status ("Saved answer, shown instantly" for a saved answer; plain steps while
  live); the question as the answer heading (`ANSWER_HEADING_ID`, focus target as today).
- RD11 Citation chips ("source stamps"): still the `<button type="button">` of A1 with `data-chip-key`; neutral surface (not
  solid green); an icon and shape per kind; a government rule carries the words "Government rule"; labels name the source
  ("Nvidia 10-K FY2024", "paragraph 162"; no leading zeros, no pilcrow); `aria-expanded` on the chip whose evidence is open.
  Until evidence loads the label shows the filing year, never a company guessed from the accession prefix.
- RD12 (none this round: "In short" is deferred, D-R1).
- RD13 "Read with care": a short note beside change claims, shown when the answer contains one of the pinned hedge phrases,
  citing the measured hand-check counts from the facts file.
- RD14 "What was checked", plain sentences from the deck, never "verified", never a percentage:
  - a saved benchmark answer (`source == "benchmark"`; the store refuses to seed one without complete passing checks,
    `store.py` `refusal_reason`): its checks as recorded, worded "checked when the answer was saved";
  - a live answer: its checks, worded per result ("All figures matched the filings", "Some figures could not be matched",
    "No dollar amounts or percentages in this answer", and the cited-sources line);
  - a repeated live answer served from the cache (`put_answer` stores no checks): "Answered earlier. The checks are shown
    only when an answer is first written; open a source to read it." No claim either way.
- RD15 "Sources (N)" under every answer, built from the same parsed citations as the chips.

**Evidence panel**
- RD16 The same single `<aside aria-label="Evidence">` and controller (A5 semantics unchanged): a sticky right column at
  900 px and wider, a fixed bottom sheet below 900 px with `main` padded by its height while open (WCAG 2.4.11), a sticky
  header with a visible "Close" button at least 44 px (accessible name stays "Close evidence").
- RD17 Body: title; Section / Filed / Status list (an "Older filing, replaced by ..." band when superseded); the passage in
  serif; the cited words marked with the highlighter; a legend "Highlighted: the words copied exactly from the source"; the
  source link; "Details for analysts" (ids) in a disclosure.
- RD18 Highlight rule: exact match only after whitespace normalisation, fragments split on an ellipsis, at least 20
  characters. A quote that straddles two passages, or a passage cut short, gets no mark and no legend. Marks are text nodes
  inside `<mark>`, rendered outside `AnswerText` (R21 unchanged); never HTML from data.

**Company page**
- RD19 Header: company switcher, name and ticker, the latest annual and quarterly report in words ("10-K for the fiscal year
  ended Jan 25, 2026, filed Feb 25, 2026"); for TSMC and ASML, which file no 10-Q: "Files an annual report on Form 20-F; no
  quarterly 10-Q". "Ask about <company>" and "Compare filings" actions.
- RD20 At a glance: revenue, net income and R&D for the latest fiscal year with the prior year beneath, and the number of
  government rules linked; each figure is a source stamp opening its XBRL fact.
- RD21 Section navigation: one row of in-page links (Key numbers, Government rules, Relationships, Filings, Risk changes);
  on a phone the row scrolls sideways and never wraps; following a link focuses the target heading (`scroll-margin-top`); a
  hash change does not trigger route focus. Every section stays in the DOM (not ARIA tabs). The hash is matched against the
  fixed list of section ids with `getElementById`, never `querySelector(location.hash)`; anything else is ignored.
- RD22 Key numbers: columns are the company's latest fiscal years, newest first, labelled "FY2026 (ended Jan 25, 2026)";
  readable names with a one-line plain meaning ("Revenue: money from sales"); compact values in the reported currency ($,
  EUR, TWD; never a default "$") with the exact value in the accessible name and a "Show exact values" switch; a fiscal-year
  sentence; a metric with nothing in these years reads "Not in our data for these years. The latest we hold is FY2012."
  Every figure is a source stamp.
- RD23 Government rules: its own section with the notice that these are US export-control rules linked by keyword matching,
  actions by the government and not statements the company made; each rule carries the "Government rule" stamp, date and
  title. With none linked: the D-R3 sentence.
- RD24 Relationships: never render "A CUSTOMER_OF B". Directional relations show the other company, the verbatim quote and a
  chip under "In the filing's own words", listed before competitors; competitors become a name list with chips.
- RD25 Filings and risk changes: today's content and the four `KIND_*` hedge labels unchanged, restyled; "Compare filings"
  jumps to Risk changes. Active risks are labelled "Summaries written by semigraph, not quotes" until the data track adds
  their citations (D-R4 batch).

**Workspace, Freshness, Method, not-found**
- RD26 Workspace for a first-time visitor: what it is in one sentence; privacy in plain words limited to what the system
  does: only someone with the workspace id and key can open it; documents are deleted 24 hours after creation (within about
  15 minutes after that); when you ask a question, passages from your documents are sent to the AI model provider to write
  the answer; three steps; the accepted file types; Create; Restore behind a disclosure. The open workspace keeps its
  behaviour, restyled.
- RD27 Freshness, Method & Evidence and not-found restyled and reworded through the deck; Method keeps the facts file, with
  INTRO there as the technical explanation (the plain grounding sentence of RD4 is the newcomer's version).

## 4. Copy deck

- Every user-facing string lives in `web/src/copy/deck.ts` (plain strings or small pure functions), except the facts file
  and the `KIND_*` labels. `Company.copy.ts` (other than `KIND_*`) and `Workspace.copy.ts` fold into the deck.
- Facts that stay exact, with their pin sources:

| Export | Lives in | Pinned by (and stays pinned by) |
|---|---|---|
| INTRO, WITHDRAWN, RELIABILITY, CHECKS_HINT | `pages/Method.copy.ts` | `tests/test_web_method_copy.py` against the legacy `index.html` |
| AGENT_HINT, TRACING_NOTE | moved from `Ask.copy.ts` to `pages/Method.copy.ts` | `tests/test_serve_agent_ui.py:89-97` assertions, and `tests/test_web_method_copy.py` extended to pin them against `index.html` (today pinned by `test_web_ask_copy.py`) |
| KIND_NEW, KIND_DROPPED, KIND_UNSETTLED, KIND_CHANGED | `pages/Company.copy.ts` (unchanged) | `context_layout._ITEM_SECTIONS` via `tests/test_answerer_blocks.py` and `test_answerer_context.py` |

- The three lib files with differential tests (`answerMeta.ts`, `askStatus.ts`, `evidenceView.ts`) and `steps.ts` gain a
  `code` (with params) on each badge, status and fact; their legacy `text` stays. The three differential tests project each
  result to its legacy fields (`{text, cls}` and the like) before comparing; their assertions do not change. The UI renders
  `deck[code]` (a total `Record`).
- `tests/test_web_ask_copy.py` becomes `tests/test_web_copy_deck.py`: (1) the matcher self-tests; (2) every sentence-like
  literal under `web/src` lives in `copy/`, the facts file, `Company.copy.ts` or the four lib files; (3)
  `artifacts/m5b_copy_deck.json`, a recorded snapshot, so every copy change is a reviewable diff; (4) fact invariants: the
  rule notice names the Federal Register, keyword matching and "not statements"; the measured counts are present; no change
  label says "removed" or "dropped"; no "verified", "unlimited" or "AI-powered"; (5) the cost line's default form equals the
  approved sentence and its numbers equal the Settings defaults, and the rendered line uses `/api/stats` when present.
- `docs/v2/M5B_TEST_CLASSIFICATION.md` gains the rename (`scripts/check_test_map.py` stays green).

## 5. Security requirements and abuse cases (checkable)

- SR1 No new origin; the CSP in `src/semigraph/serve/routes.py` is unchanged (`default-src 'self'`, no `font-src`, already
  covers same-origin fonts). `tests/test_dist_contract.py` allows `@font-face` only when every `src` is a same-origin
  `/v2/assets/*.woff2` `url()` or a `local()`; `data:` fonts, remote fonts and ttf/otf/woff files fail; the preload links
  carry `crossorigin` and point at a dist woff2. Tests for each.
- SR2 S1 and S2 hold: no `dangerouslySetInnerHTML`, `innerHTML`, or `href`/`src`/`style` from data; the highlighter and the
  deck render text nodes only. Abuse case: a passage or quote containing `<img onerror>` or a `javascript:` string renders as
  text, and the highlighter still marks only an exact copy (negative tests with the hostile fixtures of `company.test.tsx`).
- SR3 S4 holds: every source stamp, including the glance figures and the specimen stamps, passes `classifyId` before any
  request; a `doc:` id outside a workspace makes no request.
- SR4 Starters can never become paid calls: a starter always sends a public `hybrid` ask (the cache key includes the
  strategy, `store.py`), resets the depth control to Standard, is disabled while a workspace is open (R23), and a starter id
  missing from `/api/examples` is dropped, never typed. Tests: one per rule.
- SR5 Vendored fonts: a vendored-assets table in `tests/test_web_licenses.py` (path, OFL-1.1, sha256, source URL) is checked
  against `web/src/assets/fonts` and `dist`; `RUNTIME_ALLOWED` for npm packages is unchanged; the licence files ship in
  `dist`. No lockfile changes (no dependency audit needed unless one does).
- SR6 Font content type: `src/semigraph/serve/ui_v2.py` registers `.woff2` as `font/woff2` (`mimetypes.add_type`), because
  Python 3.13's table has no mapping and the files would be served as `application/octet-stream`; a pytest checks the header on
  `/v2/assets/*.woff2`, and another that `ui_v2.py` registers the type itself (a Linux host's own table may already map it); S9's Opus security review covers the `ui_v2.py` diff.
- SR7 If D-R2 is approved (`retrieval/dossier.py`, a database path): the query stays parameterised, read-only and bounded
  (4 periods per metric), with a negative test that a ticker outside the 13 still returns 404; S16 amended for this one
  constant; `ecc:security-reviewer` (opus) on the diff. The answer template fingerprint does not include this file, so the
  saved examples stay valid.
- SR8 No LLM, prompt or tool change this round, so no prompt-injection surface changes.
- SR9 Escape: one Escape closes the innermost open thing only (a popover before the panel); the drawer ignores events with
  `defaultPrevented`; tests with focus inside and outside the popover (Base UI also listens on `document`).
- SR10 Hash handling (RD21): a crafted `#...` (selector syntax, a very long string, an unknown id) is ignored without an
  exception and moves no focus (negative test).

## 6. Accessibility (A1-A10 of the M5b spec hold; changes)

- A4 amended: the depth control is a labelled radio group (fieldset and legend).
- A5 amended: position and the visible "Close" text change; semantics, focus moves and the non-modal rule do not.
- A8 re-audited: contrast of chips, marks, stamps and the rule notice at 4.5:1 or better in both schemes; a government rule
  told apart in forced-colours mode; status never by colour alone.
- A9 amended: bottom sheet under 900 px; no motion anywhere (R30's three-way check stays).
- New: targets of chips and stamps at least 24x24 px (WCAG 2.5.8); the section-nav row is keyboard reachable and the
  focused link scrolled into view; popovers are click-opened (WCAG 1.4.13 then does not apply), focus moves in, Escape
  returns it to the trigger.
- A11 is unchanged and still asks for a person: decision D-R7 settles whether an agent-run NVDA pass (the agent drives NVDA
  and reads its speech log) closes it, or a person also listens.

## 7. Acceptance criteria and traceability (tests written first; RED then GREEN in Status)

General:
1. `uv run pytest tests/test_dist_contract.py tests/test_web_copy_deck.py tests/test_web_method_copy.py
   tests/test_web_source_scan.py tests/test_web_licenses.py tests/test_static_ui.py tests/test_serve_agent_ui.py
   tests/test_serve_ui_v2*.py` green; the real-dist leg does not skip.
2. In `web/`: typecheck, lint, `vitest --coverage` at 80% or more, the full Playwright suite, Lighthouse accessibility 95
   or more and CLS at most 0.02 on the five pages.
3. `git diff --stat` shows no change to `src/semigraph/serve/static/index.html`, `stream_runtime.py`, `guard.py`, any
   prompt or `web/package-lock.json`; `src/semigraph/serve/ui_v2.py` only for SR6; `retrieval/dossier.py` only if D-R2.
4. In both schemes: mark, chip and stamp contrast 4.5:1 or better; chip targets 24 px or more; axe 0 violations on every
   page, the answer view and the open panel; no sideways scroll at 390 px, also under a forced wide font.
5. `/legacy` byte-identical; the walkthrough's 29 cached questions still answer and every chip opens.
6. After the deploy: the first-visitor readings rerun on the new screens, and one or two real people do the tasks of
   `decision.md` section 5 (a beginner on a phone taps a starter, then a source, and says within 30 seconds which words came
   from the filing; reads the freshness line correctly; answers "Did Nvidia say this?" about a rule with no; an analyst
   finds Compare filings, Show exact values, Depth and workspace Upload unaided).

| Req | Test file | Assertion |
|---|---|---|
| RD1 | `format.test.ts`, `a11y.spec.ts` | tokens present both schemes; contrast via axe; rule hue distinct from mark |
| RD2 | `test_dist_contract.py`, `test_web_licenses.py` | font rules of SR1; sha256 table; preload of Regular and Semibold only |
| RD3 | `shell.test.tsx`, `shellFreshness.test.tsx`, `route-focus.spec.ts` | nav names, freshness sentence forms, UTC date |
| RD4 | `ask-page.test.tsx`, `ask-first-screen.spec.ts` | order of h1, sentence, box, cost line; on screen at 1440x900 and 390x844 |
| RD4a | `ask-page.test.tsx`, `test_web_copy_deck.py` | stats-driven numbers; defaults until loaded; approved sentence |
| RD5 | `ask-page.test.tsx` | six starters, curated ids only, D1/X4 never, missing id dropped, disclosure lists the rest |
| RD6 | `answerSample.test.tsx` | sentence found in its fixture text; no highlight or legend (decision 003) |
| RD7 | `ask-page.test.tsx`, `a11y-labels.spec.ts` | radio group, agent option gated, workspace leaves Standard only |
| RD8 | `ask-page.test.tsx`, `error-placement.spec.ts` | message next to the box and in status; no request |
| RD9 | `ask-page.test.tsx` | company list after the starters |
| RD10 | `ask-answer-view.test.tsx` | status wording; heading id and focus |
| RD11 | `chips.test.tsx`, `ask-drawer.spec.ts` | button, label forms, rule words, `aria-expanded`, year before load |
| RD13 | `ask-answer-view.test.tsx` | note shown only with a pinned hedge phrase |
| RD14 | `badges.test.tsx` | the three wordings by source; no "verified" |
| RD15 | `ask-answer-view.test.tsx` | count equals chips' distinct sources |
| RD16 | `evidenceDrawer.test.tsx`, `evidence-sheet.spec.ts` | sheet under 900 px, padding, 44 px Close, name unchanged |
| RD17 | `evidenceDrawer.test.tsx` | body parts, superseded band, disclosure |
| RD18 | `highlight.test.ts` | exact match, ellipsis split, 20 chars, no mark on straddle or cut |
| RD19 | `company.test.tsx` | report sentence; 20-F wording for TSM and ASML |
| RD20 | `company.test.tsx`, `companyChips.test.tsx` | glance figures are stamps with XBRL ids |
| RD21 | `company-nav.spec.ts`, `companyNav.test.tsx` | one row at 390 px; heading focus; crafted hash ignored |
| RD22 | `companyblocks.test.tsx`, `format.test.ts` | newest first, currency, exact value, "Not in our data" |
| RD23 | `companyblocks.test.tsx` | notice text, stamp, empty-list sentence |
| RD24 | `companyblocks.test.tsx` | no raw relation names; quote and chip; order |
| RD25 | `companyChanges.test.tsx` | `KIND_*` unchanged; Compare filings target; Active risks label |
| RD26 | `workspace.test.tsx`, `workspace.spec.ts` | privacy sentences; Restore disclosure; flows unchanged |
| RD27 | `method.test.tsx`, `freshness.test.tsx` | facts file rendered; deck wording |

## 8. Plan (increments; each leaves the app shippable)

Web tests run in the off-OneDrive mirror; each increment ends with `ecc:react-reviewer`, `ecc:typescript-reviewer` and
`ecc:a11y-architect` on its diff (and `ecc:security-reviewer`, opus, where SR6 or SR7 code changes), fixes of CRITICAL and
HIGH, and a commit.

| Inc | What | Tests first | Size |
|---|---|---|---|
| R0 | Contracts, tokens, fonts and `ui_v2.py` woff2 type, deck scaffold and facts move, `lib/format.ts` (UTC dates, compact money by currency, paragraph numbers, fiscal-year labels), Base UI popover and collapsible wrappers, lib `code` fields with the differential tests projecting | `test_dist_contract.py`, `test_web_copy_deck.py`, `test_web_method_copy.py` (extended), woff2 content-type pytest, `test_web_licenses.py` (vendored table), `format.test.ts` | about 1 day |
| R1 | Shell and Ask first screen (RD3-RD9, RD4a) | `ask-page.test.tsx`, `shell.test.tsx`, `shellFreshness.test.tsx`, `answerSample.test.tsx`, new `ask-first-screen.spec.ts`; e2e wording and selector updates in `ask-flows`, `ask-keyboard`, `a11y-labels`, `freshness`, `error-placement` | 1-1.5 days |
| R2 | Answer view, chips, evidence panel, highlighter, sources, checks (RD10-RD18) | `ask-answer-view.test.tsx`, `highlight.test.ts`, `evidenceDrawer.test.tsx` (additions), `chips.test.tsx`, `badges.test.tsx`, new `evidence-sheet.spec.ts`; `ask-drawer` selectors | 1.5-2 days |
| R3 | Company page (RD19-RD25), plus D-R2 if approved | `company.test.tsx`, `companyblocks.test.tsx`, `companyChips.test.tsx`, `companyNav.test.tsx`, `company.spec.ts`, `company-nav.spec.ts` | 1.5 days |
| R4 | Workspace, Freshness, Method, not-found, final copy sweep (RD26-RD27) | `workspace.test.tsx`, `method.test.tsx`, `freshness.test.tsx`, `workspace.spec.ts`, `method.spec.ts`; deck snapshot final | 1 day |
| Close | Full regression, stage-4 security (`/security-review`, gitleaks, semgrep, `differential-review`, `spec-to-code-compliance`), Opus verifier, owner deploy to `/v2/`, NVDA pass, real people, owner click-through (B6) and sign-off, `UI_V2` flip | | |

In every increment, A1-A10 tests may change selectors and wording only, never their assertions about behaviour.

## 9. Data track (separate; the UI states a rule and does not fix data)

| Defect | UI rule this round | Data fix (later spec) |
|---|---|---|
| Relationship rows that read backwards ("Apple CUSTOMER_OF Alphabet") | RD24 | Check edge direction in extraction |
| Old capex rows (2011-2012, 2015-2016) | RD22 "Not in our data for these years" | Load missing capex facts |
| Only two periods per metric | D-R2 | - |
| Active risks without citation chips | RD25 label | Evidence ids on the dossier query |
| No fiscal period on evidence | Filing date only | `period_of_report` on `EVIDENCE_QUERY` |
| Examples X1 and T15 missing from the live list | RD5 intersection | Re-seed |
| Saved answers D1 and X4 contradict a detail of their source | Not starters (RD5); the checks wording tells readers to open the source | Re-answer or drop |

## 10. Decisions for the owner (recommendation first)

- **D-R1 "In short": none this round.** Measured on the 51 saved answers: under an honest rule (the answer's own first block,
  every sentence cited, at most 320 characters, not a lead-in to a list) 31 would qualify, but on 29 of them that block is
  the whole answer, so the heading would repeat it; it would add something on only 2 (T5 and X4, and X4 is one of the two
  answers that contradict their source). A useful summary needs a prompt change, a paid re-benchmark and re-seeding all
  saved answers: its own spec later.
- **D-R2 Four fiscal years instead of two on the Company page: yes, in R3.** One constant (`DOSSIER_METRIC_PERIODS`) in a
  database query path, with SR7; saved answers are not affected.
- **D-R3 Empty government-rules list (ASML, Alphabet, Amazon, Meta):** "No export-control rule in our data was linked to
  <company> by keyword matching. This is not a finding that none applies."
- **D-R4 Limit-stating wording on the live site, as one batch** (the strengths-only rule and the M5b spec's S15 cover all
  public copy, so each needs your yes): "Not financial advice"; "Summaries written by semigraph, not quotes" (Active
  risks); "Not in our data for these years. The latest we hold is FY..."; "Basic text search: comparison only; cannot track
  changes over time"; D-R3's sentence; RD14's "Answered earlier. The checks are shown only when an answer is first written".
  Recommended: yes to all; each is the honest alternative to a blank or a misleading label.
- **D-R5 Daily cap in the cost line:** "Free to use, up to 5 live questions every 10 minutes (20 a day)." The 20 is the
  per-address daily cap; the site-wide caps (150 answers, $10 a day) are not shown. Recommended: yes.
- **D-R6 The six starters:** N2 (Nvidia revenue, check a number), D1 is excluded, so D3 (AMD's foundries, understand a
  company), Q2 (ASML's geopolitical risks, understand a company), R1 (rules affecting AMD, track export rules), T7 (Nvidia
  risk factors FY2024 to FY2025, see what changed), NG15 (Micron revenue and capex, check a number). Recommended: this list.
- **D-R7 Accessibility pass (A11):** (a) recommended: the agent runs the full NVDA pass on the new pages and reads the
  speech log, and you (or a person you name) listen to the Ask and Company pages for about 10 minutes; or (b) the agent's
  pass alone, recorded as "agent-run, owner-accepted".
- **D-R8 Fonts load as `optional`:** no layout shift; a first visitor on a slow link may keep the system font for that
  visit. Recommended: yes (the alternative, `swap`, shifts the layout once).
- **D-R9 OFL-1.1 as a runtime licence** (the two font families only, recorded in the vendored-assets table). Implied by
  your yes to self-hosted fonts; listed so the licence change is explicit.

## 11. Superseded or amended in docs/specs/m5b-frontend.md

R4 (same-origin woff2 and `local()` `@font-face` allowed; `data:` fonts still forbidden); R22 (parity replaced by the new
first screen; the select becomes a radio group); R24 (layout only); R25 (layout and labels; `KIND_*` unchanged); R26 (line
wording); R27 (deck plus facts file); R30 (character parity narrowed to the facts file; palette replaced; nothing moves
stays); S9 (`ui_v2.py` gains the woff2 type, reviewed); S10 (vendored OFL fonts recorded); S15 (the banned-phrase scan
stays; parity replaced by the deck snapshot; limit-stating wording per D-R4); S16 (adds `retrieval/dossier.py` only if
D-R2); A4, A5, A8, A9 as in section 6; A11 per D-R7. Unchanged: S1, S2, S4, A1-A3, A6, A7, A10, R21, R23, R31, R33, R34.

## 12. Risks and where the obvious implementation is wrong

1. Wrapping a chip in `Popover.Trigger`: the click would toggle a popover instead of opening the panel and focus would
   return to an unmounted element. Chips stay plain buttons; popovers only for the specimen stamps and the glance figures,
   controlled and anchored, the opener passed explicitly.
2. One Escape closing both a popover and the panel: SR9.
3. Fuzzy highlighting marking words that were not copied: RD18.
4. A dark-mode highlighter with light text, or a box-shadow underline that vanishes in forced colours: dark ink on yellow and
   a real `text-decoration` underline.
5. A fixed bottom sheet hiding the focused element: RD16.
6. Starters becoming paid calls: SR4.
7. "$" on TSMC (TWD) and ASML (EUR) figures: RD22.
8. `new Date("2024-02-21")` shown in local time is Feb 20 in the US: format in UTC.
9. Guessing the company from an accession prefix before evidence loads: RD11.
10. `font-display: swap` shifting the layout, subsetting breaking the OFL name clause, a missing woff2 MIME type: RD2, SR6.
11. Saying "figures matched" where no checks were stored: RD14.
12. A section-nav hash stealing focus or being used as a selector: RD21, SR10.
13. A hard-coded cost line drifting from production limits: RD4a.
14. Test churn from copy changes hiding a real regression: the deck snapshot makes every wording change a reviewed diff.

## 13. Owner-facing summary of what changes for a visitor

The first screen says what the site is, puts the question box first with a plain cost line and six starter questions;
answers name their sources in words, highlight the exact copied words and open the source beside the answer (a bottom
sheet on a phone); the Company page shows readable figures newest first, keeps government rules in their own section and
no longer shows relationship rows that read backwards; every page uses plain words; nothing about how answers are made
changes.

## 14. Review disposition (Opus review of r1, 2026-11-10: NEEDS_CHANGES, 14 findings)

All addressed in r2: facts-file pins corrected (section 4 table; INTRO kept on Method); differential tests project to legacy
fields (three tests, not four); SR4 completed with the hybrid, depth-reset and workspace rules; SR6 for the woff2 type;
SR1 allows `local()` fallbacks and requires `crossorigin`; vendored-assets licence table (SR5) and D-R9; RD4a reads
`/api/stats` with defaults and D-R5 names the daily cap; "In short" measured and deferred (D-R1), so R21 stays unchanged;
RD14 covers the three answer sources; D1 and X4 never starters; owner decisions added (D-R4 batch, D-R5 to D-R9);
traceability table; SR10 hash handling; fonts pinned by version, type and file, preload of Regular and Semibold only, CLS at most 0.02;
SR9 tests focus inside and outside the popover; RD19 20-F wording; the rule hue is not yellow; acceptance item 6 lists the
real-person tasks; RD6 pinned to a fixture; RD26 states that passages go to the model provider and the deletion window.

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves this spec and D-R1 to D-R9 | **PASSED 2026-11-10**: r1 reviewed by Opus (NEEDS_CHANGES, 14 findings); r2 addresses all; owner: "all as recommended" |
| 2 Dev (TDD) | per increment R0-R4: RED then GREEN, reviews clean of CRITICAL and HIGH | **R0 part 1 (2026-11-10)**: tokens (RD1), self-hosted fonts and preload (RD2), woff2 type (SR6), vendored files and licences (SR5), `lib/format.ts`, facts move (section 4). RED seen for each: font rules in `test_dist_contract.py`, woff2 content type, vendored-font table, `format.test.ts` (module missing), method-copy pins (`AGENT_HINT: not found`). GREEN: pytest dist/licences/serve/copy 300 passed + 33 passed against a mirror build; vitest 1151 passed; tsc and oxlint clean; Playwright 123 passed (axe included); Source Sans 3 400/600/700 load in Chromium in both schemes. Reviews: code (Sonnet) and security (Opus), no CRITICAL or HIGH; fixed with RED first: a timestamp with no offset read as local time (moved the day west of UTC), "-$0", an unknown currency code dropped, and the font checker made an allow-list (the security review's 10 bypass strings are negative tests). Added after the CSS, so with no RED: `tokens.test.ts` (every text pair 4.5:1, edges and focus 3:1, both schemes; rule hue outside the yellow band). First visit on slow 4G with the cache off: the fonts shift nothing (CLS 0 on Company); the old Ask page shifts 0.371 when its example list arrives above the box, which R1's order removes (R1's first-screen spec asserts CLS 0.02 or less); Semibold and Bold miss the window, so headings draw in the bold fallback face (real Arial Bold, no faux bold) on a first visit; preloading Semibold too would change RD2 and is the owner's call. After the fixes: vitest 1191 passed; Playwright 123 passed; pytest 344 passed, 1 skipped (the existing symlink test). Staged into R1/R2 (they land with the first components that use them): the deck scaffold and `test_web_copy_deck.py`, lib `code` fields, the Base UI popover and collapsible wrappers. **R1 slice 1 (deck)**: `copy/deck.ts`; `tests/test_web_copy_deck.py` replaces `test_web_ask_copy.py` (the five parts of section 4; files the redesign has not reached are counted in `NOT_YET_IN_THE_DECK`, which shrinks to empty by R4; the old parity checks still run on `Ask.copy.ts` and the four lib files); snapshot `artifacts/m5b_copy_deck.json`; `liveLimits` for the cost line (a daily cap of 0 = none, so the bracket is left out). `M5B_TEST_CLASSIFICATION.md` maps only the legacy UI tests and never listed `test_web_ask_copy.py`, so it has no row to rename (`check_test_map.py` green). RED: deck tests (no `deck.ts`), `liveLimits` (not a function, then the zero cap). Code review (Sonnet): 2 HIGH (the sentence detector dropped short sentences, digits and JSX text after an expression), fixed with the reviewer's cases as RED matcher tests; MEDIUM fixed (old self-tests restored, `Company.copy.ts` counted, rule notice asserted per file, word ban site-wide except the legacy negations "not verified" in the lib files until R2); two deck sentences softened ("Answers cite their sources", "any government rules"). GREEN: pytest copy 43 passed; vitest 1196 passed; tsc, oxlint clean. **R1 slice 2 (shell, RD3)**: nav Ask, Companies, Workspace, the freshness line as a link to Freshness ("Data as of Sep 24, 2026, 9 newer filings being added"; plain failed and unavailable forms; `lib/shellFreshness.ts`), "How it works"; brand links to Ask. RED: `shellFreshness.test.ts` (module missing), `shellFreshness.test.tsx`, `layout.test.tsx`, `shell.test.tsx` (13 failing). e2e specs renamed to the new link names (`servers.ts` PAGES gains `nav`), each change because a nav name changed. Found by the suite: the open drawer covered the header's right-hand links; until RD16 lands in R2 the drawer starts below the one-row header at `lg` and up. The deck test now flags a counted file that holds no copy (it caught `Nav.tsx`). GREEN: vitest 1204; Playwright 123 passed (the 2 failing are `ask-first-screen.spec.ts`, R1 slice 3's RED, not committed here); pytest copy 43. Review (Sonnet, no CRITICAL or HIGH); MEDIUM fixed in cf168c1 (a stale monitor reads "overdue", the link's name starts "Freshness:", the line is cut visually at `lg`, an e2e at 1024 px). **R1 slice 3a (Ask first screen, RD4-RD9 except the stamp popovers)**: h1 "Ask about 13 chip and AI companies." (tab "Ask"), grounding sentence with the three specimen stamps (not yet clickable, so "Click one to see an example." waits for slice 3b), labelled box with Ask beside it, the cost line from `/api/stats`, "More options" (Base UI collapsible) holding Depth as native radios, RD8's empty-question note (beside the box and in the status region, nothing sent), six starters by job (`lib/starters.ts`) with "Show all examples" by company, the covered companies after the starters (a right column on wide screens); the legacy withdrawn notice joins the footer until R4. RED: `ask-first-screen.test.tsx` (12 of 14 failing), `ask-first-screen.spec.ts` (headline missing); `starters.test.ts` was written first but its RED was not run before the module (stated, not claimed). The old strategy-select and examples-panel tests keep their legacy titles (the test map references them) with bodies moved to the radios and starters; one title that described grouping by type now says how they are grouped, noted on `M5B_TEST_CLASSIFICATION.md` row 18. The fixture server seeds the six starters into its cache (pinned to `starters.ts` by `test_fixture_replay.py`). Found by the suite and fixed: Playwright's label lookup matched the form region too (it is no longer a named region); the Ask button's lighter hover gave 4.41:1 (hover keeps the full colour); under full-suite load the page shifted 0.02-0.04 (the spec now names what moved: the starters arriving, the phone's freshness link pushing "How it works", the fixture's long limit numbers), fixed by reserving the starters' rows and giving the freshness link its own row on phones; the textarea had no focus ring inside the box (the box now draws the 3 px ring; that e2e was written after the fix). GREEN: vitest 1225; Playwright 127 (CLS 0 when run alone, at or under 0.02 under load); pytest copy and fixture 116; tsc, oxlint clean; `check_test_map.py` OK. **R1 slice 3b (specimen stamps, SR3, SR9) with the reviews of baba14d**: the three stamps are Base UI popover triggers (at least 24 px tall) opening a real example: Nvidia's FY2024 10-K paragraph 162 (cited by T7), Nvidia's FY2026 revenue (N2), rule 2025-00711 (R1); the payloads were recorded from the live service on 2026-11-10 (`web/src/test/fixtures/specimens.recorded.json`) and each id is fetched once, through `evidenceFetch` (classifyId first). SR9: while a popover is open a window capture listener handles Escape first and marks it handled, and the drawer now ignores a handled Escape; a non-modal popover closes when focus leaves it (Base UI), so the "focus outside" unit test keeps focus on the stamp. RED seen: `specimens.test.ts` (module missing), the drawer's handled-Escape test, the seven review-fix tests; `specimenStamp.test.tsx` was written before the component but not run first (stated); two of its tests were flaky and were fixed by finding the cause (focus moved in late; the popover closing on focus-out), confirmed by 15 clean runs. Reviews of baba14d: React (Sonnet) no CRITICAL or HIGH; accessibility (Opus) 2 HIGH, both fixed: the shared Button, Input and Textarea drew focus as a 50% box-shadow ring (under 3:1, gone in forced colours), now the global 3 px outline (e2e checks it on three controls); pressing Ask or a starter disabled the focused button and dropped keyboard focus, now `aria-disabled` while streaming (native `disabled`, with a stated reason, only while a workspace is open). Also fixed: Ctrl+Enter asks from the box only (`aria-keyshortcuts`; one e2e started its second ask from the drawer and now uses the box), an empty Ctrl+Enter while streaming no longer replaces the status, a repeated empty Ask changes the status text so it is announced again, the box is `aria-invalid` while the note shows, the starters' note is a paragraph (the region is named by its heading alone), no `aria-busy`, a failed examples request says so, duplicate ids keep distinct keys. GREEN: vitest 1244; Playwright 130 (axe with a popover open included); pytest copy, method and fixture 80; tsc, oxlint clean. **R1 slice 3c (RD6) with the reviews of a880585**: "What an answer looks like" (`components/AnswerSample.tsx`): a sentence semigraph wrote, its stamp, the cited passage with the copied words in a `<mark>`, the source line and the legend; the quote is pinned to the recorded fixture by a test; beside the box from `lg`, below it on a phone (one grid, DOM order form, sample, starters, companies). Security review (Opus): no finding. React review (Sonnet): 2 HIGH fixed with RED first (the popover dialog had no name while loading or after a failure: now "Example: <stamp>"; reopening after a failure showed the old failure: each opening now starts from "loading"); MEDIUM fixed: a repeated empty Ask now re-enters the status region as a new node (keyed), replacing the no-break-space trick a screen reader may not speak (the NVDA pass checks it); the focus-outside test waits deterministically; a cache test; the double focus indicators removed everywhere, with `focusStyle.test.ts` guarding that no component draws its own ring or removes the outline (written after the clean-up, so no RED); the Escape listener is stable and skips IME composition. GREEN: vitest 1256; Playwright 131; pytest copy, method and fixture 80; tsc, oxlint clean. R1 (RD3-RD9) is complete. **R2 (2026-11-10, RD10-RD18, 0abf451..6c39f51)**: the highlighter core and answer sources (RD18, RD15), the hedge phrases with their per-wording hand-check counts (RD13 data, pinned against `context_layout.py` and RELIABILITY), `aria-expanded` on the open chip and the quotes argument of `open` (RD11, RD18), status, steps and checks as codes worded by the deck (RD10, RD14), the evidence panel body and the highlighter (RD17, RD18), the answer view headed by the question with plain status words (RD10), "What was checked" with "Technical details" for the legacy badges (RD14), source stamps (RD11, `lib/stamp.ts`), "Read with care" and "Sources (N)" (RD13, RD15), the panel as a bottom sheet below 900 px and a column from 900 px (RD16), and the Ask copy the redesigned page uses folded into the deck (`Ask.copy.ts` 24 to 12 sentences; the legacy status projection, error clauses, footer and the server's two refusals stay until R4). Pattern: the lib files gained code views (`checkedView`, `statusView`, `stepView`, `evidenceBody`, `stampView`) beside the untouched legacy functions, so the differential tests still compare the legacy text; the answerText differential now compares a chip by its id and place only (its label is a redesigned surface). RED seen first for every slice, except: `hedge.test.ts` in ec39a9d was run after the module existed (the Python pins were RED first), and the S2 guards of `answerNotes.s2.test.tsx` passed on their first run (nothing to fix). Measured before building RD18: the quote-before-chip rule marks 5 of 108 chunk chips in the 53 saved answers (2 answers) and 13/178 and 17/212 in two eval runs, so most panels show no highlight. **Deviations**: RD11's "Nvidia 10-K FY2024" is "Nvidia 10-K, filed 2024 · paragraph 162" (the evidence carries a filing date, no fiscal period) and the item is left out (the panel shows Section); RD13's note sits under the answer, not beside each claim (AnswerText's markup is pinned by R21), and counts a wording only when the sentence makes the claim (R2 review C1/C2); RD15 shows no section for an answer with no citations; RD16's column is `position: fixed` with the page padded beside it (the panel sits outside the router), and the body is padded, not `main` alone, so the footer is never under the sheet. **Reviews**: wf_87d19659-645 over slices 1-6 (no CRITICAL or HIGH; six findings fixed RED first in 5dae6f2); wf_c0626af5-209 over slices 8-11 (three Opus reviewers, every finding checked by an Opus refuter): A1 HIGH chips under 24 px, C1 and C2 MEDIUM "Read with care" on negated or quoted wordings and missing the prompt's wordings, C3 and S1 LOW, all fixed RED first in d3ceec6 (which was committed with the copy-deck test failing on the new pattern strings; fixed in 6c39f51); A2 (chip edge contrast) refuted; C4 (the panel's loading `<output>` is a second status role) and A3 (the Method link does not jump to the reliability section) left open as LOW. GREEN: vitest 1479 passed; Playwright 142 passed (axe included; chip targets at 390 and 1280 px in both schemes; the sheet and column at 390, 899, 900, 1000x480, 1024 and 1280 px); pytest copy deck and method copy 50, dist contract 121 against a mirror build; tsc and oxlint clean. R2 (RD10-RD18) is complete. **R3 (2026-11-10/11, RD19-RD25, D-R2, bd58710..a64b3a0)**: planned by workflow wf_86be9b04-097 (three Opus mappers, a Fable planner, two Opus critics, a Fable revision); the owner answered its five questions with the recommended options (`docs/decisions/001-r3-company-copy.md`: "not in our data" for a metric held in no year, no passage chips on rules, "N government rules listed below", the new wordings, and `annualForm`/`quarterly` in `pages.generated.ts`). Slices: fixtures for NVDA (four years, a stale capex row, a 10-Q, relations both ways), TSM (20-F, TWD) and ASML (20-F, EUR, no rules), with evidence for every id they cite (bd58710); `lib/companyView.ts` and `deck.company` (c3cb6dd); the header in words and the section navigation with SR10's hash rule (67d09a2); `DOSSIER_METRIC_PERIODS = 4`, Key numbers with a "Show exact values" switch, At a glance, figure stamps (282861f); Government rules and Relationships (8691f4d); Filings, Active risks, "Ask about" and "Compare filings" (d6f5a1d). RED seen first for every unit test file; the e2e specs of slices 3-6 (`company-nav`, `company-numbers`, `company-rules`, `company-actions`, `company-stability`) were written after their unit tests went GREEN, and the numbers e2e then found four defects (a 16 px link, a 22.5 px switch, the switch's transition against A9, the Filings table overflowing 320 px), fixed in 282861f; the page-wide no-sideways-scroll check moved from slice 3 to slice 4, where the old metrics table it tripped over was replaced. **Deviations**: RD19's fiscal year end is derived from the figures (the latest year end within 180 days before the annual report; else the clause is dropped), the 10-Q line reads "10-Q filed <date>", and a whole-filing amendment shows under its own form; the switcher sits right under the name, before the reports (CLS); RD20/RD22's glance figures stay compact and open the evidence directly; RD21's nav label "Risk changes" leads to the heading "Compared risk disclosures" (pinned since R25; superseded 2026-11-11 by N2: the heading now says "Risk changes"); RD24 states no direction, and a quote whose first chunk id is invalid shows no chip beside it (with no quote, its chip leads the others); RD25's Active risks show no date (`last_evidenced_at` is never written for them: a data-track item); "Ask about" pre-fills only and route focus stays on the h1; `Company.copy.ts` keeps 7 sentences (the pinned `KIND_*` labels and heading, the pair templates), not 3, until R4's copy sweep. **Gates (slice 7)**: Lighthouse accessibility 1 and best-practices 1 on all ten runs; CLS was 0.29 on mobile (the header's second row of links wrapping when the freshness line arrived, an R1 shell defect) and 0.095 on Company desktop (the reports above the switcher, then a classic scrollbar appearing), fixed in ff0a4e0 to at most 0.012; the full Python suite found the S1 scan failing on RD16's non-literal `setAttribute` (fixed in ff0a4e0); its other failure, `test_tools_probe_w1d.py`, is this checkout's CRLF copy of an LF blob (autocrlf), not code. Coverage: statements 98.6%, branches 95.1%. **Review**: wf_11e7169a-51d (four Opus lenses, an Opus refuter per finding): no CRITICAL or HIGH; C1, A2, T2 (MEDIUM) and A1, T1 (LOW) confirmed, C2 refuted; fixed RED first with seven of the seventeen LOWs in a64b3a0; open as LOW: blank cells for a year missing inside the columns (C4, A5, A7), the fixture's copied chunk grammar (C5), no glance rule count when none is linked (A4, by design), and test-strength notes T3, T6, T7, T9. GREEN: vitest 1566 passed; Playwright 172 passed; full Python suite 11057 passed before the review fixes (the two failures above), its affected files rerun after; tsc and oxlint clean. R3 (RD19-RD25) is complete. **R4 (2026-11-11, RD26-RD27, the copy sweep, a0d536b..f029e28)**: planned by workflow wf_0e7be316-1e6 (three Opus mappers, a Fable planner, two Opus critics, a Fable revision); the owner answered its questions with the recommended options (`docs/decisions/002-r4-workspace-copy.md`). Slices: the sweep's plumbing, `Company.copy.ts` down to the four `KIND_*` labels (97f2af7); `Ask.copy.ts` retired, the legacy words the page still shows pinned as top-level deck exports, the withdrawn-accuracy notice on Method only (c1d99df); Freshness in plain words with UTC dates and the not-found page through the deck (6b0ae81); Method with a plain lead and its reliability section reachable by `#method-reliability`, Layout's route focus honouring a registered section hash (3d13814); the open workspace's words through the deck word for word, uploader names in `<bdi>`, long names and the id wrapping, a drag cue that is not colour alone (0a3dd6a); the first-time Workspace view (RD26 as decision 002 settled it: what it is, three steps as an `<ol>`, the types, who can open it, when it is deleted, what is sent, Create, Restore behind "I have a workspace id and key"), one action runner and one status output, focus to the h2 after create or restore and to Create after delete (d3fb4d4); `NOT_YET_IN_THE_DECK` empty and guarded (30e568f). Owner, 2026-11-11: Semibold preloaded too (78640f4); an llm-council on the RD18 highlight (wf_cd477044-14f) chose D, plain form (`docs/decisions/003-evidence-highlight.md`, 246bb40); the NVDA guide follows the redesigned pages (c3a9c98). RED seen first for every slice's unit tests; the slice-5 and slice-6 e2e cases were written after the fix (stated), and the review's T1 and T7 e2e cases were mutation-checked (with the wrapping rules removed in the mirror, both fail). **Deviations**: RD26's deletion sentence counts from creation and drops "within about 15 minutes" (the clean-up runs only while the site runs); its provider sentence names the question too and says providers; the access sentence adds "It stays open in this browser tab." and the Restore trigger is conditional (decision 002); the h2 loses "(private, 24h)" and the visible label "token" becomes "Workspace key" (storage and header keep "token"); the types line is also the drop zone's description in the open view; RD6's sample shows no highlight or legend (decision 003); RD2 preloads Semibold as well as Regular; `Company.copy.ts` is admitted by its export list and its four labels only; the R2 review's C4 (a second status role in the panel) is handled by scoping the tests, not by changing the panel; `freshnessLine` is kept for the legacy port tests. **Gates (slice 7)**: Lighthouse accessibility 1 and best-practices 1 on all ten runs; CLS at most 0.014; performance 0.97-1 except Workspace on mobile, 0.78 (LCP 6.0 s simulated: its first paragraph waits for the 546 KB bundle and `/api/stats`, which the fixture server sends uncompressed; production's edge sends that bundle as Brotli, 150 KB, immutable); vitest coverage statements 98.66%, branches 95.28%; full Python suite 11061 passed, 438 skipped, the one failure `test_tools_probe_w1d.py` (this checkout's CRLF copy of an LF blob, not code). **Review**: wf_7e50d814-0ed (four Opus lenses, an Opus refuter per finding): no CRITICAL or HIGH; C1/A1 (MEDIUM, one bug: a second Delete press during the first cleared the focus flag), A3, A4 and the test-strength LOWs T1-T4, T6, T7 fixed RED first in f029e28; A5 and T5 refuted; open as LOW: a Create error shows below an open Restore form (A2), and Delete has no confirmation (A6, the legacy behaviour, not added here). GREEN: vitest 1591 passed; Playwright 192 passed; pytest copy deck 48; tsc and oxlint clean. R4 (RD26-RD27) is complete. |
| 3 Regression | section 7 items 1-5 | **PASSED 2026-11-11 for items 1-4; item 5 partly, the rest after the deploy**: (1) the named pytest files 556 passed, 1 skipped (symbolic links need a privilege this account lacks), against the real mirror build (WEB_DIST); (2) typecheck and lint clean, vitest 1591 with 98.66% statements, the full Playwright suite 192, Lighthouse accessibility 1 on the five pages and CLS at most 0.014; (3) since 887f2df only `retrieval/dossier.py` (D-R2) and `serve/ui_v2.py` (SR6) changed under `src/semigraph`, and no prompt, `index.html`, `stream_runtime.py`, `guard.py` or `web/package-lock.json`; (4) the token contrast tests, chip targets at 24 px or more, axe on every page and the open panel, no sideways scroll at 390 and 320 px under a forced wide font (Playwright); (5) `/legacy` is unchanged (`index.html` untouched, `test_static_ui.py` green); the 29 cached questions and their chips are checked on the live `/v2/` after the deploy |
| 4 Security | `/security-review`, gitleaks, semgrep, `differential-review`, `spec-to-code-compliance`; dependency audit if a lockfile changes | **PASSED 2026-11-11** over `origin/v2..f1c5a38` (48 commits): `/security-review` (an Opus finder over the whole range): no findings; gitleaks: 48 commits, no leaks; semgrep (`p/python`, `p/security-audit`, `p/typescript`, `p/react`, baseline `origin/v2`): no new findings; `differential-review` (wf_0e6c0f22-bf1, three Opus lenses: removed code, data to sinks, the workspace token and build): no findings, report `docs/security/differential-review-2026-11-11.md`; `ecc:security-reviewer` (Opus) on the server diff `887f2df..HEAD -- src/` for SR6 and SR7: no CRITICAL, HIGH or MEDIUM, two LOW (SR6's reason named the wrong fallback type; the SR6 test could not see the registration on a host that already maps woff2); `spec-to-code-compliance` in two passes: the RD items (wf_1067ad24-bd8: 25 hold, 4 partial, none contradicted) and SR1-SR10, section 6, D-R1..D-R9 and the section 12 risks (wf_03f4b6c8-e8b, an Opus refuter per divergence: every block implemented or a recorded deviation; D-R7's pass is stage 8); no lockfile, Dockerfile, `fly.toml` or workflow changed, so no dependency audit or checkov |
| 5 Fix findings | if any | **DONE 2026-11-11**, all LOW, none needing the council: RD1 chips sized `text-[0.9em]` (11.7 px in the Company tables) now use the 13 px token, with a source-scan test keeping arbitrary font sizes out (RED first; 7d13895); SR4 starter tests now pick Basic text search or Deep research first and check the ask is `hybrid` and the depth resets to Standard (mutation-checked: sending the current depth, or not resetting, fails both); SR1 negative cases for an `.otf` src and a preload of a woff2 missing from the dist; SR6 reason corrected to `application/octet-stream` and a test pinning the module-level `mimetypes.add_type` call (fails with the line removed); RD2 names `web/public/licenses/` for the OFL texts, RD6 states decision 003, the RD6, RD21 and R3 traceability rows name the real test files. A REQ-14 claim (raw ISO dates in labels) was refuted: `chipLabel` and `evidenceView` are the legacy port kept for the differential tests, and every rendered date goes through `formatDate` (`lib/stamp.ts`, `lib/specimens.ts`, `evidenceBody`) |
| 6 Re-test and verifier | stage 3 and 4 again; Opus verifier | **PASSED 2026-11-11 at a12a7da**: stage 3 again in the off-OneDrive mirror at HEAD's tree: tsc and oxlint clean, vitest 1593 with 95.27% branches and 98.63% statements, the build, Playwright 192; the full Python suite (run from PowerShell, WEB_DIST at the mirror build) 11051 passed, 459 skipped, 2 xfailed, with two failures, both of the checkout and not the code: `test_tools_probe_w1d.py` (this checkout's CRLF copy of LF blobs; `git cat-file` shows no CR in the blobs) and the image-check harness test (no `sh` on PowerShell's PATH; skip-guarded like its neighbours in a12a7da; it passes under Git Bash). Stage 4 again over the stage-5 commits: `/security-review` over `f1c5a38..a12a7da` (Opus): no findings; gitleaks over all 51 commits: no leaks; semgrep with baseline f1c5a38: no findings. Opus verifier over `origin/v2..a12a7da`: no code defect; it asked for this record and the security review above, and for five stale test names in section 7 and section 8 (`ask-answer-view.test.tsx`, `answerSample.test.tsx`), corrected here; the hard constraints hold (no secrets or local paths added, no Dockerfile, lockfile, `fly.toml` or workflow change, only `dossier.py` and `ui_v2.py` under `src/semigraph`, master untouched, every web import resolves case-sensitively for Linux CI) |
| 7 Push, CI, owner deploy to `/v2/` | CI green; live checks | **Push and CI PASSED 2026-11-11**: origin/v2 at 875364f (52 commits, with 1110c60 and 887f2df); GitHub Actions `tests` run 42960939588 (unit, web, serve-shipped, neo4j-state, security-scan, deepeval-wrapper) and `image` run 42960939538 (containment) all success. **Deployed and live-checked 2026-11-11 (release v21)**: the owner ran `scripts.check_env_fly` (PASS), `flyctl deploy` (image `deployment-01M74FVYVA4CZXJ8GZZEND9YSW`, one machine, health check passing) and the deployed-mode image check (all checks PASS, 4 skipped); rollback target v20 `deployment-01M71ED0WT0J86WK92QPK48D0G`. Live `/v2/` is this build: its script, stylesheet and both font preloads have the same hashed names as the tested mirror build and the stylesheet is byte-identical; a font is served `font/woff2` with `nosniff` and the immutable cache header. Walkthrough: all 29 cached questions on the examples panel (X1 and T15 are not on it, as at v20) answered as "Saved answer, shown instantly." in 1.5-3.0 s, one POST each, no model call and no bot check touched; every chip of every answer (181) opened its evidence, focus went to Close and Escape returned it to the chip. All 13 company pages load; every chip key is unique; all 108 chips on NVDA and the first five on each other page open their evidence (NVDA had 162 at v20: the passage chips under each government rule were dropped by decision 001 item 2). No sideways scroll on the Ask page, the 13 company pages, Freshness, Method, Workspace and the not-found page at 390 and 320 px. No console error and no failed request from the app; the only console errors and 401s come from the Cloudflare bot-check widget's own scripts |
| 8 After deploy | NVDA pass (A11 per D-R7), first-visitor rerun and real people, owner click-through (B6) and written sign-off, `UI_V2` flip | **IN PROGRESS 2026-11-11 (v21)**: first-visitor rerun done (`docs/v2/M5B_PERSONA_WALKTHROUGH_RESULTS.md` 4b: grasp 2/3/2 at v20 -> 4/4/4, all three find the box on the first screen; remaining confusions listed there, the main one the chip label before its first open); the agent's NVDA pass attempted (`docs/v2/M5B_A11Y_MANUAL_PASS.md`, v21 section): speech was captured only on page load (it matches Ask-1, the Ask-3 landmarks and X-5) because NVDA does not hear the automation's input, so the other rows are keyboard and focus results (A1, A3, A4 and A5 stay open until someone listens; Co-7e not shown); a first attempt that touched the bot-check widget by mistake is recorded there and not counted); axe on the live pages and an open evidence panel, light and dark: 0 violations. **Owner's NVDA pass done 2026-11-11** (`docs/v2/M5B_A11Y_MANUAL_PASS.md`, owner section): 21 PASS, 5 NOTE, 3 FAIL, 2 NOT HEARD of 31 rows, findings N1-N5 (N1 a chip said "expanded" after the panel closed; N2 the Risk changes heading; N3 labels run into values; N4 two missing spaces and a doubled line; N5 raw ids read out) fixed test first, plus two of the same kind found by the Opus review of the fixes (the evidence panel's facts, the unresolved companies list); the owner waived the re-listen ("don't need to do it"), so A11 is owner-accepted with the fixes checked in Chrome, not re-heard (Ask-4c and Me-1 not heard). Owner, word for word: "fix first, then switch. (be careful. commit and push)", read as the written sign-off (B6, our reading of that message; the owner did not use the word) for the switch once the fixes are live; the chip-label question was not answered, so it defaults to ship and backlog (no change); the README paragraph was not approved, so none was added. **Release v22 (2026-11-11)**: the owner deployed the fixes at 28f9b7d (image `deployment-01M75FEY4C8ABDDVJ7TSVBWW8T`, CI green; image check in deployed mode all PASS, 4 skipped); the live `/v2/` serves the tested build (same hashed assets). Live checks: N1-N5 read from Chrome's accessibility tree all PASS (the chip collapsed at frame 1, focus back at frame 2, never on the body; "More options Depth: Standard"; "Freshness: Data as of ..." with no tooltip; "Open the source"; the evidence facts, the Key numbers row header and the six Freshness label rows each read with a space; heading and link both "Risk changes"); axe 0 violations on the six pages and an open evidence panel, light and dark; all 29 cached walkthrough questions answered as saved answers with every chip opening (X3 and X4 hit the free-question limit on the first run and passed on a retry after the window); 13 company pages, every chip key unique, no sideways scroll at 390 and 320 px, no app console error or failed request. Open: the owner's click-through before the switch (B6), the `UI_V2` flip (committed in `fly.toml`; checkov does not cover `fly.toml`, so no IaC scan applies) |
