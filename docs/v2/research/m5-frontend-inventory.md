# M5 research: frontend-inventory

Repo: `ai-semiconductor-risk-intelligence-graphrag`, branch `v2`. Read-only analysis, no repo edits, no servers, no paid
calls, no Neo4j connections, no `.env*` files read. All file:line references below are to the branch's working tree
as read on 2026-09-30.

Current UI: `src/semigraph/serve/static/index.html` (1,043 lines — the file has grown since the task's "~1,040 lines"
estimate), one inline `<style>` block, one inline `<script>` block, no external JS except Cloudflare Turnstile,
tested by `tests/ui/*.test.mjs` (`node --test`, 8 files) plus `tests/test_static_ui.py` and
`tests/test_serve_agent_ui.py`.

---

## 1. Feature-parity inventory (what an M5 rewrite must not lose)

### 1.1 Page shell and static copy

- `<head>`: title, viewport meta, one `<style>` block (index.html:7-50). No external stylesheet.
- Header (index.html:53-58): product name, one paragraph explaining the bitemporal-graph model and the
  verbatim-excerpt-vs-keyword-matched-rule distinction (pinned verbatim by
  `tests/test_static_ui.py::test_header_separates_verbatim_evidence_from_keyword_matched_rules`), a `#stats` line
  ("loading graph statistics…" placeholder) and a hidden `#freshnessLine` hint.
- Two-column responsive grid (`main`, index.html:15-16): single column under 900px, `340px + 1fr` above it. No JS
  media-query logic — pure CSS.
- Left card: "Benchmark questions (instant, cached)" — `#examples` (index.html:60-63).
- Center card: "Ask the graph" — textarea `#q` (maxlength 500, index.html:66), strategy `<select id="strategy">`
  with two static options `hybrid` / `vector` (index.html:68-71; `agent` is added/removed **only by script**, never
  present in markup — pinned by `test_serve_agent_ui.py::test_the_agent_option_is_created_by_script_never_present_in_the_markup`),
  `#ask` button, `#limits` hint span, a **withdrawn-accuracy notice** with two links to `docs/v2/M1B_PLAN.md` and
  `docs/v2/REVIEW_2026-09-26.md` on GitHub (index.html:75; the exact stale phrases this notice must never regress to
  — "95% correct", "text-verified removed", "dropped risk lineages", etc. — are enumerated and negatively asserted in
  `tests/test_static_ui.py::test_page_never_carries_the_withdrawn_claims`), hidden `#agentHint`, a Turnstile mount
  point `#turnstile-widget` (index.html:77), `#status` line, an agent step timeline `<div class="steps">` wrapping
  `<ol id="agentSteps">` (index.html:79), `#answer` (rendered markdown), hidden `#meta` (badges/warnings footer).
- Workspace card `#workspacePanel` (index.html:83-117), hidden until `/api/stats` reports `uploads_enabled`: see
  §1.7.
- Evidence drawer `<aside id="drawer">` (index.html:119-127): close button, title `#dTitle`, `#dMeta`, `#dFresh`,
  `#dFacts`, `<pre id="dText">`, `#dLink`.
