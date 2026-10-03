# M5b test classification: the old page's tests against the new Vite + React page

Date: 2026-10-03. Branch `v2`. Purpose: promotion gate 1 of M5_DECISIONS.md section 2.3 ("every old assertion and pin mapped to a new test or a written obsolete reason"). This file is the classification and the plan for the mapping; it changes no source, test or other document. Gate 1 is met only when the new tests it names exist (or a written obsolete reason does), not by this file.

State inspected: HEAD `2ed0a40`; `src/semigraph/serve/static/index.html` git blob `4b83233`; `git status --short tests/ui src/semigraph/serve` printed nothing (no uncommitted change to the page or to the UI tests). The live-page accessibility fixes planned in M5A_BUILD_PLAN.md:106 (focusable chips, `aria-live` on `#status`, a label for the question box) will edit `index.html` and the a11y tests and pins; rows that touch chips may need a refresh after they land.

## 1. Method and exact counts

Every test was read in full, together with `tests/ui/harness.mjs` and the page script (`index.html` lines 135 to 1075), so each contract below comes from the test body and the code it calls, not from its title. Commands were read-only.

| Command | Result |
|---|---|
| `node --test tests/ui/*.test.mjs` | `tests 178`, `pass 178`, `fail 0`, `suites 0` (Node v24.19.0) |
| `node --test` (default discovery) | the same: 178 pass |
| `node --test tests/ui/` (a directory argument) | fails with `Cannot find module '...\tests\ui'` (MODULE_NOT_FOUND) and reports 1 failing test. Node 24 reads the argument as a module path. The file-list form above works and is the form `tests/test_static_ui.py` uses. |
| `node --test tests/ui/<file>` for each of the nine test files | a11y 27, agent 19, badges 15, checks 19, chips 12, copy 10, evidence 16, upload_flow 24, workspace 36 (sum 178) |
| `grep -cE '^\s*test\(' tests/ui/*.test.mjs` | a11y 19, agent 19, badges 15, checks 19, chips 12, copy 10, evidence 16, upload_flow 24, workspace 36 (sum 170) |
| `rtk proxy uv run pytest tests/test_static_ui.py tests/test_serve_agent_ui.py --collect-only -q` | `40 tests collected` |
| `rtk proxy uv run pytest tests/test_static_ui.py tests/test_serve_agent_ui.py tests/test_serve_api.py::test_index_sets_security_headers -q` | `41 passed` |
| `grep -rln "index.html\|static/index\|INDEX_PATH" tests/` | `tests/test_serve_agent_ui.py`, `tests/test_static_ui.py`, `tests/ui/harness.mjs`, `tests/ui/upload_flow.test.mjs`, and compiled caches under `tests/__pycache__` (one is for a `test_verify_none_report` module that has no source file; ignored) |
| `grep -rn "routes.CSP\|content-security-policy\|CSP" tests/ --include=*.py` | the CSP assertion in `test_serve_agent_ui.py`, the header test in `tests/test_serve_api.py` (the adjacent row in section 4.3) and a comment in `test_static_ui.py` |
| search of `src/` for `SECURITY_HEADERS`, `Content-Security-Policy`, `add_middleware`, `StaticFiles` | only `routes.py:37` (definition) and `routes.py:123` (the `/` handler) |

In this environment the plain `uv run pytest ... --collect-only -q` printed "Pytest: No tests collected", because the command-output filter (RTK) condenses pytest output; `rtk proxy` bypasses it and gives the raw list above.

Counts and discrepancies, each with its unit:

- **Node tests: 178** by the runner, in nine test files. The tenth file in `tests/ui/` is `harness.mjs`, a helper with no tests (M5A_BUILD_PLAN.md:106 counts "10 files incl. `a11y.test.mjs`", which includes the helper).
- **Runner 178 versus grep 170: a difference of 8.** `a11y.test.mjs` lines 206 to 218 hold one `test(` call inside a `for` loop over 9 key events, which the runner reports as 9 tests (18 single tests plus 9 gives 27, against 19 call sites). In every other file grep and runner agree.
- **M5_DECISIONS.md gives the node-test count as 149** (lines 75 and 76 say "149 node tests"; lines 90 and 142 say "149-test classification"), and M5A_BUILD_PLAN.md:106 says "recorded 149 vs grep 158". Neither matches today's 178 (runner) or 170 (grep). The 149 is the runner count before commit 208364a, whose message records "node 178 (149 + 29 new)": the 29 added tests are the accessibility fixes (27 in `a11y.test.mjs`, 2 in existing files). This file uses 178.
- **Pytest: 40 collected items from 25 test functions** (`test_static_ui.py`: 25 items from 10 functions; `test_serve_agent_ui.py`: 15 items from 15 functions). Two functions are parametrized: the withdrawn-claims pin (13 cases) and the citation-grammar pin (4 cases); each is one row with its case count.
- **Rows versus tests.** The node tables have 170 rows covering 178 tests (the a11y key loop is one row covering 9). The pytest tables have 25 rows covering 40 items. Section 4.3 adds one more existing test, outside the 40. Summary counts below are in tests or items, not rows.

### Classes

- **PORT**: the assertion is about user-visible text, a copy rule, a data transformation or an escaping outcome that would hold in any renderer. The new test uses the same fixtures and the same expected values. Rows marked `[edit]` also need a mechanical change: the import path, and usually an assertion that today runs a regex over an HTML string (or over a `plain()` object) must read rendered text or the DOM instead (for example the chip-markup token, `<b>` counts, `<button` counts). Fixtures and wording do not change. Rows without `[edit]` move verbatim apart from the import line.
- **REWRITE**: the assertion depends on something that will not exist in the new stack: the text of the old page script, the vm and its globals (`wsAskAsOfInstant`, `evidenceCache`, `turnstileToken`, `currentWorkspace`), the fake DOM, `markupElement()` reading `index.html`, old DOM ids or old markup. The contract is kept; a new test is written with a different harness. Fixtures and case tables are often reusable.
- **OBSOLETE**: the contract is intentionally dropped, with a cited decision. A row with `[SEC]` is never OBSOLETE.
- **UNCLEAR**: the contract cannot be read from the test.
- **Tie-break**: when unsure between classes, the row is REWRITE or PORT, never OBSOLETE.
- **`[SEC]`** in the contract column marks a security, privacy or paid-call (cost or abuse control) contract. It is a reading aid, not a class.

### Targets

- **unit**: Vitest unit test of a pure TypeScript function with the same name and signature as today.
- **RTL**: Vitest with React Testing Library (and user-event) component test.
- **E2E**: Playwright against the built page.
- **client**: unit test of the thin typed client (`web/src/lib/sse.ts` in M5A_BUILD_PLAN.md:106) with recorded SSE frames and a fake `fetch`.
- **pytest**: backend or contract pytest (including `tests/test_serve_ui_v2.py`, M5_PLAN.md:218).
- **scan**: a pytest or lint scan of the new `web/src` and the built `dist/`.

### Result on OBSOLETE: none

No test maps to a contract that a decision drops, and I did not create OBSOLETE rows to fill the column. The "N risks dropped" answer-screen badge (M5_DECISIONS.md section 2.3 and decision 9; M5_PLAN.md:55) was never built on the old page: it appears only in planning documents (PLAN.md:103, M5_PLAN.md:55, M5_DECISIONS.md:79 and 131), and no old test or pin mentions it. The stats-line wording ("no longer stand alone (text check)", API key `removed_risk_items`) is the wording M5_PLAN.md:55 says stays, so those tests are PORT. MCP and API keys (decision 13, deferred) have no page tests.

When the old page itself is deleted (M5_PLAN.md:218 removes `index.html`, `tests/ui/`, `tests/test_static_ui.py` and `test_serve_agent_ui.py` after the second B6 confirmation, with their pins living in `web/tests` and `tests/test_serve_ui_v2.py`), the old-side tests go with it. That is not an OBSOLETE classification: until then they stay in place and keep passing, because `/` and `/legacy` serve the old page (gate 7).

## 2. Summary

Counts are in **tests or items** (node tests as the runner counts them; pytest items as `--collect-only` counts them), not rows. "Rows" is the number of table rows in sections 3 and 4.

| Source | Rows | Tests or items | PORT | of which `[edit]` | REWRITE | OBSOLETE | UNCLEAR |
|---|---:|---:|---:|---:|---:|---:|---:|
| `tests/ui/a11y.test.mjs` | 19 | 27 | 0 | 0 | 27 | 0 | 0 |
| `tests/ui/agent.test.mjs` | 19 | 19 | 6 | 1 | 13 | 0 | 0 |
| `tests/ui/badges.test.mjs` | 15 | 15 | 15 | 4 | 0 | 0 | 0 |
| `tests/ui/checks.test.mjs` | 19 | 19 | 19 | 3 | 0 | 0 | 0 |
| `tests/ui/chips.test.mjs` | 12 | 12 | 12 | 6 | 0 | 0 | 0 |
| `tests/ui/copy.test.mjs` | 10 | 10 | 10 | 6 | 0 | 0 | 0 |
| `tests/ui/evidence.test.mjs` | 16 | 16 | 16 | 13 | 0 | 0 | 0 |
| `tests/ui/upload_flow.test.mjs` | 24 | 24 | 0 | 0 | 24 | 0 | 0 |
| `tests/ui/workspace.test.mjs` | 36 | 36 | 36 | 10 | 0 | 0 | 0 |
| **Node tests, subtotal** | 170 | 178 | 114 | 43 | 64 | 0 | 0 |
| `tests/test_static_ui.py` | 10 | 25 | 18 | 18 | 7 | 0 | 0 |
| `tests/test_serve_agent_ui.py` | 15 | 15 | 1 | 1 | 14 | 0 | 0 |
| **pytest items, subtotal** | 25 | 40 | 19 | 19 | 21 | 0 | 0 |
| **Total (178 node tests + 40 pytest items)** | 195 | 218 | 133 | 62 | 85 | 0 | 0 |
| Adjacent, outside the 40: `tests/test_serve_api.py::test_index_sets_security_headers` (section 4.3) | 1 | 1 | 0 | 0 | 1 | 0 | 0 |