- Footer (index.html:128-132): cost note, GitHub link, hidden `#tracingNote`, and a long **reliability note**
  (`#reliability`, index.html:131) giving hand-checked-sample accuracy for four classes of "what changed" claim
  (removed / not-matched / no-earlier-match / wording-not-found), each phrased with hedged, sample-based language —
  pinned character-for-character (all four fragments, "small", and "no % / no 'verified' outside one exempted
  phrase") by `tests/test_static_ui.py::test_page_states_how_reliable_change_claims_are_in_plain_words_with_the_measured_counts`.
  This note is placed **after** `<footer>` opens (also asserted) and must not itself claim more than the underlying
  2-annotator sample supports — a real trust-building UI requirement, not decoration.

### 1.2 Security posture (must be reproduced, not just "have some CSP")

- **CSP**, sent as a real response header on `GET /` (never a `<meta>` tag): `src/semigraph/serve/routes.py:34-39`.
  ```
  default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com;
  style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://challenges.cloudflare.com;
  img-src 'self' data:; base-uri 'none'; form-action 'none'
  ```
  plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Strict-Transport-Security: max-age=31536000; includeSubDomains` (routes.py:37-39). `connect-src 'self'` means
  **every** `fetch()` in the page must be a same-origin `/api/...` call — pinned by
  `test_serve_agent_ui.py::test_the_page_stays_inside_the_csp_one_script_no_new_origin`, which also asserts there is
  exactly one `<script>` tag and no `<script src>` in the markup (`test_static_ui.py::test_only_the_existing_external_script_and_inline_script_are_used`
  independently re-asserts this and enumerates the one allowed external script URL, the Turnstile loader).
- **`__TURNSTILE_SITE_KEY__` substitution**: the file ships with a literal placeholder token that `routes.py:120-123`
  (`GET /`) substitutes for `request.app.state.settings.turnstile_site_key` before serving. Pinned to occur exactly
  once (`test_static_ui.py::test_turnstile_placeholder_is_substituted_exactly_once`) and to actually happen on a real
  served page (`test_serve_agent_ui.py::test_the_served_page_carries_the_agent_containers_and_the_turnstile_substitution`).
  A rewrite that bakes the site key into a static build (no server template step) needs an equivalent mechanism —
  either a public env var baked at CF Pages build time, or a tiny `/config` endpoint.
- **Escaping discipline**: exactly one `esc()` helper (index.html:135-136, HTML-entity map) is used for every string
  that becomes `innerHTML`; every place the page renders **server-controlled free text** through `innerHTML` runs it
  through `esc()` first (citations' `chipLabel`, evidence drawer `metaHtml`/`factsHtml`, badge text, warnings, change
  report headlines/quotes). This is exhaustively fuzzed by the node tests (script tags, `onerror=`, quote-breakouts)
  in `badges.test.mjs`, `chips.test.mjs`, `checks.test.mjs`, `evidence.test.mjs`, `workspace.test.mjs`.
- **`textContent`-only rule for a narrower set of fields**: the agent step summary (arbitrary server text: tool
  names/counts/fiscal years chosen by an LLM-driven planner) is asserted to **never** reach `innerHTML`,
  `insertAdjacentHTML` or `outerHTML` anywhere in `stepText`/`addStep`/`clearSteps`/`syncAgentUi`/`agentEnabled`/
  `tracingNote` (`test_serve_agent_ui.py::test_server_text_of_a_step_never_reaches_innerhtml`, plus
  `agent.test.mjs`'s `Fake` element that **throws** if `innerHTML` is ever set during an agent test). The evidence
  drawer's excerpt text (`#dText`) and evidence title (`#dTitle`) are also assigned via `.textContent`
  (index.html:701, 700) rather than escaped-into-`innerHTML`, and `docChipLabel` is documented as deliberately
  returning **plain, unescaped** text for exactly this reason (index.html:443-450) — a caller building HTML (chip
  spans) still wraps it in `esc()` itself.
- **`safeUrl`** (index.html:372-373): a link from the API is rendered only if it matches `^https:\/\//i` — never
  `javascript:`, `data:`, or plain `http:`. Used for the SEC/Federal-Register "open the source" links and would be
  the same rule any `<a>`-based citation-source link needs.
- **Only `[id]` bracket forms are treated as citations** — see the ID grammar below; free-text brackets like
  `[Reported Metrics]` are left as plain escaped text, never turned into a link (chips.test.mjs).
- Nothing in the new (M3/M4) UI **moves**: no CSS `transition`/`animation`/`transform` on `.steps ol`/`.steps li`
  (`test_serve_agent_ui.py::test_nothing_in_the_new_ui_moves`) — a deliberate low-motion/vestibular-safety choice
  that should be an explicit design requirement for the Next.js step timeline, not an accident of not having built
  it yet.

### 1.3 Citation grammar and chip rendering

Four id forms, defined **once** in `src/semigraph/retrieval/ids.py:22-25` and mirrored **verbatim** as regex-literal
strings in the page (index.html:140-146); `tests/test_static_ui.py::test_page_citation_grammar_matches_retrieval_ids`
diffs the page's copy against the Python source of truth string-for-string, so any drift between a Next.js rewrite's
own citation regex and `ids.py` would be silently invisible without an equivalent contract test:

| Form | Pattern | Example | Meaning |
|---|---|---|---|
| `chunk` | `[0-9\-]+:[IVX]+\.[0-9A-Z]+:[0-9]{4}` | `0001045810-26-000021:I.1A:0361` | a filing passage |
| `xbrl` | `xbrl:[0-9]+:[a-z][a-z0-9_]*:[0-9]{4}-[0-9]{2}-[0-9]{2}` | `xbrl:1045810:revenue:2026-01-25` | a reported financial fact |
| `fr` | `fr:(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}` | `fr:2026-19537`, `fr:C1-2026-16628` | a Federal Register rule (external) |
| `doc` | `doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4}` | `doc:0123456789ab:v2:0007` | a chunk of a user's uploaded document |

- `classifyCitation(id)` (index.html:148-152), `chipLabel(id, evidence)` (index.html:155-168, plus `docChipLabel` for
  the `doc` form, index.html:445-450) turn a bare id into a human label, e.g. `Item 1A ¶0361`,
  `10-K FY2026 · Item 1A ¶0361` once evidence has loaded, `XBRL revenue FY ended 2026-01-25`, `BIS rule 2026-19537`,
  `your document v2 ¶0007` / `Q2 board memo.pdf · v2 ¶0007`.
- `renderMarkdown` (index.html:274-289) is a small hand-rolled subset (headings `#`-`####`, `- `/`* ` bullet lists,
  `**bold**`, paragraphs) that turns every `[id]` matching `CITE` into a `<span class="cite" data-id="…">label</span>`
  chip and escapes everything else; bracket text that is **not** one of the four id forms (including two ids inside
  one bracket, `[a, b]`) is left as plain escaped text, never a chip.
- Clicking a `.cite` chip anywhere on the page (both `#answer` and the workspace what-changed view `#wsChanges` share
  one delegated click handler, index.html:745-746) opens the evidence drawer (`openCitation`, index.html:724-743).
- **Stale citation marking**: once an answer's `done` event carries `workspace.stale_citations` (a `doc:`-id list),
  every matching `.cite` chip in the just-rendered answer gets `.stale` (a distinct CSS style, `.cite.stale`,
  index.html:49) and an appended tooltip "— a newer version exists" (`finish()`, index.html:689-696).

### 1.4 The ask flow and SSE consumption

- `ask()` (index.html:652-687): builds `{question, strategy, turnstile_token}` (plus `workspace_id`/`as_of` and an
  `X-Workspace-Token` header when a workspace is active), `POST`s `/api/ask`, and hand-parses the
  `text/event-stream` body itself (no EventSource — a POST body needs `fetch` + `ReadableStream`, so events are
  split on blank lines and `event:`/`data:` lines parsed by hand, index.html:668-685). Handles non-OK responses
  (403 gets an extra clause about the Turnstile check depending on whether a token was ever obtained,
  index.html:667) and resets Turnstile + reloads `/api/stats` after every ask, success or failure.
- Ctrl/Cmd+Enter submits from the textarea; Escape closes the drawer (index.html:748).
- Status line copy is fully data-driven, never hard-coded per strategy beyond the initial "the research agent is
  choosing lookups…" vs "retrieving from the graph…" split (index.html:656).
- **A workspace, once created, silently changes what "Ask" does — every path, including the benchmark examples.**
  `ask()` attaches `body.workspace_id`/`body.as_of` and the `X-Workspace-Token` header whenever `currentWorkspace`
  is non-null (index.html:659-663), unconditionally — clicking a "instant, cached" benchmark example
  (`askExample`, index.html:646-648) while a workspace is active still routes through `POST /api/ask` with
  `workspace_id` set. Server-side, any `workspace_id` skips the answer cache entirely (`routes.py:255-266`, "no
  cache on either side: an answer drawn from a private document must never be replayed to anyone else") and
  restricts `strategy` to `hybrid` only — `vector` or `agent` with a workspace id is a 400
  (`guard.py:83-98`, `WORKSPACE_STRATEGIES = ("hybrid",)`). So today, once a user opens a workspace, every
  "free, cached" example click becomes a **paid live call**, and switching to `vector`/`agent` becomes an error the
  UI does not pre-empt (it only reacts to the 400 after the fact). A rewrite should decide this on purpose —
  e.g. disable/relabel the benchmark panel and the non-hybrid strategy options while a workspace is open — rather
  than reproduce the current silent behavior change.

### 1.5 Answer rendering and the "checks" trust layer

This is the largest and most safety-critical piece of copy logic; a rewrite must reproduce the **exact severity
rules**, not just "show some badges":

- `metaHtml(ev, defaultModel)` (index.html:255-272) assembles, in order: a cached/usage-cost badge, an
  `answerBadge` (what actually answered — draft/escalated/routed/live, **never** claiming "verified" for a routed or
  unescalated answer: `answerBadge` never emits "draft verified" or "all citations verified" wording, pinned by
  `badges.test.mjs`), a truncation badge, then `checksSummary`'s badges + warnings + a fixed hint sentence.
- `checksSummary(ev)` (index.html:217-252) badge rules (all pinned in `badges.test.mjs`/`checks.test.mjs`):
  - Citations: **green** "cited ids were retrieved" only if there is ≥1 citation **and** none is hallucinated;
    zero citations is never green. A hallucinated/not-retrieved id is a **bad** badge naming the ids
    (`hallucinated` list). A zero-citation, non-refusal answer is a **warn** badge "no citation: this answer cites
    no source" with a matching warning line; a zero-citation **refusal** (`is_refusal: true`) is neutral (no
    badge color, no warning).
  - Numbers: **green** "numbers matched the retrieved context" only when `numbers_checked > 0` **and** nothing is
    unmatched or echoed. `numbers_checked === 0` (or absent, an older payload) is neutral text — "no figures to
    check" vs "numbers not checked" are **deliberately different strings** for "checked and found none" vs "an
    older payload that predates this check" (checks.test.mjs). An echoed figure (only the *question* stated it) is
    a **warn**, never a match; an unmatched one is **bad**.
  - Pseudo-citations (bracketed non-id text) become a warning line listing the bracket contents verbatim.
  - An unsupported removal claim is a **warn** badge plus a warning line quoting the offending sentence(s), with
    copy that names the "no longer appears" lists rather than saying "removed lists" (checks.test.mjs).
  - The hint sentence (shown under every answer) explicitly states the checks' actual scope: only `$`/€/USD/EUR/
    NT$/TWD-tagged amounts and percentages are checked, a bare number is not, and change-claim wording is only as
    reliable as the hand-checked sample described in the footer note (`checks.test.mjs`).
  - `checksPassed(c)` (index.html:190-194) is the **one** client-side predicate mirroring the server's
    `failed_check_names`/`checks_failed` (`src/semigraph/retrieval/verify.py:355-378`) — used only for local
    UI decisions today, but a natural single source of truth for a "release the draft or not" badge in a rewrite.
- `answerBadge` (index.html:197-214) distinguishes four mutually exclusive outcomes: routed straight to the strong
  model (change-over-time question, no cheap draft), escalated after a rejected draft (names the reasons in plain
  words via `reasonText`/`REASONS`, index.html:170-178), a cheap draft that passed (green-ish, no color class) or
  was released anyway with warnings (`cls: "warn"`), or a live stream with no escalation configured at all
  (falls back to "answered by `<defaultModel>` (streamed live)" using `/api/stats`'s `models.llm`).
- `renderMarkdown`/citation escaping as above; a `checksSummary` badge/warning list never claims more than the
  server proved (this exact non-overclaiming property, e.g. never printing "verified"/"draft verified"/"all
  citations verified" anywhere, is asserted across `badges.test.mjs` and `test_static_ui.py`'s stale-claims list).

### 1.6 Evidence drawer

- `evidenceView(id, d)` (index.html:403-438) branches on `classifyCitation`:
  - `xbrl`: "reported financial fact… not parsed by a model" note, a `<dl>` of Company/Metric/Value/Period/
    Disclosed-in, a "Open the source" link.
  - `fr`: "External event… not a statement by the company" note, Title/Published/Document number/Kind/Topics/
    "Kind flagged relevant" facts, the rule's abstract as body text, a federalregister.gov link.
  - `doc`: "\<title\> — uploaded by you, not a filing" note (title escaped), a freshness badge block reused from the
    filing-chunk case, the chunk text via `.textContent`.
  - `chunk` (default): filer/form/filing-date/section-title/mentions line, freshness badges, the risk item's
    headline(s) if the chunk sits inside a `RiskItem`, the excerpt text, an "Open the filing on sec.gov" link.
- `freshnessHtml(d)` (index.html:379-391) badges: `current` (ok/green), `superseded [by X]` (warn), `corrected [by
  X]` (bad/red, plus an explanatory line that the amending filing holds the current text), any other status string
  as a bare warn badge, `valid until <date>`, and `not returned by default search` when `retrievable === false`.
- `formatNumber` (index.html:291-295) locale-formats numeric values (`Intl.NumberFormat`, 4 fraction digits max);
  non-numeric strings pass through unchanged.
- Clicking a `chunk` chip whose evidence has since loaded **upgrades the chip's own label in place**
  (`showEvidence`, index.html:702-704) to include form/period, not just the drawer contents.

### 1.7 Upload workspace panel (M4)

Hidden entirely until `/api/stats.uploads_enabled` is true (index.html:626, gated server-side by
`routes.uploads_available`, `routes.py:181-188`, which ANDs the `UPLOADS_ENABLED` flag, the embedder's ability to
count tokens, and — in production — a configured Turnstile secret).

- **Create / restore** (`#wsCreate`, index.html:85-96): "Create a workspace" button, or paste an existing
  `workspace id` + `token` pair to restore one. Workspace state (`{id, token, expires_at}`) lives **only** in
  `sessionStorage` under key `semigraph:workspace` (index.html:781-787, 588) — never `localStorage`, never sent
  anywhere but back to its own API as `X-Workspace-Token`; wrapped in try/catch so a private-browsing block never
  crashes the page.
- **Active workspace** (`#wsActive`, index.html:97-116): id + expiry line, delete button, an "ask as of" date
  picker (`#wsAsOf`), an upload-target `<select id="wsTarget">` (populated from `GET /api/workspace/{ws}`'s document
  list: "Upload a new document" plus "New version of \<title\>" per existing document — `uploadTargetOptions`,
  index.html:513-520), a drag-and-drop zone (`#wsDrop`, accepts `.pdf .docx .md .markdown .txt .html .htm`, up to 30
  pages), a job-progress list (`#wsJobs`), the document list (`#wsDocs`), and the what-changed view (`#wsChanges`).
- **Per-document "ask as of vN"** (`versionAskAsOfOptions`, index.html:531-535): one option per version, keyed by
  that version's own `created_at` instant (never a hand-typed date) so a same-day v1→v2 upload is still
  disambiguated; **at most one** document's selector may be non-"current" at a time — picking one clears every
  other document's selector (`reapplyAskAsOf`, index.html:848-859, and `renderDocuments`'s change listener,
  index.html:869-876) and the manual date picker is a fallback only when no per-version instant is active
  (`wsAsOfValue`, index.html:595).
- **Version timeline**: `versionTimeline` (index.html:476-478) renders e.g. `v1 (superseded) → v2 (current)`;
  `docHtml` (index.html:820-828) shows the timeline plus per-version buttons that open the what-changed view
  against the previous version.
- **Upload flow** (`uploadFile`/`handleFiles`, index.html:951-992): one file at a time, **fully sequential** — the
  next file is not even POSTed until the previous file's job reaches a terminal state (ready/failed), because the
  server holds a single `upload_slots` permit; each file also gets its **own fresh** Turnstile token
  (`waitForFreshTurnstileToken`, index.html:762-773, polling every 100ms up to a 15s timeout) rather than reusing
  one that `resetTurnstile()` just nulled — sending an empty token would be a silent 403 in production. Title
  defaults to the file name truncated to the server's own 120-char cap (`uploadTitleFor`, index.html:524-526,
  `MAX_UPLOAD_TITLE_CHARS = 120`).
- **Job progress** (`watchJob`/`streamJobOnce`, index.html:892-949): one `<div class="jobrow">` per file, live SSE
  text via `jobStateText` (index.html:462-473: `received→queued`, `validating→checking`, `parsing→reading the
  document`, `chunking→splitting into sections`, `embedding→embedding N/M (~Ss left)`,
  `comparing→comparing with the previous version`, `indexing→saving`, `ready`, `failed: <fixed message>`).
  Reconnects up to `WATCH_RECONNECTS = 3` times (3s apart) on a dropped stream or 5xx/429 before giving up and
  showing "connection lost while watching this upload"; a 404 (job/workspace gone) stops immediately.
- **What-changed view** (`changesHtml`/`changeItemHtml`, index.html:539-578): Added / Removed / Changed sections
  (each item shows its headline plus, for `changed` items, the actual quoted before/after passages with their own
  `doc:` citation chips — never a bare link), a separate **Minor rewordings** section for units the aligner touched
  but that produced no surviving sentence-level passage (never silently folded into the unchanged count), an
  unchanged-section count, and an optional note when some sections were compared only as whole sections rather
  than sentence-by-sentence for negation (`negation_check_skipped`).
- **Evidence cache invalidation** (index.html:707-743, `dropDocEvidence`/`docEvidenceGen`/`openSeq`): every cached
  `doc:` evidence entry is dropped the moment a job reaches a terminal state (a new version supersedes the
  previous one's chunk status) or the watcher gives up/finds the job gone, and on any `setWorkspace` (switch/
  delete/create/restore) the **whole** cache is cleared. An in-flight `doc:` evidence fetch that straddles one of
  these drops is still shown to the user but is **not** written back into the cache afterward. Two citation chips
  clicked in quick succession show whichever chip was **clicked last**, regardless of which fetch resolves first:
  each `openCitation` call captures its own `seq = ++openSeq` (index.html:725), and a result is only rendered when
  `seq === openSeq` still holds (index.html:739) — so an earlier click whose fetch happens to return *after* a
  later click's fetch is silently discarded from the drawer (it is still cached for a future click). Getting this
  backwards — showing whichever fetch finishes last — is exactly the race a naive React `useEffect`/`useState`
  rewrite tends to introduce; a rewrite needs an equivalent per-open sequence token (or `AbortController` +
  "ignore if a newer request started").
- **Freshness header line** (`freshnessLine`, index.html:498-509): `"Data as of <snapshot_as_of> · checked <relative
  time> · N filing(s) pending"`, or `"freshness check failed <relative time>"` (preferring `last_error_at` over a
  possibly-null `checked_at` — the very first check a monitor ever makes can fail with no prior good check), or
  `"freshness check unavailable"` for `disabled`/`unconfigured`/`never`/no-monitor states alike.

### 1.8 Opt-in "Deep research (agent)" strategy (M3)

- The `<option value="agent">` is added to `#strategy` **only** when `/api/stats.agent_enabled === true`
  (`syncAgentUi`, index.html:607-616) — never present in the served markup regardless of flag state (pinned,
  `test_serve_agent_ui.py`). If the flag flips off while `agent` is selected, the select is reset to `hybrid` and
  the option removed.
- `AGENT_HINT` (index.html:346) is shown **only** while `agent` is the actively selected strategy (not just "the
  flag is on") — fixed live, on the `#strategy` `change` event, not only after the next `/api/stats` poll
  (`FIX 3`, agent.test.mjs) — so the hint never describes the agent while a hybrid answer is on screen.
- `TRACING_NOTE` (index.html:347) is shown only when `stats.agent_enabled && stats.tracing === true`, and its exact
  wording is pinned to never overclaim what a Langfuse trace holds: length + one-way hash of the question, tool/
  model names, token counts, cost, timings, fallback reason — **never** the question or answer text, **never** the
  asker's address, and "sampling rate" rather than "small sample" (true at any configured rate).
- Step timeline (`STEP_VERBS`, index.html:339-343; `stepText`, index.html:352-360; `addStep`/`clearSteps`,
  index.html:597-602): one `<li>` per `step` SSE event, text built from a fixed verb-per-tool map
  (`lookup_company→"Looked up …"`, `search_filings→"Searched the filings: …"`, `financial_metrics→"Fetched …"`,
  `risk_changes→"Compared risk disclosures: …"`, `relationships→"Checked relationships: …"`,
  `active_risks→"Listed active risks: …"`, `compute_change→"Computed …"`), an unknown/malformed tool name falls
  back to `"Ran <tool words>"` or plain `"Ran a tool"`, a failed step appends `" (failed)"`, the summary is
  clamped to `MAX_STEP_CHARS = 140` chars with an ellipsis, and the whole list is capped at `MAX_STEPS_SHOWN = 12`
  items regardless of how many the server streams. The `<ol>` is a polite live region
  (`aria-live="polite" aria-relevant="additions"`, `aria-label="What the research agent did"`).
- Retrieval status line (`retrievalStatus`, index.html:362-369) turns the `retrieval` event's `anchors`/`counts`
  into `"anchors: <names or 'no company detected; defaulting to Nvidia'> · N relationships · N XBRL metrics · N
  active risk disclosures · N risk-change items · N excerpts — generating…"`.
- Escalation status line (`escalationStatus`, index.html:182-185): `"the first draft did not pass the automatic
  checks (<reasons>); answering again with the stronger model…"`.
- Benchmark examples always force `strategy = "hybrid"` before asking (`askExample`, index.html:646-648) — a saved
  example must never be re-asked live as the (paid, slower) agent — and re-syncs the agent hint immediately, not
  only after the example's own answer lands.

### 1.9 Accessibility affordances — present, and gaps the M5 Lighthouse a11y ≥ 95 gate must not inherit

**Present:**
- The agent step timeline is a real live region: `aria-live="polite" aria-relevant="additions"` on the wrapping
  `<div class="steps">`, `aria-label="What the research agent did"` on the `<ol id="agentSteps">`
  (index.html:79, pinned by `test_serve_agent_ui.py::test_the_timeline_is_a_polite_live_region_that_announces_additions`).
- Escape closes the evidence drawer; Ctrl/Cmd+Enter submits a question from the textarea (index.html:748).
- No motion anywhere in the M3/M4 UI (`test_nothing_in_the_new_ui_moves`) — a deliberate vestibular-safety property.
- Dark mode via `prefers-color-scheme` with a separate color palette (index.html:8-9).

**Gaps a rewrite must not copy (none of these are tested or asserted anywhere today):**
- The evidence drawer (`<aside id="drawer" aria-hidden="true">`, index.html:119) never has its `aria-hidden` flipped
  to `false` when opened, nor back to `true` when closed (`openCitation`/`drawerClose` handler,
  index.html:724-747) — assistive tech is told the drawer is always hidden even while it is visibly open and
  focusable. Worse, the drawer is hidden only by `transform:translateX(100%)` (index.html:33), not `display:none`
  or `hidden` — its close button and content stay in the DOM and in the natural tab order even while closed and
  scrolled offscreen, so a keyboard user tabbing through the closed page lands on an invisible close button before
  ever reaching visible content below it.
- Citation chips are plain `<span class="cite" data-id="…">` (created via `renderMarkdown`, index.html:277, and
  `changeItemHtml`, index.html:548) with no `tabindex`, no `role="button"`, and no keyboard handler — only a mouse
  click opens the drawer (`$("answer").addEventListener("click", …)`, index.html:745). A keyboard-only user cannot
  reach a single citation in the answer or the what-changed view.
- No focus management around the drawer: opening it does not move focus into it (e.g. to the close button or a
  heading), and closing it does not return focus to the chip that opened it — the drawer's own close button is an
  unlabelled "×" (`title="close"` only, index.html:120), not an `aria-label`.
- `#q` (the question textarea) and the workspace-restore inputs `#wsRestoreId`/`#wsRestoreToken` have only a
  `placeholder`, no associated `<label>` (index.html:66, 91-92) — placeholder text disappears once typing starts
  and is not a substitute for a label for screen readers.
- The file input `#wsFile` is `style="display:none"` (index.html:112), which removes it from the tab order
  entirely, and its sibling `<label for="wsFile">` drop zone (index.html:110-111) is a plain `<label>` with no
  `tabindex` and no button role — a `<label>` is not natively focusable. There is **no keyboard path to upload a
  file at all**: only a mouse click on the drop zone, or an actual drag-and-drop, reaches the file picker
  (drag/drop handlers only, index.html:1030-1033). A rewrite needs a real, focusable, keyboard-activatable trigger
  (a `<button>` that calls `input.click()`, or `tabindex="0"` plus a keydown handler on the drop zone) alongside
  drag-and-drop.
- `#status` and `#answer` are ordinary `<div>`s with no `aria-live` — a screen-reader user gets no announcement when
  the status line changes ("retrieving…" → "done") or when the answer text streams in, unlike the agent step
  timeline which does announce.
- Given the M5 gate is Lighthouse a11y ≥ 95, all of the above (drawer `aria-hidden`, focus trapping/return, chip
  keyboard access, form labels, live regions on `#status`/`#answer`) are concrete, verifiable fixes the Next.js
  rewrite should budget for — they are gaps in the *current* page, not features to preserve.

---

## 2. API calls and SSE events the page consumes (cross-checked against the server routes)

All routes live in `src/semigraph/serve/routes.py` (core + `/api/ask`), `dossier_routes.py` (dossier/risk-changes),
`monitor_routes.py` (freshness), and `workspace_routes.py` (uploads); registered in `main.py:264-270`.

| Page call | Method + path | Server handler | Response shape actually used |
|---|---|---|---|
| `loadStats()` | `GET /api/stats` | `routes.py:163-178` | full server shape: `{graph:{nodes:{Company,Filing,EvidenceSpan,RiskFactor,ExportControl,...}, relationships, risk_items, removed_risk_items, removed_paragraphs}, snapshot, ledger:{today:{paid}}, paused, limits:{max_queries_per_day, per_ip, max_question_chars}, models:{llm, escalation, embedder}, agent_enabled, tracing, uploads_enabled, freshness:{status, checked_at, pending_count}}` (`graph_stats`, `main.py:111-136`). The page's `statsHtml`/`syncAgentUi` read only `graph.nodes.*`, `graph.removed_risk_items`/`removed_paragraphs`, `ledger.today.paid`, `limits.*`, `paused`, `agent_enabled`, `tracing`, `uploads_enabled` — `snapshot`, `graph.relationships` and `graph.risk_items` are returned but **never displayed anywhere** on the current page, a candidate data source for an M5 "coverage strip"/Method page that needs no new backend work. |
| `loadExamples()` | `GET /api/examples` | `routes.py:137-148` | `{source, examples:[{id, type, question}]}` — **only** bootstrap-accepted, still-cached examples are listed |
| `loadFreshness()` | `GET /api/freshness` | `monitor_routes.py:44-53` | `{configured, enabled, status, checked_at, snapshot_as_of, last_error_at, next_check_at, pending_count, pending_filings, federal_register, unresolved, duration_s}` (page reads only `status`/`checked_at`/`snapshot_as_of`/`pending_count`/`last_error_at` today) |
| `ask()` | `POST /api/ask` | `routes.py:233-277` → `_paid_stream` (routes.py:347-411) | SSE (see below) |
| `openCitation()` (public) | `GET /api/evidence/{id}` | `routes.py:200-217` | `{type: "chunk"\|"xbrl"\|"fr", ...}`; 400 malformed id, 404 unknown/not-public (a `doc:` id is always 404 here — `_EVIDENCE` dict has no `"doc"` key, routes.py:87-93). Exact per-kind fields, from the Cypher `RETURN` clauses: **chunk** (`EVIDENCE_QUERY`, routes.py:54-67) → `chunk_id, text, source_url, section_key, section_title, accession_no, form, filing_date, filer, status, is_current, retrievable, valid_to, superseded_by, corrected_by, mentions, item_headlines` — note **no `fiscal_year` and no `headline` field**, see the dead-branch note below. **xbrl** (`XBRL_EVIDENCE_QUERY`, routes.py:71-78) → `metric_id, metric, concept, value, unit, period_start, period_end, company, cik, accession_no, form, filing_date, source_url`. **fr** (`FR_EVIDENCE_QUERY`, routes.py:81-83) → `document_number, title, publication_date, url, kind, topics, relevant, abstract`, plus static `source: "federal_register", external: true, note: "…"` merged in by `routes.py:87-93`. |
| `openCitation()` (workspace) | `GET /api/workspace/{ws}/evidence/{doc_id}` | `workspace_routes.py:478-488` | `{id, text, document_id, version, is_current, status, valid_to, superseded_by_version, title}` (repo.py `EVIDENCE_QUERY`) |
| `createWorkspace()` | `POST /api/workspace` `{turnstile_token}` | `workspace_routes.py:139-149` | 201 `{workspace_id, token, expires_at, limits}` |
| `restoreWorkspace()`/`loadWorkspaceData()` | `GET /api/workspace/{ws}` (header `X-Workspace-Token`) | `workspace_routes.py:155-163` | `{workspace_id, expires_at, documents:[{document_id, title, latest_version, versions:[{version, created_at, pages, chars, is_current, status, items_compared, not_compared_reason, suspicious}]}], usage:{documents, pages, embedded_tokens}}` |
| `deleteWorkspace()` | `DELETE /api/workspace/{ws}` | `workspace_routes.py:166-172` | 204 |
| `uploadFile()` | `POST /api/workspace/{ws}/documents` (multipart `file`,`title`,`document_id?`; header `X-Turnstile-Token`) | `workspace_routes.py:361-386` | 202 `{job_id, document_id, version}`, or `{unchanged: true, document_id, version}`, or an error `{detail, code}` (413/415/422/429/503) |
| `watchJob()`/`streamJobOnce()` | `GET /api/workspace/{ws}/jobs/{job_id}` | `workspace_routes.py:437-453` | SSE `event: job`, `data: {job_id, state, document_id, version, progress?, error?, chunks?, units?, items_compared?, not_compared_reason?, suspicious?}` |
| `showChanges()` | `GET /api/workspace/{ws}/changes?document_id=&from=&to=` | `workspace_routes.py:463-472` | `{items_compared, not_compared_reason?, added, removed, changed, minor_rewordings, unchanged_count, negation_check_skipped}` (identical shape whether "first version" or a real diff — `uploads/jobs.py:373-380`) |

**Not called by the current page at all** (exist server-side, unused by the UI — directly relevant to §4):

- `GET /api/company/{ticker}/dossier` and `GET /api/company/{ticker}/risk-changes` (`dossier_routes.py:47-80`) — the
  M4 dossier/what-changed-across-filings data the M5 plan's "dossier + what-changed" screen needs. Response shapes:
  - `dossier`: `{company:{ticker,name,cik}, data_as_of, filings:[{accession_no, form, filing_date, is_current, status, superseded_by}], metrics:[...METRICS_QUERY rows...], active_risks:[{risk_id, summary, category, last_evidenced_at}], edges:[...company_edges_query rows...], rules:[...RULE_EDGES_QUERY rows...]}`
  - `risk-changes`: `{company, pairs:[{older:{accession_no,form,filing_date}, newer:{...}, compared, not_compared_reason, items:[{kind: dropped|new|changed|unsettled, headline, older_item_id, newer_item_id, passages:[{quote, chunk_id, side}]}]}]}`
- `POST /api/admin/policy` / `GET /api/admin/policy` (`routes.py:421-435`) and `POST /api/admin/freshness/check`
  (`monitor_routes.py:56-67`) — `X-Admin-Token`-gated (404 without a valid token, `routes.py:414-418`); no admin UI
  consumes these today.

### `/api/ask` SSE event grammar (the contract a Next.js/AI-SDK stream encoder must reproduce)

Defined by `answer_stream` (`src/semigraph/retrieval/answerer.py:593-624`, actual event construction in
`stream_answer_for_context`/`stream_answer_for_prompt`/`_live_events`/`_buffered_events`/`_draft_then_escalate`,
answerer.py:470-683), plus agent-only and workspace-only additions:

1. `{"event": "retrieval", "anchors": {...}, "counts": {edges, metrics, risks, temporal, chunks}, "anchor_defaulted": bool}` — always first for the fixed/hybrid/vector path (answerer.py:636-638). A **workspace** ask adds `"doc_chunks": N` to this same event (`retrieval/workspace.py:186-188`). An **agent** ask emits zero or more `step` events *before* this one.
2. *(agent only)* `{"event": "step", "n", "tool", "args", "summary", "ok"}` — one per planner tool call (`agent/stream.py:132-136`).
3. *(only when an escalation model is configured and the cheap draft fails verification)* `{"event": "escalated", "reasons": [...], "from": "<draft model>", "to": "<escalation model>"}` (answerer.py:583).
4. `{"event": "delta", "text": "<chunk>"}` — zero or more; for a *buffered* release (workspace answers, or a draft that must be edited before release) exactly **one** `delta` carries the whole text (answerer.py:532, 578).
5. Terminal `{"event": "done", "question", "strategy", "answer", "citations": [...], "hallucinated": [...], "checks": {citations_retrieved, numbers_grounded, numbers_checked, unmatched_numbers, echoed_numbers, pseudo_citations, has_citation, is_refusal, unsupported_removal_claim, unsupported_removal_sentences}, "finish_reason", "usage": {prompt_tokens, completion_tokens, estimated?}, "cost_usd", "chunk_ids", "context_chars", cached?, escalated?, escalation_reasons?, answered_by?, routed?("cheap"|"strong")}` (answerer.py:470-484).
6. Or terminal `{"event": "error", "detail": "<user-safe message>", "partial"?, "usage"?, "cost_usd"?, "strategy"?}` — the client-facing `detail` is always a fixed, generic string server-side (never a raw exception), redacted of anything secret-shaped (`routes.py:386-392`).
7. `POST /api/ask` itself can also fail before any SSE stream starts: 400 (bad question/strategy/as_of/workspace id), 403 (Turnstile), 404 (bad workspace/token — same message as "not found" for either), 429 (rate limit / daily budget / busy), 503 (paused / uploads unavailable) — all plain JSON `{detail}` bodies (routes.py:233-277).

**The cached-hit `done` shape is narrower than the live one, and this matters for a rewrite's type**: a cache hit
(`routes.py:261-266`) skips retrieval, deltas, and the writer entirely and returns exactly
`{event: "done", cached: true, question, strategy, answer, citations, hallucinated, source, created_at}` — the
fields `get_answer` actually selects (`serve/store.py:139-145`). There is **no `checks`, no `usage`, no
`cost_usd`, no `finish_reason`, no `agent`/`workspace` block** on a cached answer (the store never persists them).
`metaHtml` branches on `ev.cached` first (index.html:259) for the usage/cost badge specifically, but it still runs
`checksSummary(ev)` unconditionally afterward (index.html:268-270) — with `ev.checks` absent, that function's `!c`
branch fires and produces **"numbers not checked"** (never "no figures to check", a deliberately different string
for "an older/cache-only payload that predates this check" — checks.test.mjs) plus, from the persisted
`citations`/`hallucinated` arrays alone, an ordinary "N citation(s)" badge and (when `hallucinated` is empty, which
it always is for a cached answer since only checks-passing answers are ever cached, `routes.py:376-377`) a green
"cited ids were retrieved" badge — `badges.test.mjs`'s "answers without a checks object (cached, older) do not
claim their numbers were checked" test pins exactly this behavior. So a cached answer **does** show a checks
summary; what it never shows is the per-field grounding/pseudo-citation/removal-claim badges that need a real
`checks` object, and never the usage/cost badge. A rewrite's discriminated union for the `done` event needs
`cached: true` to imply a strictly smaller field set on `done` itself, while the checks-summary UI component should
still run against `checks: undefined` gracefully rather than being skipped outright for a cached answer.

An **agent** ask's `done` additionally carries `"agent": {tool_calls, model_calls, elapsed_s, fallback_reason,
planner_model, planner_usage, planner_cost_usd, planner_prompt_version, stop_reason}` with the planner's dollar
cost already folded into `cost_usd` (`agent/stream.py:43-62`). A **workspace** ask's `done` additionally carries
`"workspace": {id_hash, doc_chunks, stale_citations, suspicious}` (`retrieval/workspace.py:196-201`) and, per the
gate order in `routes.py:255-266`, is **never** a cache hit — a workspace answer always runs the writer live.

**Two dead client-side branches worth flagging for a rewrite** (the server never sends the field the branch reads,
confirmed against the exact `RETURN` clauses in §2's evidence-route row above): `chipLabel`'s
`ev.fiscal_year ? "FY"+ev.fiscal_year : ...` branch (index.html:159-161) never fires — no route anywhere in
`src/semigraph/serve` or `src/semigraph/retrieval` ever sets a `fiscal_year` key on an evidence or citation
payload (confirmed by grep); it always falls through to the `filing_date` branch. Likewise `evidenceView`'s
`factsHtml([["Risk item", [d.headline, ...list(d.item_headlines)]...`  (index.html:433) reads `d.headline`, but
`EVIDENCE_QUERY` (routes.py:54-67) returns only `item_headlines`, never a bare `headline` key — so `d.headline` is
always `undefined` for a real `/api/evidence` response and the "Risk item" fact line is effectively driven by
`item_headlines` alone. Neither is a bug today (both degrade harmlessly), but a rewrite copying this logic
verbatim should not assume `fiscal_year`/bare `headline` are live server fields; note that `evidence.test.mjs`'s
"a risk item headline, when present, is shown" test happens to pin exactly this dead `headline` field (it passes
`headline` directly in its test payload, which the real route never sends) — a ported test should key its fixture
on `item_headlines` instead, to actually exercise what the server sends.

**The AI SDK integration PLAN.md names is not built yet, and is real new work, not a frontend-only swap.** M5's own
text says "Vercel AI SDK" and M3's scope line mentions "AI SDK stream encoder + contract test"
(`docs/v2/PLAN.md:99`); `research-scale-frontend.md` §B recommends `useChat` against the AI SDK's own
`x-vercel-ai-ui-message-stream: v1` protocol (`text-*`, `tool-input/output-*`, `source-document`, `data-*`,
`start-step`/`finish-step` parts). A repo-wide search for `ui-message-stream`, `x-vercel-ai`, `start-step` and
`source-document` returns **nothing** — the server today speaks only the custom SSE grammar above
(`retrieval`/`step`/`escalated`/`delta`/`done`/`error`, a `{question, strategy, turnstile_token, workspace_id?,
as_of?}` POST body), which is a different shape than `useChat`'s message-array request body and UI-message-stream
response. Reproducing the M3 plan line means either (a) writing the encoder (an async generator translating the
existing event grammar into AI-SDK parts, roughly the "~100 lines" the research doc estimates) as new backend
work, or (b) keeping today's grammar and building a custom fetch/stream transport on the frontend instead of
`useChat`'s default transport — a real architecture decision for M5, not implied by "use the AI SDK."

**Moving the frontend to Cloudflare Pages (a separate origin from the Fly-hosted API) breaks two same-origin
assumptions the current page relies on.** The CSP's `connect-src 'self'` (routes.py:35) and the test that every
`fetch()` call starts with `"/api/`  (`test_serve_agent_ui.py::test_the_page_stays_inside_the_csp_one_script_no_new_origin`)
both assume the page and the API are served from the same origin — true today (`GET /` on the Fly app serves the
page itself, `routes.py:119-123`). A static Next.js export on Cloudflare Pages calling a separate Fly API origin
needs: a relaxed/rewritten CSP (`connect-src` naming the API origin, or a Cloudflare-side proxy/rewrite that keeps
it same-origin), CORS headers on the API for the custom `X-Workspace-Token`/`X-Turnstile-Token` headers this page
sends (none of FastAPI's routes add `CORSMiddleware` today — `grep -rn CORSMiddleware src` is empty), and
`guard.client_ip`'s trusted-header name changing from Fly's `fly-client-ip` to Cloudflare's `CF-Connecting-IP`
(`config.py:70` documents the header as configurable but defaults to none; `research-buyer-demo.md` §5 already
flags `CF-Connecting-IP` for the edge layer, consistent with this).

### Upload job SSE grammar (`GET /api/workspace/{ws}/jobs/{job_id}`)

`event: job`, one JSON object per state transition (`uploads/jobs.py:60`, states `received → validating → parsing →
chunking → embedding → comparing → indexing → ready|failed`); `embedding` carries `progress: {done, total, eta_s}`;
`failed` carries `error: {code, message}` from a **fixed** message table (`uploads/jobs.py:63-80`, never a raw
exception). At most 3 live watchers per job (`MAX_LIVE_WATCHERS_PER_JOB`, `uploads/jobs.py:41`); a 4th gets 429. A
finished job's log survives 60s (`REGISTRY_GRACE_PERIOD_S`) for a reconnecting watcher; after that, `GET` replays
the persisted final state once instead of streaming.

---

## 3. What the UI tests pin (portable to a rewrite's own test suite)

- **`tests/ui/harness.mjs`**: loads the page's inline `<script>` into a Node `vm` context with a stub DOM (no
  jsdom/npm deps) and exposes 39 named functions from the page's top-level scope (the `EXPORTS` list,
  harness.mjs:13-26) for direct unit testing. Most are genuinely pure (`esc`, `chipLabel`, `checksSummary`,
  `evidenceView`, `changesHtml`, …), but a handful are **stateful** and exposed anyway because `agent.test.mjs`
  needs to drive them against a hand-built fake DOM: `syncAgentUi`/`addStep`/`clearSteps` mutate page-global `let`s
  (`stepsShown`, `lastAgentStats`) and the real `#agentSteps`/`#agentHint`/`#strategy` elements, `askExample`
  mutates `#q`/`#strategy` and calls `ask()`, and `dropDocEvidence` mutates a `Map` passed in by reference. The
  real contract this file demonstrates is narrower and more useful than "everything here is pure": *label/status/
  badge-text builders and payload-shaping functions are pure and DOM-free*, while *DOM-mutating orchestration
  functions are still unit-testable given a minimal fake element*. A rewrite that moves the pure half into React
  components should keep it as an independently testable module (not inline JSX), and should test the stateful
  half (state-sync effects, upload sequencing) the way `agent.test.mjs`/`upload_flow.test.mjs` do — against a
  fake/mock environment, not full DOM rendering.
- **`badges.test.mjs`** / **`checks.test.mjs`**: the full trust-badge state machine in §1.5 — every "never says
  X" negative assertion (`draft verified`, `all citations verified`, a green numbers badge with `numbers_checked:
  0`, a green citations badge with zero citations) is a regression test a rewrite must keep passing in spirit even
  if the exact strings change, because they encode a **product commitment** (never overclaim verification), not
  incidental copy.
- **`chips.test.mjs`**: the citation-id grammar (4 forms) and `chipLabel`/`renderMarkdown` chip rendering,
  including "two ids in one bracket is not a citation" and full XSS-escaping of model-generated markdown.
- **`copy.test.mjs`**: `/api/stats`-derived copy (`statsHtml`, `limitsText`, `costNote`, `retrievalStatus`) —
  pins that `removed_risk_items` is labelled "risk factors that no longer stand alone (text check)" (never
  "removed"/"verified"), that a legacy field name (`deleted_risk_lineages`) must never leak into the UI, that a
  zero count still renders (not treated as "missing"), and that model names/cost notes derive from `/api/stats`
  rather than being hard-coded to one provider.
- **`evidence.test.mjs`**: all four evidence-drawer shapes (current/superseded/corrected chunk, xbrl, fr, doc),
  `safeUrl`'s https-only rule, `formatNumber`, and exhaustive per-field XSS escaping of every server-supplied
  string across all three evidence shapes.
- **`agent.test.mjs`**: the entire opt-in-agent UI state machine in §1.8, using a hand-rolled `Fake` DOM element
  whose `innerHTML` setter **throws** — the strongest possible test that agent-step text can never become markup.
- **`workspace.test.mjs`**: `docChipLabel`, `staleClass`, `jobStateText`, `versionTimeline`, `relativeTime`,
  `freshnessLine` (including the ordering-bug-fix cases: error status preferring `last_error_at`, "stale" ignoring
  `last_error_at`), `uploadTargetOptions`/`uploadTitleFor`, `versionAskAsOfOptions`, `changeItemHtml`/`changesHtml`
  (including the "minor rewordings never folds into unchanged_count" and "negation_check_skipped" note rules).
- **`upload_flow.test.mjs`**: the two stateful flows the pure-function harness cannot reach — runs the page's
  **real** inline script in its own `vm` context with fake `fetch`/`FormData`/DOM (not the pure-function subset).
  Pins: (a) the C3 "ask as of vN" instant resets on workspace switch, re-applies to whichever selector still offers
  it, and forces every other selector back to "current"; (b) R3/finding-15 multi-file-drop sequencing — the next
  file is never POSTed until the previous job reaches a terminal state, and never with a stale/empty Turnstile
  token; (c) job-stream reconnect behavior (429/5xx retried, a dropped stream retried up to `WATCH_RECONNECTS`,
  404 stops at once); (d) the evidence-cache invalidation races in §1.7 (drop-on-terminal, drop-on-gone,
  in-flight-fetch-not-recached, last-click-wins, whole-cache-clear-on-workspace-switch).
- **`tests/test_static_ui.py`** (Python, structural — what the JS unit tests cannot see): runs `node --test` over
  all 8 files as a pytest gate; independently re-asserts the withdrawn-claims list, the header's verbatim/keyword
  distinction, the reliability-note counts, the placeholder-question wording, the vector-baseline label, "exactly
  one `<script>` tag, no external script but Turnstile," the Turnstile-placeholder substitution count, and diffs
  the page's 4 citation regexes against `retrieval/ids.py` byte-for-byte.
- **`tests/test_serve_agent_ui.py`** (Python, structural): the agent `<option>` is markup-absent until script adds
  it; `agentHint`/`tracingNote`/`agentSteps` all ship `hidden` in markup; the step timeline is a proper
  `aria-live="polite" aria-relevant="additions"` region with the right `aria-label`; no agent-related function body
  ever contains `innerHTML`/`insertAdjacentHTML`/`outerHTML`; the SSE dispatcher routes `step` events to
  `addStep`; benchmark-example clicks force `hybrid` and resync the hint; nothing in the new UI has CSS motion;
  the page never leaves `connect-src 'self'` (every `fetch()` call site is asserted to start with `"/api/`);
  the agent hint/tracing-note copy never says "verified" and matches the exact required substrings; and a real
  `TestClient` round-trip confirms the served page actually substitutes the site key and carries the agent
  container ids.

---

## 4. M5 frontend/buyer items: existing backend vs. new backend work

Cross-referencing `docs/v2/PLAN.md` §6 (M5 scope) and §7 (buyer demo) against the actual serve-layer code:

### Already have real backend support for M5

With two named exceptions the rows below call out explicitly: the Answer screen still needs an AI-SDK stream
encoder or a custom transport (§2), and the Eval/Limits page has only static prose in `index.html` behind it today,
no live eval-results endpoint.

| M5/buyer item | Backend today |
|---|---|
| Answer with step timeline, `[n]`/citation chips, freshness badges | The retrieval/verification/streaming logic is fully shipped: `/api/ask` SSE (`step`/`retrieval`/`delta`/`done`), `/api/evidence/{id}`, per-chunk freshness fields (§2). **But** M5 names the Vercel AI SDK (`useChat`) as the frontend's stream client, and the server speaks a custom SSE grammar, not the AI SDK's `ui-message-stream` protocol (repo-wide search for `ui-message-stream`/`x-vercel-ai`/`start-step`/`source-document` finds nothing) — so this item is **not** frontend-only under the AI-SDK plan; it needs either a new stream-encoder route or a custom (non-`useChat`) transport on the frontend. See §2's SSE-grammar section for the full analysis. |
| "N risks dropped, not supported" badge | `checks.unsupported_removal_claim` + `unsupported_removal_sentences` on every `done` event (`retrieval/verify.py:326-354`); today shown as a warning line, not yet a dedicated count badge, but the data exists. |
| Company dossier | `GET /api/company/{ticker}/dossier` (`dossier_routes.py:47-62`) — **built, cached, read-rate-limited, not called by any UI today.** |
| Risk-change delta / what-changed | `GET /api/company/{ticker}/risk-changes` (dossier_routes.py:65-80, cross-filing) **and** `GET /api/workspace/{ws}/changes` (workspace_routes.py:463-472, cross-upload-version) — both built; the *upload* one is already wired into the current page's `#wsChanges` view, the *filing* one is unused. |
| Freshness page/banner | `GET /api/freshness` + the `FreshnessMonitor` (`serve/monitor.py`) — full status machine (`ok/stale/error/never/unconfigured/disabled`), pending-filing list, Federal-Register delta, admin "check now" (`POST /api/admin/freshness/check`) — only a one-line banner is rendered today; a full dashboard page is pure frontend work over an existing, richer payload. |
| Document upload + version diff, private workspaces | Full M4 upload pipeline (§1.7/§2) — reusable as-is; a rewrite mainly needs to re-implement the drag-drop/job-watch/what-changed UI in the new stack. |
| Eval-transparency / Limits page | `/api/stats` (graph counts, limits, models) plus the footer's reliability numbers are hand-authored **static prose** in `index.html` today, not sourced from a live eval-results endpoint (`artifacts/eval_*.json` is referenced only in the research doc, not served by any route) — a Limits/Method page can reuse the existing copy verbatim but has no dynamic backend to call yet unless one is added. |

### Need new backend work (nothing today, or only a fragment)

| M5/buyer item | Gap |
|---|---|
| **Export to PDF/Markdown with footnoted citations** | No export endpoint, no PDF/Markdown generator, and no `weasyprint`/`reportlab` dependency anywhere in `src/semigraph` or `pyproject.toml` (each grepped individually). Would need a new route that re-renders a stored answer + its resolved citations into a document. |
| **Conversation history / follow-ups** | No `Conversation` node type, no session/thread persistence anywhere in the codebase (a grep for `Conversation` across `src` finds nothing) or in `store.py`; `sessionStorage` only ever holds the upload-workspace identity, never Q&A history. The two research docs **disagree on the design**, and neither has been built: `research-buyer-demo.md` item 9 proposes `Conversation` nodes in Neo4j; `research-scale-frontend.md`'s A4 table instead recommends **no checkpointer at all** — "keep history client-side." Whether this needs *new backend work* actually depends on which design M5 picks: a client-only history (list of past Q&A pairs re-rendered from what the browser already received) needs none; a true **follow-up** feature — a second question that can refer back to the first — needs the server to accept prior turns at all, and `AskRequest` (routes.py:96-101) today carries only `question, strategy, turnstile_token, workspace_id, as_of`, with no conversation/thread id or prior-turn field, so multi-turn *retrieval* is backend work regardless of where display-history lives. M5 should resolve the design conflict first, then scope accordingly. |
| **API + OpenAPI + hashed keys** | FastAPI is created with `docs_url=None, redoc_url=None` (`main.py:265-266`) — OpenAPI is **explicitly disabled**, not merely unbuilt. No API-key model or `api_key`/hashed-key field exists anywhere (grepped `src/semigraph/serve` and `config.py` individually); the only bearer-style secrets today are `ADMIN_TOKEN` (operator-only, `routes.py:414-418`) and the per-workspace upload token (`uploads/repo.py`, not a general API key). Needs a new key-issuance/hash-and-compare mechanism, per-key quotas, and re-enabling (and probably curating, since the auto-generated schema would otherwise expose internal `/api/admin/*` routes) the OpenAPI schema. |
| **Read-only MCP server** | No MCP server, no `mcp`/`fastmcp` reference anywhere in `src/semigraph` or `pyproject.toml` (grepped individually), no tool-exposing route layer for it in this repo. Net-new (the research doc's own recommendation, FastMCP, would sit in front of the *existing* `/api/evidence`, dossier and stats reads — those reads are the reusable part). |
| **Cloudflare Access invite gate + roles + audit log** | No auth/identity layer at all beyond the admin-token check and per-IP rate limiting; no `AuditEvent` node type or hash-chain logic in the graph schema. Net-new; the research doc's Cloudflare-Access-JWT-verification approach is unimplemented. |
| **Admin console (cost/answer, routing, eval scores, Langfuse link)** | Only `GET/POST /api/admin/policy` (kill switch + `ledger_summary`, `store.py`) and `POST /api/admin/freshness/check` exist; no per-answer cost breakdown endpoint, no routing/eval-score aggregation endpoint, no UI at all. The ledger data (`store.ledger_summary`) is a plausible seed for a cost-by-day view, but nothing shapes it for "cost per answer" or "routing rate" yet. |
| **Supply-chain map (React Flow + GDS centrality)** | No graph-topology export endpoint (only `dossier`'s 1-hop `company_edges_query` and the answer-time relationship retrieval exist — neither returns a graph-wide edge list suited to a map), and **no GDS usage anywhere in the codebase** (`grep -i gds` across `src/semigraph` returns nothing) — centrality would have to be computed and precomputed/cached from scratch, plus a new endpoint to serve node/edge/centrality JSON to React Flow. |
| **Async/scale rework** (Valkey admission, FalkorDB, Locust to 1,000 VUs) | Out of frontend scope but blocking for the M5 gate: today's rate limiting, answer cache, kill switch and daily ceiling are **all** per-process-local (`guard.RateLimiter`, in-memory dict) or Neo4j-backed (`serve/store.py`), single-machine by explicit design (`guard.py:5-8`). `research-scale-frontend.md`'s whole §A is about replacing this before any horizontal scale claim can be made; nothing in the current serve layer does it yet. |

---

## 5. Research docs: what M5 should take from them, and where today's code has moved on

### `docs/v2/research/research-buyer-demo.md` (dated 2026-09-25)

- Its ranked feature table (§1) largely **already matches shipped M4 backend work**: item 6 (risk-change delta) and
  item 7 (company dossier) are placed under "Milestone M2" in the table, but the doc is explicit that this
  milestone column is "my proposal" (§1, "Milestones: they are my proposal"), not a binding schedule — and what
  actually shipped put this data under M4 instead (`dossier_routes.py`'s own docstring cites
  `docs/v2/M4_PLAN.md 4.5`). The *feature* ranking and effort/risk estimates still look reasonable; only the doc's
  own proposed sequencing turned out to differ from what was built. Nothing in the doc's item 6/7 mentions that the
  dossier/risk-changes routes are **already built and unused** by any UI — that's new information this inventory
  adds (§4).
  - Item 4 ("API with OpenAPI docs and hashed keys", "Effort S", "FastAPI native") understates the work: FastAPI's
    OpenAPI generation is currently turned **off** (`docs_url=None, redoc_url=None`), so "native" would need to be
    re-enabled and then curated (the auto-generated schema would otherwise expose internal admin routes) before it
    is buyer-presentable — a real gap in the doc's effort estimate, not just a milestone-label typo.
  - Item 13 ("Admin console… Langfuse link") assumes Langfuse tracing is wired up broadly; today it is wired only
    for the **opt-in agent** path (`serve/tracing.py`, gated by `agent_enabled` and a sampling rate,
    `serve/routes.py:176`) — the hybrid/vector paths that most traffic will use are **not traced**, so an admin
    console's "Langfuse link" would only ever cover a slice of answers unless tracing is extended.
- §4 "Honest disclaimers" and the Limits-page section list map closely to prose **already shipped verbatim** in the
  current footer (§1.1 above) — the withdrawn-accuracy note and reliability note in `index.html` are a stricter,
  more hedged version of the doc's disclaimer #4 ("the citation check confirms retrieval, not entailment"). The doc
  predates the 2026-09-26 accuracy withdrawal by one day; what the review actually withdrew (`M1_REPORT.md:52`,
  `REVIEW_2026-09-26.md:53`) is the **judge-calibration/correctness** numbers, because the labels came from
  "three blind AI passes (**AI-assigned, not human**)" that judged against the same retrieved context the model
  saw rather than the source filing, so they could not catch a false "dropped risk" claim — not, by name, the
  doc's separate "Faithfulness was 0.865–0.927" figures (a different, full-context-judged metric that
  `M1B_PLAN.md:51` lists as a check being *kept*, not retired). This inventory did **not** find text in either
  review doc that names or withdraws the faithfulness figures specifically, so an M5 Eval/Limits page should treat
  them as unverified-but-not-explicitly-retracted rather than assuming either that they are safe to reuse or that
  they were covered by the same withdrawal as the correctness numbers.
- §5's Cloudflare-Access / roles / audit-log / quotas design is still fully applicable — none of it exists yet
  (§4 above), so the doc's recommendations stand unchallenged by current code.
- §6's demo storyline references "the old chunks turn stale and the new ones current" for an uploaded revised
  document — this is **exactly** what `staleClass`/`workspace.stale_citations` already implements end-to-end
  (§1.3, §1.7); the storyline is demo-ready against current code without further backend work for that beat.
- §7's cost/scale/hallucination objection-answers cite "0 of the cited IDs were absent" and specific 100%/90%
  correctness figures from the same now-withdrawn correctness benchmark the page itself disclaims — an M5
  buyer-facing Eval page must **not** resurrect these correctness numbers; it should instead surface whatever the
  "source-text-grounded re-measurement… in progress" (index.html:75, linking `M1B_PLAN.md`) eventually produces.
- §3's delta caveat — "greedy clustering at cosine 0.75 can split a reworded risk into a false 'dropped plus new'
  pair" — **describes a retired system**, not the one the shipped page's copy or `dossier.py` uses today.
  `src/semigraph/graph/temporal.py:1-4` states this in its own module docstring: the old pass "clustered every
  RiskFactor's LLM SUMMARY by embedding similarity (greedy, 0.75 cosine)… called each cluster a 'lineage'… and
  marked a lineage 'Deleted' although its text was still in the newer filing" — exactly the failure mode the doc
  warns about — and was **replaced** by the current sentence-level text-alignment comparison
  (`graph/alignment.py`, `graph/passages.py`, `graph/items.py`) that the M1b review and the page's own reliability
  footer (§1.1) describe instead. (A different `0.75` — `alignment.py:140`'s `lex_only_accept` threshold for
  lexical-only sentence-pair acceptance — is part of the *current* aligner and is unrelated to the retired
  clustering the doc's caveat is about.) An M5 demo script should drop this specific caveat rather than carry it
  forward against code it no longer describes; the newer, real caveats are the ones in the page's own reliability
  note (§1.1: e.g. "6 of 12 'new' items were already disclosed earlier").

### `docs/v2/research/research-scale-frontend.md` (dated 2026-09-25)

- Part B (frontend) is the direct source for the M5 PLAN.md line about "Next.js static frontend + Vercel AI SDK";
  its recommendation (Next.js static export + AI SDK `useChat` + AI Elements + a **custom** citation drawer, since
  AI Elements' own `InlineCitation` is only a hover card) is consistent with what would be needed to reproduce
  §1.3/§1.6 above, and its screen list (§"Screens" 1-6) matches the M5 PLAN.md screen list closely.
- Its citation-wiring description ("The drawer fetches `/api/evidence/{chunk_id}` (routes.py:120-130), which
  already returns filer, form, date, URL and text") is **stale on line numbers**: as of this branch, `GET
  /api/evidence/{evidence_id}` is at `routes.py:200-217`, not 120-130 (that range is now the `index`/`healthz`/
  `examples` handlers). The *behavior* description is still accurate — the route does return exactly those fields
  for a chunk id — only the line reference has drifted as the file grew with M4 work (workspace routes, evidence
  query docstring, etc.). This is a reminder that any M5 spec citing line numbers from this research doc should be
  re-verified against current `routes.py`, not trusted as-is.
- Its "backend validates `[S3]` markers against retrieved IDs and renumbers them" line describes a **renumbering**
  scheme (`[S1]`, `[S2]`, …) that the shipped citation grammar does not implement — the actual system cites by
  stable, semantically meaningful ids (`chunk`/`xbrl`/`fr`/`doc` forms, §1.3) and never renumbers; a rewrite should
  follow the *shipped* grammar (already cross-tested against `retrieval/ids.py`) rather than the doc's generic
  `[Sn]` sketch, which predates M1b's four-form id design becoming final.
- Part A's scale *findings* (single-machine limiter, 2 answer slots, in-memory rate limiting) still hold exactly —
  `guard.py:34-54`'s `RateLimiter` is still the same process-local sliding-window design, and
  `main.py:236` (`threading.BoundedSemaphore(settings.max_concurrent_answers)`) still gates concurrency in-process;
  nothing in Worker C's M4 upload work touched this, so the doc's "v1 answer path cannot scale horizontally"
  finding is unchanged and still the real blocker for M5's 1,000-VU load-test gate. Its own **line-number
  citations for those findings have drifted**, though, from the file growth since 2026-09-25: `A2` cites
  `routes.py:164-201` for `_paid_stream`, which is now at `routes.py:347-411`; `main.py:86` for the
  `BoundedSemaphore`, which is now at `main.py:236`; and `config.py:57` for `max_concurrent_answers`, which today
  holds an unrelated setting (`critic_input_price_per_mtok`) — the real default now sits at `config.py:68`. The
  *conclusions* are unaffected; only the pointers need re-verifying before quoting them in an M5 spec.
- The doc does not mention the freshness monitor, the dossier/risk-changes routes, or the opt-in agent at all
  (all M3/M4 work landed after or around when this doc was written) — its frontend screen list should be read as a
  **starting point**, not a complete inventory: this inventory's §1 and §4 are the more current source for what a
  Next.js rewrite actually needs to reproduce or newly expose.

---

## Summary of load-bearing file:line references

- Page: `src/semigraph/serve/static/index.html` (1,043 lines; DOM ids/markup 1-132, pure helpers 134-580, stateful
  page behavior 580-1039).
- CSP / security headers / `GET /`: `src/semigraph/serve/routes.py:34-39, 119-123`.
- `/api/ask` gate order and SSE assembly: `routes.py:233-277, 347-411`.
- Citation id grammar (source of truth): `src/semigraph/retrieval/ids.py:22-25`.
- Answer event grammar: `src/semigraph/retrieval/answerer.py:470-683, 593-624`.
- Checks object / failure predicate: `src/semigraph/retrieval/verify.py:326-378`.
- Agent event additions: `src/semigraph/agent/stream.py:43-62, 127-146`.
- Workspace answer additions: `src/semigraph/retrieval/workspace.py:174-202`.
- Dossier/risk-changes (unused by UI): `src/semigraph/serve/dossier_routes.py:47-80`, `src/semigraph/retrieval/dossier.py`.
- Freshness monitor/status machine: `src/semigraph/serve/monitor.py`, `src/semigraph/serve/monitor_routes.py`.
- Upload jobs/repo: `src/semigraph/uploads/jobs.py`, `src/semigraph/uploads/repo.py`.
- OpenAPI disabled / router wiring: `src/semigraph/serve/main.py:264-270`.
- Feature flags: `src/semigraph/config.py:63-102` (`admin_token`, `agent_enabled`, `freshness_enabled`,
  `uploads_enabled`, `turnstile_site_key`/`turnstile_secret_key`, rate-limit/quota settings).
- UI tests: `tests/ui/*.test.mjs` (8 files), `tests/test_static_ui.py`, `tests/test_serve_agent_ui.py`.
- M5 plan text: `docs/v2/PLAN.md:91-110` (milestones §6, buyer demo §7).
- Research docs: `docs/v2/research/research-buyer-demo.md`, `docs/v2/research/research-scale-frontend.md` (both
  dated 2026-09-25 in their own text).