Reading the table:

- Of 218 tests and items, **133 are PORT** (62 of them need an `[edit]`; 71 move verbatim apart from the import line) and **85 are REWRITE**. **OBSOLETE is 0** and **UNCLEAR is 0** (see sections 1 and 6). The adjacent header test adds one REWRITE.
- The 85 REWRITE tests are in four places: the keyboard tests in `a11y.test.mjs` (27), the upload-flow tests (24), the DOM-driven agent tests (13), and the pytest pins that read old page source or markup (21). Every other node file is PORT.
- 29 rows carry `[SEC]`, covering 32 tests or items (14 PORT, 18 REWRITE, none OBSOLETE). They are the ones to check first when the new tests are written.
- Most of the PORT rows test pure functions (`answerBadge`, `checksSummary`, `chipLabel`, `stepText`, `freshnessLine`, ...) whose logic can move to TypeScript with its fixtures. The cost of the port is mostly in the REWRITE rows, which need new harnesses: React Testing Library, Playwright, a typed-client test with recorded SSE frames, and scans of the new source.

## 3. Node tests (`tests/ui/*.test.mjs`)

One table per file. The number before each test name is its position among the file's `test()` call sites; other sections refer to rows by file and number.

### 3.1 `tests/ui/a11y.test.mjs` (27 tests, 19 rows)

`tests/ui/a11y.test.mjs`: keyboard and assistive-technology behaviour of the evidence drawer and the upload drop zone (promotion gate 3). Every row is REWRITE: the file runs the old page's script in a vm against a focus-aware fake DOM (`FakeElement`) whose elements are built from the old markup by `markupElement()`, and none of that exists in the new stack. Runner count 27 = 18 single tests + 1 loop of 9 key events (one row below).

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. first load: the drawer is closed in the markup and nothing inside it can be reached by Tab or by assistive technology | A closed evidence drawer, and the close button inside it, cannot be reached by Tab or by assistive technology from the first paint. | REWRITE | E2E (Tab order, accessibility snapshot); RTL for the attributes | Built on the fake DOM and `markupElement`. The 'right before any script runs' clause cannot hold for a client-rendered page, so the new test asserts the state at first render (gate 3). |
| 2. opening from a citation moves focus into the drawer and exposes it to assistive technology | A click on a citation chip opens the drawer, moves focus to its close button and exposes the drawer to assistive technology. | REWRITE | E2E; RTL | Fake DOM focus model; real focus and `inert` behaviour should be tested in a real browser (gate 3 asks for scripted keyboard tests). |
| 3. a citation in the what-changed view opens the drawer the same way | A chip in the what-changed view opens the same drawer, and closing it returns focus to that chip. | REWRITE | E2E; RTL | Fake DOM. The second chip surface (`#wsChanges`) must be covered in the new Workspace view too. |
| 4. closing with the close button returns focus to the opener and takes the drawer out of the tab order again | Closing with the close button returns focus to the chip that opened the drawer and takes the drawer out of the tab order again. | REWRITE | E2E | Fake DOM. Gate 3: focus returns to the right chip. |
| 5. Escape closes the drawer, returns focus to the opener and re-hides the drawer | Escape closes the drawer, returns focus to the opener and hides the drawer again. | REWRITE | E2E | Fake DOM; the key listener is on `document` today. |
| 6. Escape with the drawer closed does nothing, and a stale opener is never refocused | Escape with the drawer closed does nothing, and an opener from an earlier open is never refocused. | REWRITE | RTL; E2E | Fake DOM. |
| 7. Escape while focus is on another control closes the drawer without pulling focus away from it | Escape pressed while focus is on another control closes the drawer without pulling focus away (for example while typing in the question box). | REWRITE | RTL; E2E | Fake DOM. |
| 8. a second citation clicked while the drawer is open becomes the element focus returns to | A second chip clicked while the drawer is open becomes the element that focus returns to. | REWRITE | E2E | Fake DOM. |
| 9. with today's non-focusable chips the drawer still opens and closes cleanly and leaves nothing hidden in the tab order | Opening and closing the drawer works when the opener cannot take focus, and leaves nothing hidden in the tab order. | REWRITE | E2E; RTL | Chips become focusable under gate 3, so a 'non-focusable chip' stops existing, but the same code path (no focusable opener) arises when an answer re-render unmounts the opener (gate 3: focus returns to the right chip even after re-render). Keep it as that case. Not OBSOLETE. |
| 10. a click on something that is not a citation does not open the drawer | A click on something that is not a citation does not open the drawer. | REWRITE | RTL | Fake DOM. |
| 11. Ctrl+Enter still submits the question; a bare Enter or Escape does not | Ctrl+Enter submits the question exactly once; a bare Enter or Escape does not. | REWRITE | RTL (user-event, fake client) | Drives `ask()` through the fake DOM with a counting fake fetch. The only test of the ask shortcut. |
| 12. the close button has an accessible name beyond the multiplication sign | The drawer's close button has an accessible name beyond the multiplication sign. | REWRITE | RTL (`getByRole("button", {name: /close/i})`) | Reads the old markup through `markupElement`; a one-line check against the new component. |
| 13. the drop zone is a keyboard tab stop with a button role; the hidden file input is not a second one | The upload drop zone is a keyboard tab stop with a button role, and the hidden file input is not a second tab stop. | REWRITE | E2E; RTL | Fake DOM built from the old markup. |
| 14. Enter on the drop zone opens the file chooser once, and the key is consumed | Enter on the drop zone opens the file chooser once and the key is consumed. | REWRITE | RTL; E2E | Fake DOM. |
| 15. Space on the drop zone opens the file chooser once and does not scroll the page | Space on the drop zone opens the file chooser once and does not scroll the page. | REWRITE | RTL; E2E | Fake DOM. |
| 16. `<key>` on the drop zone does not open the file chooser and is not consumed (9 cases: Tab, Shift+Tab, Escape, a letter, an arrow, Ctrl+Enter, Cmd+Enter, Alt+Enter, a held-down Enter) | The drop zone is not a keyboard trap: Tab, Shift+Tab, Escape, letters, arrows, and modified or repeated Enter (including the Ctrl+Enter ask shortcut) neither open the file chooser nor are consumed. | REWRITE | RTL or E2E (`test.each`) | Fake DOM. The 9-case key table carries over verbatim as the `test.each` data. |
| 17. a mouse click on the drop zone still opens the file chooser exactly once | A mouse click on the drop zone opens the file chooser exactly once. | REWRITE | RTL | Fake DOM. |
| 18. drag and drop on the zone behave as before | Drag and drop on the zone still works: dragover and dragenter are cancelled and style the zone, a drop passes the files to the upload handler and never opens the chooser. | REWRITE | RTL (`fireEvent.dragOver`/`drop` with `dataTransfer`) | Fake DOM. |
| 19. choosing files in the chooser still uploads them | Choosing files in the file chooser uploads them. | REWRITE | RTL | Fake DOM; replaces the page-level `handleFiles` global with a spy. |

The focus-return rows use a synthetic chip with `tabindex="0"` (`chip()` in the file). Real chips from `renderMarkdown` have no tabindex today (the file's own comment says so), so these contracts are never exercised against real chip markup on the old page.

### 3.2 `tests/ui/agent.test.mjs` (19 tests, 19 rows)

`tests/ui/agent.test.mjs`: the opt-in 'Deep research (agent)' option, step timeline, hint and privacy line. The agent UI is not dropped by any M5 decision (the `step` event stays in the SSE grammar), so nothing here is OBSOLETE. The pure functions port; the rows that drive `syncAgentUi`, `addStep` and `clearSteps` through a stub DOM (`Fake`) become component tests.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. a step reads as a short plain sentence built from the tool and the summary | Each agent step reads as a short plain sentence built from the tool and the summary ('Looked up Nvidia', 'Fetched revenue for FY2024-2026'). | PORT | unit (`stepText`) | Pure function; fixtures verbatim. |
| 2. a failed step says so, a step without a summary is just the verb, an unknown tool gets a generic verb | [SEC] A failed step says '(failed)'; an unknown or hostile tool name (markup, `constructor`, `__proto__`) never becomes markup and never reaches an object prototype lookup. | PORT | unit | Pure function; verbatim. |
| 3. a step is defensive about the shape of what the server sent | A malformed step payload (null, wrong types, a 500-character summary) still gives one bounded line. | PORT | unit | Verbatim. |
| 4. server text is returned as plain text for textContent, never escaped into markup and never interpreted | [SEC] Server text in a step is returned verbatim as plain text, to be displayed as text and never parsed. | PORT | unit | Verbatim. The 'rendered as text' half is row 17 below. |
| 5. agentEnabled is true only for an explicit true | The agent option appears only when the service reports `agent_enabled` as an explicit true. | PORT | unit (`agentEnabled`) | Verbatim. |
| 6. with the agent off nothing agent-related is shown | While the service reports the agent off, nothing agent-related (option, hint, privacy line, timeline) is shown. | REWRITE | RTL | Drives `syncAgentUi` against the stub `Fake` elements. The new test renders the Ask view with agent-off stats. |
| 7. with the agent on the option appears once, however often the stats are reloaded, without a privacy line | With the agent on, the option appears exactly once however often stats are reloaded, and no privacy line appears without tracing. | REWRITE | RTL (re-render with fresh stats three times) | `syncAgentUi` and `querySelector('option[value="agent"]')` on a stub. |
| 8. the hint stays hidden for the default hybrid strategy even while the agent is enabled | The hint stays hidden for the default hybrid strategy even while the agent is enabled. | REWRITE | RTL | Stub DOM. |
| 9. picking the agent strategy shows the hint; switching back to hybrid hides it again | Picking the agent strategy shows the hint; switching back to hybrid hides it. | REWRITE | RTL (user-event `selectOptions`) | Uses the stub's `fireChange`. |
| 10. clicking a benchmark example (which forces hybrid) hides the hint at once, not only after the answer | Clicking a benchmark example (which forces hybrid) hides the agent hint at once, not only after the answer. | REWRITE | RTL | Calls the page function `askExample` through the vm. See also test_serve_agent_ui row 6 (the request body is not checked anywhere). |
| 11. syncAgentUi stays idempotent when re-run (as loadStats does after every answer) with the agent strategy selected | Re-running the stats sync (as happens after every answer) with the agent strategy selected keeps the hint shown and one agent option. | REWRITE | RTL (re-render while agent is selected) | 'Idempotent' describes imperative DOM syncing; in a declarative render it becomes 'the selection and hint survive a stats refresh'. |
| 12. the hint is hidden the moment the service reports the agent off, even with agent selected | The hint is hidden the moment the service reports the agent off, even with agent selected. | REWRITE | RTL | Stub DOM. |
| 13. the privacy line appears only when a sample of agent questions is really traced, and never claims the text is sent | [SEC] The privacy line shows only when tracing is on and states exactly what a trace holds (length and one-way hash of the question, tools and models, token counts, cost, timings, fallback reason; never the text of the question or answer, never the address). | PORT | unit (`tracingNote` text) + RTL (visibility) | [edit] The wording regexes and the two `tracingNote()` returns carry over verbatim; the `hidden` assertions become visibility checks. |
| 14. turning the agent off again removes the option, resets a selected agent to hybrid and clears the timeline | Turning the agent off removes the option, resets a selected agent to hybrid and clears the timeline. | REWRITE | RTL | Stub DOM. |
| 15. the option keeps a chosen strategy that is not the agent | The strategy option keeps a chosen non-agent strategy (vector) across stats reloads. | REWRITE | RTL | Stub DOM. |
| 16. each step is one list item set through textContent, and the list becomes visible with the first step | Each step is one list item and the list becomes visible with the first step. | REWRITE | RTL (list items by role) | `addStep` against the stub; asserts `li` children's `textContent`. |
| 17. a hostile summary lands in textContent, so it is displayed as text and cannot run | [SEC] A hostile step summary is displayed as text and cannot run. | REWRITE | RTL (render the timeline with an `<img onerror>` summary: one list item with the literal text, no `img` element) | The stub's `innerHTML` setter throws, which makes the old test strong; the new test needs an equivalent assertion. |
| 18. clearing the timeline empties and hides it, and a new answer starts from step one | Clearing the timeline empties and hides it, and a new answer starts from step one. | REWRITE | RTL (a new ask resets the steps) | Page globals `stepsShown`, `clearSteps`. |
| 19. the timeline is bounded whatever the server streams | The timeline never shows more than 12 steps whatever the server streams. | REWRITE | unit if the step list is a reducer, else RTL | `stepsShown` is a page global. |

### 3.3 `tests/ui/badges.test.mjs` (15 tests, 15 rows)

`tests/ui/badges.test.mjs`: the answer badge and checks summary say what happened and never more than the checks prove. All rows are PORT: the subjects are pure functions returning text or data.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. a routed answer is never described as a verified draft | A routed (change-over-time) answer is labelled 'answered by &lt;model&gt; (routed ...)' and never as a verified or passed draft. | PORT | unit (`answerBadge`) | Pure function; fixtures verbatim. |
| 2. an escalated answer names the failed checks in plain words | An escalated answer says it escalated after a failed check and names each failed check in plain words. | PORT | unit | Verbatim. |
| 3. an unknown escalation reason falls back to a readable form | An unknown escalation reason is shown as readable text (underscores become spaces). | PORT | unit (`reasonText`) | Verbatim. |
| 4. a cheap draft that passed says so, and only when no check flagged anything | '&lt;model&gt; draft passed the checks' appears only when no check flagged anything; otherwise the badge is a warning. | PORT | unit | Verbatim. |
| 5. with the routing fields missing the badge claims nothing about verification | With the routing fields missing the badge says only 'answered by &lt;model&gt;' and claims nothing about verification. | PORT | unit | Verbatim. |
| 6. a live stream without escalation falls back to the configured answer model | A live stream without escalation information is labelled with the configured answer model; missing input gives no badge. | PORT | unit | Verbatim. |
| 7. no badge text anywhere says 'draft verified' or 'all citations verified' | No answer-footer badge text says 'draft verified' or 'all citations verified'. | PORT | RTL (answer footer) | [edit] `metaHtml` regex over a string becomes an assertion on the rendered badge text; the four events are reusable verbatim. |
| 8. checks that all passed produce honest wording and no warnings | When all checks pass the badges say what was checked ('cited ids were retrieved', 'numbers matched the retrieved context'), there are no warnings, and the word 'verified' never appears. | PORT | unit (`checksSummary`) | Verbatim. |
| 9. unmatched numbers and pseudo-citations become visible warnings | Unmatched numbers and pseudo-citations become visible warnings that quote them. | PORT | unit | Verbatim. |
| 10. a cited id outside the retrieved context is flagged as bad | A cited id outside the retrieved context gets a red badge and a warning naming it. | PORT | unit | Verbatim. |
| 11. answers without a checks object (cached, older) do not claim their numbers were checked | Answers with no checks object (cached or older) say 'numbers not checked' and never claim numbers matched. | PORT | unit | Verbatim. |
| 12. a missing or malformed event never throws | Missing or malformed event data never throws in the checks or footer code. | PORT | unit; RTL for the footer | [edit] The `checksSummary` half is verbatim; the `metaHtml` half renders the footer component with the same malformed inputs. |
| 13. server-supplied strings are escaped inside the badges and warnings | [SEC] Server-supplied strings are escaped inside the badges and warnings. | PORT | RTL (hostile strings: no `script` or `img` element, the text appears literally) | [edit] The old assertions are negative regexes only (no `<script`, `<img`, `<b>x`), so they also pass if a field is silently dropped; the escaped text is never asserted present. Strengthen when porting. |
| 14. a truncated live answer is flagged, a cached one says it was saved | A live answer cut by the token budget is flagged, and a cached answer says it is a saved example answer (not 'benchmarked'). | PORT | RTL | [edit] `metaHtml` regexes become rendered-text assertions. |
| 15. the escalation status names the reasons the draft was rejected | The escalation status line names the reasons the draft was rejected and never prints 'undefined'. | PORT | unit (`escalationStatus`) | Verbatim. |

### 3.4 `tests/ui/checks.test.mjs` (19 tests, 19 rows)

`tests/ui/checks.test.mjs`: a badge may only say what the checks prove and never shows green for a check that examined nothing; plus the saved-example panel. All rows are PORT: pure functions over text and data (rows 16 to 18 go through HTML-string builders).

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. with figures examined and all grounded the numbers badge is green | With figures examined and all grounded, the numbers badge is green. | PORT | unit (`checksSummary`) | Pure function; fixtures verbatim. |
| 2. with no figure to check the badge is neutral and never green | With no figure to check the badge reads 'no figures to check' and is never green. | PORT | unit | Verbatim. |
| 3. an old payload without numbers_checked still reads 'numbers not checked', not 'no figures to check' | A payload without `numbers_checked` (older or cached) reads 'numbers not checked', not 'no figures to check'. | PORT | unit | Verbatim. |
| 4. a figure taken from the question is a warning, not a match | A figure that only the question states is a warning ('taken from your question'), never a match. | PORT | unit | Verbatim. |
| 5. echoed and unmatched figures are reported separately | Echoed and unmatched figures are reported in separate warnings. | PORT | unit | Verbatim. |
| 6. an answer with no citation that is not a refusal never shows the green cited-ids badge | An answer with no citation that is not a refusal never shows the green cited-ids badge and gets a warning. | PORT | unit | Verbatim. |
| 7. a zero-citation refusal is fine: neutral, no warning, and not green either | A zero-citation refusal is neutral: no warning and not green. | PORT | unit | Verbatim. |
| 8. a payload with zero citations and no checks at all is never green about citations | A payload with zero citations and no checks is never green about citations. | PORT | unit | Verbatim. |
| 9. cited answers keep the green cited-ids badge | Cited answers keep the green 'cited ids were retrieved' badge. | PORT | unit | Verbatim. |
| 10. an unsupported removal claim is a warning badge and names the sentence | An unsupported removal claim is a warning badge and names the sentence. | PORT | unit | Verbatim. |
| 11. the checks hint points at the change-claim reliability note and never calls the comparison verified | The checks hint points to 'How reliable are change claims?' and never calls the comparison verified. | PORT | unit | Verbatim. The new page must also carry that note (see test_static_ui row 5). |
| 12. a removal claim flag without sentences still warns | A removal-claim flag without sentences still warns. | PORT | unit | Verbatim. |
| 13. a cheap draft released with any of the new failures is not called a pass | A cheap draft released with any of the new failures (no citation, echoed figure, unsupported removal claim) is not called a pass; the badge is a warning. | PORT | unit (`answerBadge`) | Verbatim. |
| 14. checksPassed is one predicate: a refusal with no citation passes, an uncited claim does not | `checksPassed` is one predicate: a refusal with no citation passes; an uncited claim, an echoed figure or an unsupported removal claim fails; older payloads count as clean. | PORT | unit | Verbatim. |
| 15. the new escalation reasons read in plain words | The new escalation reasons read in plain words. | PORT | unit (`reasonText`, `escalationStatus`) | Verbatim. |
| 16. sentences and figures from the server are escaped in the new warnings | [SEC] Sentences and figures from the server are escaped in the new warnings. | PORT | RTL (hostile payload) | [edit] `metaHtml` regexes become DOM assertions. This test also asserts that `&lt;script&gt;` is present, which makes it stronger than badges row 13. |
| 17. with no saved example the panel says so instead of listing questions that would cost money | With no saved example the panel says so instead of listing questions that would cost money, and shows no buttons. | PORT | RTL | [edit] The `<button` regex becomes 'no buttons rendered'; the no-throw calls with `undefined` and `{}` carry over. |
| 18. examples are grouped by type and escaped | [SEC] Example questions are grouped by type, one button each, and escaped. | PORT | RTL | [edit] Counts of `<button` (3) and `class="t"` (2) become buttons and group headings by role. The hostile question is asserted only as 'no `<b>x`', a negative check. |
| 19. the hint says which figures are checked and that a bare number is not | The checks hint states which figures are checked (currency amounts and percentages), that a bare number is not, and that figures only the question states are not matches. | PORT | unit | Verbatim. |

### 3.5 `tests/ui/chips.test.mjs` (12 tests, 12 rows)

`tests/ui/chips.test.mjs`: citation chips, one grammar shared with `retrieval/ids.py`, and the answer renderer. All rows are PORT; the `renderMarkdown` rows need their chip-markup regex updated because chips become keyboard-operable buttons under gate 3.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. citation ids are classified with the three-form grammar | Citation ids are classified by the grammar (chunk, xbrl, fr); anything else (a name, a bare four-digit number, undefined) is not a citation. | PORT | unit (`classifyCitation`) | Pure function; verbatim. |
| 2. an uploaded-document id is the fourth form and gets a readable chip | An uploaded-document id (`doc:` + 12 hex + version + paragraph) is the fourth form, uppercase hex is rejected, and it renders as a readable chip 'your document v2 ¶0007'. | PORT | unit + RTL | [edit] The `renderMarkdown` regex `<span class="cite" data-id=...>` changes to the new chip element (gate 3). `classifyCitation`/`chipLabel` asserts are verbatim. |
| 3. a chunk chip shows the item and paragraph, not a bare number | A chunk chip shows 'Item 1A ¶0361', not a bare number. | PORT | unit (`chipLabel`) | Verbatim. |
| 4. once evidence has loaded a chunk chip also shows the form and period | Once evidence has loaded, a chunk chip also shows the form and period. | PORT | unit | Verbatim. |
| 5. XBRL and Federal Register chips are readable | XBRL and Federal Register chips are readable ('XBRL revenue FY ended ...', 'BIS rule ...'). | PORT | unit | Verbatim. |
| 6. an id outside the grammar is shown as it is | An id outside the grammar is shown as it is, and null gives an empty label. | PORT | unit | Verbatim. |
| 7. renderMarkdown turns each of the three id forms into a labelled chip | Each of the three id forms in answer text becomes a labelled chip. | PORT | unit or RTL | [edit] Chip-markup regex becomes the new chip element (gate 3). |
| 8. bracketed text that is not a citation stays plain text | Bracketed text that is not a citation stays plain text. | PORT | unit or RTL | [edit] `doesNotMatch(/class="cite"/)` becomes 'no chip element'. |
| 9. two ids in one bracket are not a citation (one per bracket) | Two ids in one bracket are not a citation (one id per bracket). | PORT | unit or RTL | [edit] Same change. |
| 10. model text cannot inject markup through the answer renderer | [SEC] Model text cannot inject markup through the answer renderer: script and img tags are shown as text while the citation still becomes a chip. | PORT | RTL (no `script` or `img` element; the literal text is present) | [edit] The main XSS test for model output. It asserts that `&lt;script&gt;` is present, so it is stronger than the badge rows. It does not test attribute-position injection: the chip writes the matched id into `data-id` and `title` without `esc()`, which is safe only because the grammar cannot match a quote or angle bracket (see test_static_ui row 10). |
| 11. bold, bullet lists and headings still render | Bold, bullet lists and headings (one to four `#`, all shown as one heading level) still render. | PORT | RTL | [edit] `<h3>`, `<ul><li>`, `<p>` regexes become role queries (heading, list, paragraph). |
| 12. esc handles non-strings and quotes | [SEC] `esc` escapes the five HTML characters and handles non-strings. | PORT | unit | Conditional: only if a string `esc` helper survives. With React nodes and no string builder the escaping contract is carried by chips row 10, badges row 13 and evidence row 12; do not drop this row without confirming those exist. |

### 3.6 `tests/ui/copy.test.mjs` (10 tests, 10 rows)

`tests/ui/copy.test.mjs`: page copy that depends on live data (stats line, models and limits line, cost note, retrieval status). The stats-line wording ('no longer stand alone (text check)', API key `removed_risk_items`) is the wording M5_PLAN.md:55 says stays, so these rows are PORT. The 'N risks dropped' answer-screen badge is a different, never-built element (section 1).

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. the stats line counts risk factors that no longer stand alone, not text-verified removals | The stats line counts 'risk factors that no longer stand alone (text check)' and does not use the withdrawn phrasings ('dropped risk lineages', 'text-verified', 'removed risk items', 'verified absent'). | PORT | RTL (stats line) | [edit] `statsHtml` regex `<b>41</b> <label>` becomes a text assertion; fixtures verbatim. |
| 2. the removed-item count is omitted when the API does not report it | The removed-item count is omitted when the API does not report it; the old edge count (`deleted_risk_lineages`) is never shown as lineages; no 'undefined' or 'NaN'. | PORT | RTL | [edit] Same change. |
| 3. a zero count is shown, not treated as missing | A zero count is shown, not treated as missing. | PORT | RTL | [edit] Same change. |
| 4. a paused service is flagged and a sparse stats object does not throw | A paused service shows 'live questions paused', and a sparse or empty stats object does not throw. | PORT | RTL | [edit] Same change. |
| 5. the limits line names the answer and escalation models from /api/stats | The limits line names the answer and escalation models and the per-address limit from `/api/stats`, and never prints 'undefined'. | PORT | unit (`limitsText`) | Verbatim. |
| 6. the cost note is derived from the configured models, not hard-coded | The cost note is derived from the configured models (one call, or a second call when the draft fails its checks), with fallback text when models are unknown. | PORT | unit (`costNote`) | Verbatim. |
| 7. the retrieval status describes risk-change items, not dropped lineages | The retrieval status line reports anchors, relationships and risk-change items (not 'dropped lineages') and says when no company was detected and Nvidia is the default. | PORT | unit (`retrievalStatus`) | Verbatim. |
| 8. shortModel drops the provider prefix | `shortModel` drops the provider prefix. | PORT | unit | Verbatim. |
| 9. removed paragraphs are counted apart from removed risk factors | Removed paragraphs are counted apart from removed risk factors, under their own label. | PORT | RTL | [edit] Regex over HTML becomes a text assertion. |
| 10. a graph with no removed paragraphs (or an API that does not report them) shows no paragraph count | No paragraph count is shown when it is zero, absent or not a number. | PORT | RTL | [edit] Same change. |

### 3.7 `tests/ui/evidence.test.mjs` (16 tests, 16 rows)

`tests/ui/evidence.test.mjs`: the evidence drawer's content (filing excerpts, XBRL, Federal Register), its escaping, and the doc-evidence cache. All rows are PORT: the assertions are about what the drawer shows. `evidenceView` returns four HTML-fragment slots (`metaHtml`, `freshHtml`, `factsHtml`, `linkHtml`) for the old drawer element ids, so rows that use it need their assertions moved to the rendered drawer ([edit]); the fixtures (`CHUNK_PAYLOAD` and the XBRL and Federal Register payloads) mirror the real `/api/evidence` shapes and are reusable verbatim, including as typed-client fixtures.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. a current filing excerpt shows a current badge, its provenance and a safe link | [SEC] A current filing excerpt shows a 'current' badge, filer, section and mentions, and an https link with `rel="noopener"`; the excerpt text is kept as raw text. | PORT | RTL (Drawer) | [edit] Slot-string regexes become assertions on the rendered drawer, including the link's `rel` and `href`. |
| 2. a superseded excerpt names the replacing filing and the end of validity | A superseded excerpt names the replacing filing, the end of validity and 'not returned by default search', and is not shown as current. | PORT | RTL | [edit] Same change. |
| 3. a corrected excerpt names the amending filing | A corrected excerpt names the amending filing, with a red badge. | PORT | RTL | [edit] Same change. |
| 4. the risk items a chunk belongs to (item_headlines, the field /api/evidence returns) are shown | The risk items a chunk belongs to (`item_headlines`) are listed under 'Risk item'. | PORT | RTL | [edit] Asserts today an exact `<dt>`/`<dd>` fragment; becomes label and value text. |
| 5. a chunk that belongs to no risk item shows no risk-item row | A chunk that belongs to no risk item shows no risk-item row. | PORT | RTL | [edit] Today `factsHtml === ""`. |
| 6. a field /api/evidence never returns (a singular `headline`) is not rendered | A field `/api/evidence` never returns (a singular `headline`) is not rendered. | PORT | RTL | [edit] Pairs with test_serve_agent_ui row 14. |
| 7. an XBRL id shows the metric payload | An XBRL citation shows company, metric, value with grouped digits and its unit (215,938,000,000 USD), concept, period and accession. | PORT | RTL | [edit] Same change. |
| 8. the payload shapes /api/evidence returns are rendered (routes.py XBRL, Federal Register and chunk queries) | The payload shapes `/api/evidence` really returns (XBRL, Federal Register, chunk) all render. | PORT | RTL | [edit] Fixtures are the closest copy of real payloads in the repo. |
| 9. an XBRL payload with a period object still renders | An XBRL payload whose period is an object still renders 'start to end'. | PORT | RTL | [edit] Same change. |
| 10. a Federal Register id shows the rule and says it is an external keyword match | A Federal Register citation shows the rule and says it is an external event found by keyword match, not a statement by the company. | PORT | RTL | [edit] Same change. |
| 11. only https links are ever rendered | [SEC] Only https links are rendered: `javascript:`, `data:`, `http:` and undefined give no link. | PORT | unit (`safeUrl`) + RTL (no anchor element) | [edit] The five `safeUrl` assertions are verbatim; the `evidenceView` link half becomes 'no anchor rendered'. |
| 12. every server-supplied field is escaped in the drawer markup | [SEC] Every server-supplied field is escaped in the drawer. | PORT | RTL (hostile payload: no `script` or `img` element, text appears literally) | [edit] Negative regexes only, so it passes if a field is dropped. `title` and `text` are exempted because the page sets them through `textContent`; that wiring (`showEvidence`) has no test (section 5). |
| 13. an empty or partial payload never throws | An empty or partial payload never throws and gives empty excerpt text. | PORT | RTL (render with `{}` and `null`) | [edit] Same change. |
| 14. formatNumber groups digits and leaves non-numbers alone | `formatNumber` groups digits and leaves non-numbers alone. | PORT | unit | Verbatim. |
| 15. dropDocEvidence removes every cached doc: entry and keeps chunk/xbrl/fr entries | After an upload job ends, cached `doc:` evidence is dropped and chunk, xbrl and fr entries are kept. | PORT | unit (`dropDocEvidence`) | Conditional: written for a `Map`; if the new evidence cache is not a Map the assertions change shape. Backs the REWRITE rows upload_flow 15 to 18 and 21. |
| 16. dropDocEvidence is a no-op on a cache with no doc: entries | Dropping doc evidence from a cache that has none changes nothing. | PORT | unit | Same condition. |

### 3.8 `tests/ui/upload_flow.test.mjs` (24 tests, 24 rows)

`tests/ui/upload_flow.test.mjs`: stateful behaviour of the upload workspace panel. Every row is REWRITE: the tests run the old page's script in their own vm and drive page functions and page-level variables directly (`wsAskAsOfInstant`, `evidenceCache`, `currentWorkspace`, `turnstileToken`, `WATCH_RECONNECTS`, `openSeq`) with hand-built DOM stubs and fake `fetch`. In React that state lives in components or hooks, so the harness cannot move. Several fakes are reusable: the single-upload-slot fetch model (`slotModelFetch`), `streamOf` and the deferred-fetch pattern. Rows marked [SEC] must not be dropped (Turnstile fail-closed, workspace isolation).

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. renderDocuments re-applies the stored ask-as-of instant to whichever select still offers it | The chosen 'ask as of vN' instant is re-applied to whichever document's selector still offers it after the document list re-renders, and no other selector stays pinned. | REWRITE | RTL (fake client) | Reads and writes the page global `wsAskAsOfInstant` through the vm, with hand-built select stubs. |
| 2. renderDocuments nulls the stored instant once no select offers it any more (its document/version is gone) | The stored as-of instant is dropped when no selector offers it any more, so a later ask falls back to the date picker. | REWRITE | RTL | Same harness. |
| 3. picking a version on one select clears every other document's select (at most one active choice) | Picking a version on one document's selector clears every other document's selector (one active choice). | REWRITE | RTL (user-event) | Same harness. |
| 4. setWorkspace resets the ask-as-of instant, so switching (or deleting/creating) a workspace never reuses it | Switching, deleting or creating a workspace resets the as-of instant, so an ask never reuses the old workspace's instant. | REWRITE | RTL | `setWorkspace(null)` and `wsAskAsOfInstant` are page globals. |
| 5. handleFiles waits for the previous file's job to finish before posting the next one | A multi-file drop posts the next file only after the previous file's job has ended (one upload slot), and both files reach the workspace. | REWRITE | RTL with fake timers and a fake client, or E2E with route mocking | Calls `handleFiles` through the vm. The slot model (429 'busy' until the job's terminal event) is reusable as the fake. |
| 6. each dropped file's own outcome is shown, not just the last one's | Each dropped file keeps its own outcome row ('ready'), not just the last file's. | REWRITE | RTL | Reads `wsJobRow-<name>` elements from a hand-built document stub. |
| 7. waitForFreshTurnstileToken resolves immediately when Turnstile is not embedded on the page | [SEC] With no Turnstile on the page, waiting for a token resolves at once with none (the server accepts no token outside production). | REWRITE | unit if the helper takes an injected token source, otherwise RTL | Sets the module-level `TURNSTILE_SITE_KEY` and `turnstileToken` through the vm. Gate 4. |
| 8. waitForFreshTurnstileToken resolves immediately with the current token when one is already set | [SEC] When a token is already present it is used at once. | REWRITE | unit or RTL | Same; gate 4. |
| 9. waitForFreshTurnstileToken resolves to a fresh token once the widget's callback sets one | [SEC] A token set later by the widget callback is picked up within the wait window. | REWRITE | unit or RTL | Same; gate 4. |
| 10. waitForFreshTurnstileToken gives up (undefined) rather than wait forever for a widget that never re-solves | [SEC] If the widget never re-solves, the wait gives up instead of hanging, and the caller must fail closed. | REWRITE | unit or RTL | Same; gate 4 requires fail-closed behaviour. |
| 11. a second dropped file waits out a slow Turnstile re-solve instead of sending an empty token | [SEC] A second dropped file waits for the re-solved Turnstile token and never sends an empty one (the header carries 'refreshed-token', not an empty string). | REWRITE | RTL with fake timers and a fake `turnstile` object; E2E with Turnstile's test key for the real widget | Gate 4: a fresh token per upload across remounts. The old failure was a silent 403 in production. |
| 12. a progress stream that ends without a terminal event is reconnected, and the replayed final state is shown | A job-progress stream that ends without a terminal event is reconnected (including through a 429), and the replayed final state is shown. | REWRITE | client (job-stream watcher, fake fetch, frame fixtures) | Page functions `watchJob` and `streamJobOnce` with hand-made streams. The `event: job` grammar is separate from the ask grammar. |
| 13. a 5xx while watching (what a restart or deploy returns) is retried, not treated as the job being gone | A 5xx while watching (a restart or deploy) is retried, not treated as the job being gone. | REWRITE | client | Same. |
| 14. after the reconnects run out the row says the connection was lost, and a 404 stops at once | After the reconnect budget is used the row says the connection was lost, and a 404 stops at once. | REWRITE | client | Reads `WATCH_RECONNECTS` from the vm. |
| 15. reaching 'ready' drops cached doc: evidence but keeps a cached non-doc entry | When a job reaches 'ready', cached `doc:` evidence is dropped and other cached evidence is kept. | REWRITE | client or RTL | Reads `evidenceCache` through the vm. The pure half is evidence row 15. |
| 16. reaching 'failed' also drops cached doc: evidence | A job that reaches 'failed' also drops cached `doc:` evidence. | REWRITE | client or RTL | Same. |
| 17. a non-terminal progress event leaves the evidence cache untouched | A non-terminal progress event leaves the evidence cache untouched. | REWRITE | client or RTL | Same. |
| 18. a watcher that gives up (connection lost) still drops cached doc: evidence: the job may finish anyway | A watcher that gives up (connection lost) still drops cached `doc:` evidence, because the job may have finished unseen. | REWRITE | client or RTL | Same. |
| 19. a doc: evidence fetch still in flight when the cache is dropped is shown but never cached | A `doc:` evidence fetch still in flight when the cache is dropped is shown but not cached, so a stale payload never re-enters. | REWRITE | RTL with a deferred fetch | Uses the page-global drop counter through the vm. |
| 20. a doc: evidence fetch still in flight across a workspace switch is never cached into the new workspace | [SEC] A `doc:` evidence fetch still in flight across a workspace switch is never cached into the new workspace. | REWRITE | RTL with a deferred fetch | Privacy: one workspace's document text must not surface in another's. Page-global state. |
| 21. a job that is gone (404) also drops cached doc: evidence | A job that is gone (404) also drops cached `doc:` evidence. | REWRITE | client or RTL | Same harness. |
| 22. two chips clicked quickly: the drawer shows the LAST one clicked, whichever fetch finishes last | With two chips clicked quickly, the drawer shows the last one clicked whichever fetch finishes last; the earlier payload is still cached. | REWRITE | RTL with deferred fetches (stale-response guard in the hook) | Page global `openSeq`; reads the drawer title from a stub. |
| 23. an evidence fetch that finishes with no drop in between is cached as before | An evidence fetch that finishes with no cache drop in between is cached as before. | REWRITE | client or RTL | Same harness. |
| 24. deleting/switching the workspace (setWorkspace) still clears the WHOLE evidence cache, doc: and non-doc alike | [SEC] Switching or deleting the workspace clears the whole evidence cache, doc and non-doc entries alike. | REWRITE | RTL | Privacy: nothing from the previous workspace stays in the browser cache. Asserts on `evidenceCache.size` through the vm. |

### 3.9 `tests/ui/workspace.test.mjs` (36 tests, 36 rows)

`tests/ui/workspace.test.mjs`: the upload workspace panel's pure functions (labels, job text, version timeline, freshness line, upload options, what-changed view). All rows are PORT. Rows 3 to 5 and 30 to 36 go through `esc` or HTML-string builders (`evidenceView`, `changeItemHtml`, `changesHtml`) and need their assertions moved to rendered text ([edit]); the rest are verbatim.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. docChipLabel falls back to a generic label before evidence has loaded | A document chip label falls back to 'your document · vN ¶NNNN' before evidence has loaded. | PORT | unit (`docChipLabel`) | Pure function; verbatim. |
| 2. docChipLabel uses the document's title once evidence is known | The chip label uses the document's title once evidence is known. | PORT | unit | Verbatim. |
| 3. docChipLabel returns plain text, like chipLabel — the caller escapes it for an HTML context | [SEC] The doc chip label is plain text (a user-controlled title is not pre-escaped); a caller that builds HTML escapes it. | PORT | unit | [edit] The `esc` half applies only if a string `esc` survives; React text nodes escape on their own. |
| 4. evidenceView for a doc id uses the richer docChipLabel and escapes an untrusted title in metaHtml | [SEC] The drawer for an uploaded-document citation uses the document title and escapes an untrusted, user-chosen title. | PORT | RTL (Drawer) | [edit] `evidenceView` doc branch; `metaHtml` assertions become rendered-drawer assertions. The title is user-controlled (the upload form's `title` field). |
| 5. evidenceView for a superseded doc chunk shows the same freshness badge a filing chunk does | A superseded uploaded-document chunk shows the same freshness badge a filing chunk does. | PORT | RTL | [edit] Same change. |
| 6. staleClass flags exactly the ids named in stale_citations | A citation is marked stale exactly when its id is listed in `stale_citations`. | PORT | unit (`staleClass`) | Verbatim. Applying the class to chips (`finish()`) has no test today (section 5). |
| 7. jobStateText names each pipeline stage in plain words | Job states are named in plain words ('queued', 'reading the document', 'comparing with the previous version', 'ready'). | PORT | unit (`jobStateText`) | Verbatim. The states `validating`, `chunking` and `indexing` are not asserted. |
| 8. jobStateText shows embedding progress and an eta when given | Embedding progress and an ETA are shown when given. | PORT | unit | Verbatim. |
| 9. jobStateText surfaces the fixed error message of a failed job, never a stack trace | A failed job shows the server's fixed error message ('failed: too many pages'), or 'failed: unknown error', never a stack trace. | PORT | unit | Verbatim. |
| 10. jobStateText tolerates a missing or unknown job object | A missing or unknown job object gives 'unknown' or the raw state, without throwing. | PORT | unit | Verbatim. |
| 11. versionTimeline marks exactly the current version | The version timeline marks exactly the current version ('v1 (superseded) → v2 (current)'). | PORT | unit (`versionTimeline`) | Verbatim. |
| 12. versionTimeline handles a single version and an empty list | The version timeline handles a single version and an empty or missing list. | PORT | unit | Verbatim. |
| 13. relativeTime reports minutes, hours and days ago | Relative time is reported as 'just now', minutes, hours and days ago. | PORT | unit (`relativeTime`) | Verbatim. |
| 14. relativeTime tolerates a missing or malformed timestamp | A missing or malformed timestamp gives 'unknown'. | PORT | unit | Verbatim. |
| 15. freshnessLine reports data-as-of, checked-when and pending count | The freshness line reports data-as-of, when it was checked and the pending count. | PORT | unit (`freshnessLine`) | Verbatim. |
| 16. freshnessLine uses singular filing for exactly one pending | The freshness line says '1 filing pending' (singular) for exactly one. | PORT | unit | Verbatim. |
| 17. freshnessLine says the check is unavailable when nothing has ever run | When no check has ever run, or the status is unconfigured, the line says 'freshness check unavailable'. | PORT | unit | Verbatim. |
| 18. freshnessLine says unavailable for disabled and never, the same as unconfigured | 'disabled' and 'never' statuses also say 'freshness check unavailable'. | PORT | unit | Verbatim. |
| 19. freshnessLine says the check failed and omits the pending count when status is error | A status of 'error' says the check failed, with how long ago, and omits the pending count. | PORT | unit | Verbatim. |
| 20. freshnessLine says the check failed for a stale status too | A status of 'stale' also says the check failed, with how long ago. | PORT | unit | Verbatim. |
| 21. freshnessLine says the check failed using last_error_at when no good check has ever landed | An error with no good check ever (`checked_at` null) uses `last_error_at` and still says the check failed, not 'unavailable'. | PORT | unit | Verbatim. |
| 22. freshnessLine prefers last_error_at over an older checked_at when status is error | For status 'error', `last_error_at` is preferred over an older `checked_at`. | PORT | unit | Verbatim. |
| 23. freshnessLine falls back to a bare failed message when neither timestamp is known | With neither timestamp known the line says only 'freshness check failed'. | PORT | unit | Verbatim. |
| 24. freshnessLine for a stale status ignores last_error_at (there is no failure instant, only an aging good check) | For a 'stale' status `last_error_at` is ignored (an aging good check, not a failure instant). | PORT | unit | Verbatim. |
| 25. uploadTargetOptions always offers a new document first | The upload target list always offers 'Upload a new document' first. | PORT | unit (`uploadTargetOptions`) | Verbatim. |
| 26. uploadTargetOptions offers a 'new version of' entry per existing document, titled or not | The upload target list offers 'New version of &lt;title&gt;' per existing document, falling back to the document id. | PORT | unit | Verbatim. |
| 27. uploadTitleFor truncates a long file name to the server's own title cap | A long file name is truncated to the title cap passed in (120); null gives an empty title. | PORT | unit (`uploadTitleFor`) | Verbatim. The test passes 120 in: nothing pins the page's own constant `MAX_UPLOAD_TITLE_CHARS` or compares it with the server's limit (not checked here). |
| 28. versionAskAsOfOptions offers 'current' plus one 'ask as of vN' per version, keyed by that version's created_at | Each document offers 'ask as of: current' plus one 'ask as of vN' per version, keyed by that version's `created_at`. | PORT | unit (`versionAskAsOfOptions`) | Verbatim. |
| 29. versionAskAsOfOptions tolerates an empty or missing version list | An empty or missing version list still offers 'ask as of: current'. | PORT | unit | Verbatim. |
| 30. changeItemHtml shows a headline alone when the unit has no surviving passage | A what-changed item with no surviving passage shows only its headline. | PORT | RTL | [edit] Today an exact string (`<li>Market Outlook</li>`); becomes a list item whose text is the headline, with no quote or chip. |
| 31. changeItemHtml renders each passage as a quote plus a doc: citation chip | Each passage of a changed item shows its quote and a citation chip for the `doc:` chunk, which opens the evidence drawer. | PORT | RTL | [edit] The chip-markup regex changes to the new chip element (gate 3). |
| 32. changeItemHtml escapes an untrusted headline and quote | [SEC] An untrusted headline and quote are escaped in the what-changed view. | PORT | RTL (hostile item) | [edit] Negative regexes only (no `<b>eek</b>`, no `<script>bad</script>`), so the test also passes if the text is dropped. |
| 33. changesHtml lists a minor_rewordings section separately from changed, never folded into unchanged_count | Minor rewordings are listed in their own section, never folded into the unchanged count. | PORT | RTL | [edit] Regexes over a string become text assertions. |
| 34. changesHtml with no minor_rewordings key renders exactly as before (backward compatible) | A report without a `minor_rewordings` key renders without that section (backward compatible). | PORT | RTL | [edit] Same change. |
| 35. changesHtml says how many sections were compared at section level only when the negation check skipped some | The view says how many sections were compared as whole sections (not sentence by sentence for negation changes), and only when some were. | PORT | RTL | [edit] Same change. |
| 36. changesHtml reports the not-compared reason when items_compared is false | When the versions could not be compared, the view says so with the reason. | PORT | RTL | [edit] Same change. |

## 4. pytest pins

The 40 collected items of the two pytest files, one row per test function (parametrized functions are one row with the case count). The `test` column gives the function name.

### 4.1 `tests/test_static_ui.py` (25 items, 10 rows)

`tests/test_static_ui.py`: 10 test functions, 25 collected items. The copy pins are PORT (same strings, same assertion, a different file set); the pins that read the old script's source text or its single-script structure are REWRITE.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. `test_node_unit_tests_pass` | A failing page unit test fails the pytest run, so CI (which sets up Node 24 in `.github/workflows/tests.yml`) cannot pass with a broken page. | REWRITE | CI job running the web package's own tests (gate 9) | The wrapper shells out to `node --test` over `tests/ui/*.test.mjs`. Keep it unchanged while `/` and `/legacy` serve the old page (gate 7); the new Vitest and Playwright suites run in the new Node CI job (gate 9). It calls `pytest.skip` when `node` is missing, so a local pytest-only run can skip all 178 tests silently; CI is not affected. |
| 2. `test_page_never_carries_the_withdrawn_claims[...]` (13 cases) | No withdrawn or overclaiming phrase appears anywhere in the page (13 phrases: 'draft verified', 'all citations verified', '95% correct', '75% correct', '100% correct', 'one Claude call', 'dropped risk lineages', 'deleted_risk_lineages', 'text-verified removed', 'text verified absent', 'verified absent', 'removed lists do not support', 'stop disclosing in its latest'). | PORT | scan (pytest over the new `web/src` and built `dist/`) | [edit] Same phrase list, same assertion; only the file set changes. The scan must exclude the new test files, which will contain these phrases as negative fixtures (for example the badge tests). |
| 3. `test_header_separates_verbatim_evidence_from_keyword_matched_rules` | The header separates verbatim filing excerpts from keyword matches to external Federal Register rules. | PORT | RTL (header) | [edit] Two substring asserts on page text; assert on the rendered header. |
| 4. `test_page_reads_the_new_stat_and_says_accuracy_is_withdrawn` | The page reads the API key `removed_risk_items`, words the count as 'no longer stand alone (text check)' (and the paragraph count likewise), and says earlier accuracy figures are withdrawn pending re-measurement. | PORT | RTL; scan for the notice | [edit] The stat wording is covered again by copy rows 1 and 9. Conditional on one point: whether the new page keeps the 'withdrawn' notice is a copy decision that M5_DECISIONS does not make, so keep this pin until that is decided. The stat wording is said to stay (M5_PLAN.md:55). |
| 5. `test_page_states_how_reliable_change_claims_are_in_plain_words_with_the_measured_counts` | The 'How reliable are change claims?' note states each class of change claim with its measured counts (4 of 4, 6 of 12, 6 of the 51, 1 of 48), uses no percentages and no 'verified', and sits in the method and limits area. | PORT | RTL (the section) | [edit] The fragment list carries over verbatim; the check that it comes after `<footer>` becomes 'it is in the method or limits area'. checks row 11 relies on this note existing. |
| 6. `test_the_placeholder_question_asks_for_risk_factors_that_no_longer_appear_not_ones_the_company_stopped_disclosing` | The question box placeholder asks about risk factors that 'no longer appear' and never says 'stop disclosing' (a claim the service cannot make). | PORT | RTL (`getByPlaceholderText`) | [edit] Same strings. Gate 3 also needs a real label on the question box; a placeholder is not one. |
| 7. `test_vector_option_is_kept_and_labelled_as_a_baseline` | The vector strategy stays selectable and is labelled 'comparison baseline' and 'no temporal reasoning'. | PORT | RTL | [edit] Option text assertion. |
| 8. `test_only_the_existing_external_script_and_inline_script_are_used` | [SEC] The page loads no script from any origin other than itself and Cloudflare Turnstile (created at run time), so it stays inside the CSP. | REWRITE | scan over built `dist/index.html` and chunks; E2E (zero CSP violations in the console) | Today this pins exactly one `<script>` tag and no `src`. A Vite build emits external same-origin module scripts by design, so the single-inline-script form cannot hold; the contract to keep is 'no script origin except self and Turnstile', with the CSP header byte-identical (gate 2). Weakness in the old test: the external-URL regex only matches URLs ending in `.js`; at run time the CSP, not this test, would block any other origin. |
| 9. `test_turnstile_placeholder_is_substituted_exactly_once` | [SEC] The Turnstile site-key placeholder is replaced exactly once at serve time, so the widget gets its key. | REWRITE | pytest (whatever mechanism delivers the key) + E2E (the widget renders and yields a fresh token, gate 4) | Static files cannot substitute a placeholder. How the key reaches `/v2/` is not decided in M5_DECISIONS (M5_PLAN.md:170 lists `GET /api/config`, written for the Next.js layout). Not designed here. |
| 10. `test_page_citation_grammar_matches_retrieval_ids[CHUNK_ID, XBRL_ID, FR_ID, DOC_ID]` (4 cases) | [SEC] The page recognises exactly the four citation id forms the answerer may cite (chunk, xbrl, fr, doc), matching `retrieval/ids.py` (4 cases). | REWRITE | pytest (regenerate-and-diff of the generated `ids.generated.ts` from `scripts/export_ids.py`, per M5A_BUILD_PLAN.md:106) + unit | The old test extracts regex literals from the page's source text (`const CHUNK_ID = /.../.source;`); that text will not exist. Security-relevant: `renderMarkdown` writes the matched id into `data-id` and `title` without `esc()`, which is safe only because none of the four patterns can match a quote or angle bracket. |

### 4.2 `tests/test_serve_agent_ui.py` (15 items, 15 rows)

`tests/test_serve_agent_ui.py`: 15 test functions, 15 collected items. These pin structure that a vm test cannot see: markup, CSS and the text of named functions in the old script. All but one are REWRITE because the assertion reads old source text or old markup.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. `test_the_agent_option_is_created_by_script_never_present_in_the_markup` | The 'Deep research' option does not exist in the markup; it is added only when the service reports the agent on. | REWRITE | RTL (agent-off stats render no option or text); scan of built `dist/index.html` for agent strings | Regexes over old markup plus the script's `querySelector('option[value="agent"]')` call. A client-rendered page ships an empty shell; the real contract is the conditional render (agent rows 6 and 14). |
| 2. `test_the_agent_containers_are_hidden_until_the_service_reports_the_agent_on` | The hint, privacy line and step list are hidden until the service reports the agent on. | REWRITE | RTL | Regex for `hidden` on three element ids in the old markup; same contract as agent rows 6, 8 and 12. |
| 3. `test_the_timeline_is_a_polite_live_region_that_announces_additions` | The agent step list is a polite live region that announces additions and has the accessible name 'What the research agent did'. | REWRITE | RTL (`getByRole("list", {name})`, `closest("[aria-live]")`) | Regex over old markup; the attributes carry over. Gate 3 separately adds a polite status region for answer-ready and errors, not on the streamed answer text. |
| 4. `test_server_text_of_a_step_never_reaches_innerhtml` | [SEC] Server-supplied step text is never inserted as HTML. | REWRITE | scan (ESLint `react/no-danger` plus a grep of `web/src` for `dangerouslySetInnerHTML`, `innerHTML`, `insertAdjacentHTML`, `outerHTML`, `eval`, `new Function`) + RTL hostile-step test (agent row 17) | Pins the text of six named functions only (`stepText`, `addStep`, `clearSteps`, `syncAgentUi`, `agentEnabled`, `tracingNote`); it does not cover `setStatus`, `docHtml` or `showEvidence`. The new scan should be page-wide. |
| 5. `test_the_stream_handler_routes_step_events_to_the_timeline_and_ask_sends_the_selected_strategy` | A `step` event goes to the timeline, and an ask sends the selected strategy and clears the previous steps. | REWRITE | client (frame fixtures from the recorded SSE bytes, M5A_BUILD_PLAN.md:106) + RTL (the ask posts the selected strategy) | Regexes over the source of the stream handler and `ask()`. This is the only test of event routing; there is no behavioural test of the stream consumer today (section 5). |
| 6. `test_the_examples_still_reset_the_strategy_to_hybrid_so_a_saved_answer_is_never_asked_live_as_the_agent` | [SEC] Clicking a benchmark example always asks with strategy hybrid, so a saved example is never sent as a paid agent ask. | REWRITE | RTL with a fake fetch: select agent, click an example, assert the POST body has `strategy: "hybrid"` | Regexes over the source of `askExample`. Agent row 10 checks the select's value and the hint, but no test inspects the request body (cost control). |
| 7. `test_nothing_in_the_new_ui_moves` | The step timeline has no transition, animation or transform. | REWRITE | scan of the new stylesheet's timeline rules, or E2E computed style | Regex over the old CSS rule text. |
| 8. `test_the_page_stays_inside_the_csp_one_script_no_new_origin` | [SEC] The page makes only same-origin `/api/` requests and stays inside the CSP (one script, no new origin). | REWRITE | E2E (record requests: all same-origin or the Turnstile origin; zero CSP violations) + unit for the client's base-URL module; the `routes.CSP` half stays as backend pytest, unchanged | Regexes over `fetch(` calls and `<script` tags in the old source. Under `/v2/` the API paths must stay absolute (`/api/...`); gate 5 tests the base path. The CSP half (`routes.CSP` contains `connect-src 'self'` and `'unsafe-inline'`) is backend and does not change (gate 2). |
| 9. `test_the_new_copy_makes_no_withdrawn_claim_and_says_what_a_trace_holds` | The agent hint and privacy line make no withdrawn claim and state what a trace holds (never the question or answer text, never the address; a sampling rate, not 'a small sample'). | PORT | unit (constants imported, same regexes) | [edit] The old test extracts `const AGENT_HINT = "...";` and `TRACING_NOTE` from the page source; the new test imports the constants. Overlaps agent row 13. |
| 10. `test_the_closed_evidence_drawer_is_inert_and_aria_hidden_in_the_markup_so_first_load_is_right_without_script` | The closed drawer is `inert` and `aria-hidden`, and its close button is named 'Close evidence'. | REWRITE | E2E; RTL | Regex over the opening tag in the old markup; same contract as a11y row 1. |
| 11. `test_the_closed_drawer_is_also_visibility_hidden_in_css_as_a_fallback_for_browsers_without_inert` | A closed drawer is also hidden by CSS (for browsers without `inert`), and the slide-out finishes before it is hidden. | REWRITE | E2E (computed `visibility`), or RTL if the closed drawer is not rendered at all | Regex over the old CSS rule. If the new drawer is unmounted when closed the fallback is moot; the contract (nothing reachable while closed) stays under a11y row 1. |
| 12. `test_the_drawer_is_opened_and_closed_only_through_openDrawer_and_closeDrawer_so_inert_aria_and_focus_cannot_drift` | The drawer's open class, `inert`, `aria-hidden` and focus change together in one place, so they cannot drift apart. | REWRITE | RTL (one test: the open and closed states set all three together) | Counts `classList.add("open")` calls and function bodies in the old script. With one `open` state driving all three, drift is impossible by construction; the behaviours are in a11y rows 2 to 8, so this adds little beyond them. |
| 13. `test_the_upload_drop_zone_is_a_focusable_button_with_a_visible_focus_style_and_keeps_its_text` | The drop zone is a focusable `role=button` div (not a label), keeps a visible focus outline and its exact instruction text, and the file input is its sibling, not a child. | REWRITE | RTL (role, name, text); E2E (focus-visible outline) | Regexes over old markup, CSS and script. The exact text ('... (up to 30 pages).') carries over; the '30 pages' figure is a limit stated in copy that I did not check against the server setting. |
| 14. `test_the_chunk_evidence_fields_the_page_reads_are_fields_the_api_returns` | The chunk-evidence fields the page reads are fields `/api/evidence` returns (no dead fields). | REWRITE | pytest: compare the TS evidence type's field names to the `routes.EVIDENCE_QUERY` columns | Parses `d.<field>` reads out of the source of `evidenceView` and `freshnessHtml`. A typed client turns this into a type-versus-query contract. |
| 15. `test_the_served_page_carries_the_agent_containers_and_the_turnstile_substitution` | [SEC] The served page contains the agent step list and has the Turnstile site key substituted. | REWRITE | pytest (the `/v2/` route) + E2E | Fetches `/` through a TestClient. Unchanged while `/` serves the old page; for `/v2/` the key delivery is undecided (see test_static_ui row 9) and the container check becomes an RTL render. |

### 4.3 Adjacent: tests/test_serve_api.py (outside the 40)

One more existing test is relevant to gate 2. It does not read `index.html`, so it is not one of the 40 items above; it is listed separately.

| test | contract | class | target | reason |
|---|---|---|---|---|
| 1. `tests/test_serve_api.py::test_index_sets_security_headers` | [SEC] `GET /` returns 200 with the CSP (`default-src 'self'` ...), `X-Frame-Options: DENY` and an HSTS header. | REWRITE | pytest: the same assertions against `/v2/`, `/legacy` and a static asset under `/v2/`, with the CSP compared byte for byte to `routes.CSP` (gate 2) | Verified: nothing in `src/` sets these headers except the `/` handler (`routes.py:123`; a search of `src/` for `SECURITY_HEADERS`, `Content-Security-Policy`, `add_middleware` and `StaticFiles` finds only `routes.py:37` and `routes.py:123`), so a new static mount would carry none unless header middleware is added. M5_PLAN.md:170 and M5A_BUILD_PLAN.md:106 plan that middleware. This is the only existing pin of those headers. |

## 5. Contracts with no test today

This section lists behaviour of the old page that I checked and found untested. "Untested" means that no test file among `tests/ui/*.mjs`, `tests/test_static_ui.py` and `tests/test_serve_agent_ui.py` calls it or asserts it. I checked with `grep -l <identifier>` over those files for `sessionStorage`, `readStoredWorkspace`, `showEvidence`, `showChanges`, `setStatus`, `loadFreshness`, `uploads_enabled`, `X-Workspace-Token`, `workspace_id` and `403` (no file contains any of them) and for `docHtml`, `renderUploadTarget`, `createWorkspace`, `restoreWorkspace`, `deleteWorkspace`, `finish` and `loadStats` (they appear only in comments, test titles or unrelated identifiers such as `finish_reason`). I also searched `tests/` for the copy in item 6 and for 'risks dropped' and found no match.

1. **The ask stream consumer** (`ask()` and its frame handler): text accumulation on `delta`, `done` replacing the text with the final answer, an `error` event showing its detail, malformed frames being ignored, a non-OK response showing its detail (with the extra Turnstile hint on a 403), the network-error text, the button being enabled again, and the Turnstile token being reset after every ask. The only related checks are a source regex for `step` routing (test_serve_agent_ui row 5) and one fetch count for Ctrl+Enter (a11y row 11). The thin typed client replaces exactly this code (decision 10), so its tests are new tests, not ports.
2. **What an ask sends**: `turnstile_token` in the body; `workspace_id`, `as_of` and the `X-Workspace-Token` header when a workspace is active; the same header on workspace evidence, job and change requests; and `strategy: "hybrid"` on an example click. The upload tests check the document POST's URL and `X-Turnstile-Token` header (upload_flow rows 5 and 11) but no test inspects the body or headers of an ask or an evidence request.
3. **Workspace lifecycle**: create, restore ('workspace not found'), delete; keeping the workspace in `sessionStorage` and restoring it on load (promotion gate 6, "in-flight workspace continuity"); showing the panel only when `uploads_enabled`; a 404 on load dropping the workspace; the `showChanges` request and its error messages. Only the side effects of `setWorkspace(null)` are tested.
4. **The drawer's DOM wiring**: `showEvidence` (title and excerpt set through `textContent`, chip label upgraded once evidence loads), the 'could not load evidence' path, the URL used for workspace evidence, and stale-chip marking in `finish()` (class plus title suffix). Only the pure helpers (`staleClass`, `chipLabel`, `evidenceView`) are tested.
5. **Escaping at DOM boundaries**: `setStatus` (server error text passes through `esc` into `innerHTML`), `docHtml` (the user-chosen upload title in the document list; it is not among the harness exports) and the labels in `renderUploadTarget`. The new page should not leave these to the framework's defaults without a test.
6. **The workspace panel's privacy copy** ('Your documents (private, 24h)' and 'Private to you, never shown to anyone else, and deleted automatically after 24 hours'): no pin. The drop-zone text is pinned (test_serve_agent_ui row 13). The backend side of 'uploads are private' and 'upload text never reaches the agent planner' has tests I did not assess in depth (for example `tests/test_agent_sanitize.py::test_an_uploaded_document_id_never_reaches_the_planner`, `tests/test_serve_workspace_ask.py::test_agent_strategy_with_a_workspace_is_400_before_any_gate`, `tests/integration/test_workspace_leak_neo4j.py`). At page level the only privacy behaviours tested are upload_flow rows 20 and 24.
7. **Failure paths of the header**: `loadStats` failing ('statistics unavailable') and the `loadFreshness` wiring (the line formatter is tested, the wiring is not).

### Weak assertions on contracts that are tested

- The escaping tests badges row 13, evidence row 12 and workspace row 32 use negative regexes only (no `<script`, no `<img`). They also pass if the field is silently dropped, and none asserts that the escaped text is shown. Only chips row 10 and checks row 16 assert that `&lt;script&gt;` is present.
- `test_server_text_of_a_step_never_reaches_innerhtml` checks six named agent functions, not the other places server text reaches `innerHTML` (item 5 above).
- `test_only_the_existing_external_script_and_inline_script_are_used` finds external scripts with a regex that matches only URLs ending in `.js`.
- The drawer focus-return tests (a11y rows 2 to 8) use a synthetic chip with `tabindex="0"` (row 9 uses a non-focusable one). Real chips from `renderMarkdown` have no tabindex, so these contracts have never run against real chip markup (and a keyboard user cannot open the drawer today, as M5_DECISIONS.md section 2.3 gate 3 notes).
- `test_node_unit_tests_pass` skips instead of failing when `node` is missing; CI sets up Node 24, so only local pytest-only runs are affected.

## 6. UNCLEAR rows

**UNCLEAR rows: 0.** The contract of every row could be read from the test body and the code it calls.

Not UNCLEAR, but the class given depends on a design choice that is not made yet. The contract is readable in each case; only the form of the new test depends on the choice:

- chips row 12 and workspace row 3 (`esc`): PORT only if a string `esc` helper survives. With React nodes and no string builder, the escaping contract is carried by chips row 10, badges row 13, evidence row 12 and workspace rows 4 and 32. Do not drop either row without checking that those exist.
- evidence rows 15 and 16 (`dropDocEvidence`): written for a `Map`. If the new evidence cache is not a Map, the assertions change shape.
- upload_flow rows 7 to 10 (`waitForFreshTurnstileToken`): a `unit` target if the helper takes an injected token source, otherwise `RTL`.
- test_static_ui row 4: whether the new page keeps the 'withdrawn' accuracy notice is a copy decision that M5_DECISIONS does not make.
- test_static_ui row 9 and test_serve_agent_ui row 15: how the Turnstile site key reaches a statically served page is not decided in M5_DECISIONS (M5_PLAN.md:170 lists `GET /api/config`, written for the Next.js layout).
- test_static_ui row 1: the wrapper stays for as long as the old page is served (gate 7).
- Every `[edit]` PORT row that goes through `metaHtml`, `statsHtml`, `examplesHtml`, `renderMarkdown`, `evidenceView`, `changeItemHtml` or `changesHtml` assumes the new page asserts wording on rendered text. If the new page keeps any of these as string builders, the `[edit]` disappears for that row.
