# M5b accessibility pass record (A11)

Status: **PARTIAL. A11 is NOT met by this record.** INTERNAL document: it states limits plainly.

## Owner's NVDA pass (release v21, 2026-11-11)

The owner listened with NVDA 2026.3 in Chrome on Windows 11, guided step by step through four scenarios (Ask basics; an answer
and its sources; the Nvidia page; Freshness, Method and Workspace). NVDA's log was on; only the lines it spoke while the
semigraph tab was in front were kept (1,165), the raw log (which records keystrokes) was deleted at once, and every row was
graded from those lines by an Opus grader per scenario, with an Opus re-check of every FAIL.

| # | Req | What NVDA said (short) | Result |
|---|---|---|---|
| Ask-1 | A7 | "Ask · semigraph" | PASS |
| Ask-2 | A7 | "Ask about 13 chip and AI companies., heading, level 1", then "No next heading at level 1" | PASS |
| Ask-3 | A7 | banner; "Main" navigation; main; regions "What an answer looks like", "Or try an example", "Companies covered" | PASS (each region name is said twice: the region, then its heading) |
| Ask-4 | A4 | "Your question, edit, multi line, Control+Enter Meta+Enter, For example: ..." | NOTE: the raw shortcut text is read |
| Ask-4b | A4 | "More optionsDepth: Standard, button, collapsed", "Depth, grouping", radio buttons with their hints | NOTE: no space between "options" and "Depth" |
| Ask-4c | A1, A5 | the example windows of the three stamps were not reached | NOT HEARD |
| Ask-11 | A3, A10 | "Type a question first, or pick an example below." once per press, without moving | PASS |
| Ask-5 | A3 | "Saved answer, shown instantly."; the answer was not read out by itself | PASS with a NOTE: "Looking in the filings..." and "unavailable" first |
| Ask-6 | A1 | "Filing 2026 · paragraph 11, button, collapsed, open evidence 0000002488-26-000018:I.1A:0011" | NOTE: the raw id is read as a description |
| Ask-7 | A1, A5 | "Evidence, complementary landmark, Close evidence, button" (Enter and Space) | PASS |
| Ask-9 | A2, A5 | focus back on the chip, now "AMD 10-K, filed 2026 · paragraph 11" ... "**expanded**" (3 of 3) although the panel is closed | FAIL (state): see N1 |
| Ask-10 | A5 | no "Evidence" landmark while closed | PASS |
| Ask-6b | content | "Sources (2)", "What was checked" | PASS |
| Co-1 | A7 | "Nvidia (NVDA) · semigraph" | PASS |
| Co-2 | A7 | six level 2 headings in order, then "**Compared risk disclosures**"; the On this page link says "Risk changes" | FAIL: see N2 |
| Co-3 | A7 | three navigations with different names (Main, Companies, On this page); regions named | PASS with a NOTE |
| Co-4, Co-5 | A7 | 13 links by name and ticker, current page marked; Enter: focus on "AMD (AMD), heading, level 1" | PASS |
| Co-7, Co-7b | A1, A2, A5 | "$215.9B, Revenue, FY2026 (ended Jan 25, 2026): $215,938,000,000. Open source, button"; Enter opens Evidence with Close evidence; Escape returns | PASS; NOTE: raw id on stamps; row headers "RevenueMoney from sales" run together |
| Co-7c | A7 | "Filings, heading, level 2" | PASS |
| Co-7d | content | the Ask page opens with the box filled, nothing sent; focus at the top of the page | NOTE |
| Co-7e | A4 | "Show exact values, switch, off" -> "on"; the figures change to exact values | PASS |
| Co-8 | content | the Federal Register sentence word for word; each rule "Government rule" | PASS (NOTE: raw rule id read) |
| Co-9 | A7 | "Page not found · semigraph"; h1 "Page not found" | PASS |
| X-5 | A3, A8 | "Freshness:Data as of Sep 24, 2026, 9 newer filings being added, link, Data as of Sep 24, 2026, ..." | NOTE: no space after "Freshness:"; the date line is said twice |
| Fr-1, Fr-2 | A7 | "Freshness · semigraph"; "Freshness, heading, level 1" | PASS |
| Fr-3 | A7 | "Freshness details, list, with 7 items", "ChecksWorking", "Last checked3 h ago", "Next checkin 3 h" | FAIL (wording): see N3 |
| Me-1 | A7 | title not read | NOT HEARD |
| Me-2 | A7 | "Method & Evidence, heading, level 1", then the two level 2 headings | PASS |
| Ws-1 | A7 | "Workspace, heading, level 1" | PASS |
| Ws-2 | A4 | "I have a workspace id and key, button, collapsed", "expanded" (twice), "Workspace id, edit", the key "protected", "Restore, button" | PASS (NOTE: "expanded" twice) |
| Ws-6 (empty) | A4, A10 | "Workspace id, edit, invalid entry, blank" | PASS |

Also heard: the Cloudflare widget says "alert, Success!" after every page change (third party).

**Findings to fix (proposed to the owner, 2026-11-11):**

- **N1 (MEDIUM, A1/A5)**: when the evidence panel closes, the chip that gets focus back is announced "expanded" (3 of 3).
- **N2 (MEDIUM, A7)**: the Company page's last section is headed "Compared risk disclosures" but its On this page link and the guide
  say "Risk changes".
- **N3 (LOW)**: label and value run together when read: Freshness rows ("ChecksWorking", "Next checkin 3 h", heard as
  "check-in") and Key numbers row headers ("RevenueMoney from sales").
- **N4 (LOW)**: missing spaces in two names ("More optionsDepth: Standard", "Freshness:Data as of ..."), and the freshness line
  said twice.
- **N5 (LOW)**: every source button reads its raw citation id ("open evidence 0001045810-...") from its tooltip.

Not heard, to repeat after the fixes: Ask-4c (the three stamps' example windows) and Me-1 (the Method title).

**Owner's decision (2026-11-11), word for word:** "fix first, then switch. (be careful. commit and push)", and about the
re-listen: "don't need to do it. give me final making live commands." So the re-listen is **waived by the owner**: Ask-4c
and Me-1 stay NOT HEARD, and A11 is recorded as **owner-accepted, with the fixes checked in Chrome and not re-heard**, not as
PASS.

**Fixed (each with a failing test first; `web/e2e/screen-reader-text.spec.ts` reads Chrome's own accessibility tree, the text
runs NVDA's browse mode reads):**

- N1: a close that gives focus back happens in two steps: the chip says collapsed at once while the panel and focus on Close
  stay, and one animation frame later the panel hides and focus moves to the chip (chosen then, so an answer that re-rendered
  meanwhile still gets the right chip); an open, a route or workspace change, or the page going away cancels the pending step.
  Checked in Chrome: the chip's `aria-expanded` turns false at least one frame before it gets focus, and focus never falls to
  the page body. Not re-heard with NVDA.
- N2: the heading says "Risk changes", as its On this page link does (the agent's steps list keeps "Compared risk disclosures").
- N3: a label ends in a no-break space, which no layout drops (Freshness rows, Key numbers row headers); found by the review of
  the fixes and fixed the same way: the evidence panel's facts ("SectionItem 1A. Risk FactorsFiled...") and the Freshness page's
  list of companies the check could not look up ("TSMASML", now "TSM, ASML").
- N4: "More options Depth: Standard" (one inline span); "Freshness: Data as of ..." (a no-break space after the hidden prefix),
  and the link has no tooltip, so the date line is read once.
- N5: a chip's hover text is "Open the source"; the raw id is no longer in the tooltip (not re-heard; the panel still shows the id).

## Re-run on the redesign (release v21, 2026-11-11)

Status: **A11 still PARTIAL.** Decision D-R7 (owner: as recommended, option a) asks for the agent's NVDA pass AND about ten
minutes of a person listening to the Ask and Company pages. The agent's part is below; the listening is the owner's.

**What NVDA said (NVDA 2026.3, Chrome, Windows 11, live `https://semigraph.fly.dev/v2/`).** NVDA ran with its log on; only
the "Speaking" lines were kept and the raw log, which also records keystrokes, was deleted at once. On page load NVDA read:
"Ask · semigraph", "banner landmark", "link semigraph", "Main navigation landmark", "list with 3 items", "link current page
Ask", "link Companies", "link Workspace", "link Freshness:Data as of Sep 24, 2026, 9 newer filings being added", "link How it
works", "main landmark", "heading level 1 Ask about 13 chip and AI companies.". That matches Ask-1, the landmark part of Ask-3,
and X-5. After that NVDA said nothing for the scripted steps: keys and focus changes sent by the browser automation are not
operating-system input, so NVDA does not hear them. The rows below are therefore keyboard and focus results, not speech.

**What went wrong first, stated plainly.** The first attempt searched for navigation links with Tab and pressed Enter even when
the search failed. Twice that Enter landed inside the Cloudflare bot-check widget: once on its "Privacy" link (Cloudflare's privacy
page opened) and once while its "Verify you are human" check box had focus. No question was sent, nothing used a bot-check
result, and that run is not counted. The second run focused each target directly, sent a key only when the target had focus,
stopped any Tab walk at an iframe, and brought the test window to the front before each step; it touched nothing of the widget.
NVDA also announced "alert Success!" once at page load: the widget passing on its own, with no input.

**axe (WCAG 2.2 A and AA tags), live, light and dark scheme:** 0 violations on Ask, Nvidia, Freshness, Method, Workspace, the
not-found page, and an answer with its evidence panel open (the bot-check frame excluded as third party).

| # | Req | Observed on v21 (keyboard and focus; speech only where said) | Result |
|---|---|---|---|
| Ask-1 | A7 | NVDA said "Ask · semigraph" | PASS (speech) |
| Ask-3 | A7 | NVDA said banner, "Main" navigation, main landmark; the named regions were not reached by speech | PASS for landmarks; regions by tree at v20 |
| X-5 | A3, A8 | NVDA said "Freshness:Data as of Sep 24, 2026, 9 newer filings being added" as a link in Main, after Workspace | PASS (speech) |
| Ask-4c | A1, A5 | Enter on the "filing passage" stamp opened its example; Escape returned focus to the stamp | PASS |
| Ask-4b | A4 | Enter on More options, Tab into the Depth group, arrow keys move between the radio buttons | keyboard OK; not heard |
| Ask-5 | A3 | Enter on a starter: the answer came from cache, focus stayed on the starter; whether the status line is spoken once was not heard | keyboard OK; not heard |
| Ask-6 | A1 | the first chip reads "Filing 2026 · paragraph 11"; after it was opened once, "AMD 10-K, filed 2026 · paragraph 11" | PASS |
| Ask-7 | A1, A5 | Enter opened the panel with focus on "Close evidence"; Space did the same | PASS |
| Ask-8 | A5 | Tab inside the panel went to "Open the filing on SEC.gov"; no trap | PASS |
| Ask-9 | A2, A5 | Escape closed the panel and focus returned to the chip | PASS |
| Co-5 (nav) | A7 | Enter on Companies in Main: focus on the h1 "Nvidia (NVDA)" | PASS |
| Co-7, Co-7b | A1, A2 | a Key numbers figure is a button named "$215.9B, Revenue, FY2026 (ended Jan 25, 2026): $215,938,000,000..."; Enter opened its evidence, Escape returned focus to it | PASS |
| Co-7c | A7 | Enter on "Filings" in On this page: focus on the h2 "Filings" | PASS |
| Co-7e | A4 | Space on the "Show exact values" switch (a span with the switch role) | NOT SHOWN: reached by keyboard, its state was not read back and nothing was heard |
| Co-5 | A7 | Enter on "AMD (AMD)" in the Companies switcher: focus on the h1 "AMD (AMD)" | PASS |
| Fr, Me, Ws (nav) | A7 | Enter on each Main link: focus on its h1 (Freshness, Method & Evidence, Workspace) | PASS |
| Ws-2 | A4 | Enter on "I have a workspace id and key"; Tab reaches the two fields and "Restore" | keyboard OK; names not heard |
| Ws-6 (empty) | A4 | Restore with both fields empty sends nothing; focus moves to the empty id field | PASS |
| X-1 | A5, A7 | panel open on Ask, then Companies in Main: focus on the h1 "Nvidia (NVDA)" | PASS |

**By the guide's own rule a requirement passes only if every row citing it passes, so A1, A3, A4 and A5 stay open until someone
listens**: the PASS rows above show the keyboard and focus behaviour, not what NVDA says. D-R7 (a) sized the person's part at
about ten minutes on the assumption that the agent's speech log would cover the rest; it covers only the page load. To close
A11 the owner either listens to the speech-dependent rows (Ask-2 to Ask-11, Co-2 to Co-8, a quick pass over Fr, Me and Ws; about
15 minutes with `docs/v2/M5B_NVDA_GUIDE.md`), or accepts in writing this keyboard and focus evidence plus a ten-minute listen to
Ask and Company.

## Release v20 record (2026-11-10)

Spec A11 asks for a keyboard-only walk of every flow and one screen-reader pass with NVDA, by the owner or a person the owner
names. On 2026-11-10 the owner asked the build agent to run the pass on their behalf. What the agent could do, and did:

- **Done: a keyboard-only walk and an accessibility-tree check** of the five pages on the live preview
  (`https://semigraph.fly.dev/v2/`, release v20, 2026-11-10), row by row against `docs/v2/M5B_NVDA_GUIDE.md` section 5.
  A script drove a real Chromium with the keyboard and recorded what the browser exposes to a screen reader: titles, headings,
  landmarks, names of controls, live regions, focus. axe (WCAG 2.2 AA) ran on the live pages in the light and the dark scheme.
- **Not done: the screen-reader pass.** NVDA is not installed on this PC, the agent has no control of the desktop outside a
  browser page, and it cannot hear speech. Nothing below is something NVDA said. What a screen reader is given is necessary
  for a good spoken result, not proof of one (order, verbosity and repetition can only be judged by listening).
- **Not done: anything behind the bot check.** The agent does not pass bot checks. A live question, creating a workspace and
  an upload were not run on the live site (rows marked NOT TESTED). The automated browser tests cover those flows on the
  fixture server (`web/e2e/workspace.spec.ts`, `ask-flows.spec.ts`, `turnstile.spec.ts`), not on the live site.

To close A11 a person still listens with NVDA (about 30 minutes with the guide; this record tells them where nothing was
found, so they can concentrate on how it sounds), or the owner decides in writing to change A11.

Result: 40 PASS, 0 FAIL, 10 NOT TESTED of 50 rows. axe violations on the live pages: 0.

## Findings (none is a failure of an A-item)

1. Ask with an empty question does nothing: no request (0), and the status line keeps its previous text
   ("served from cache - no model call"). A first-time visitor who presses Ask too early gets no feedback.
2. Keyboard order on the Ask page: 57 tab stops in the page content, and the example questions come before
   the question box, so a keyboard user passes every example to reach "Your question". The skip link goes to the top of the
   content, not to the question box. Screen-reader users can jump by form field; sighted keyboard users cannot.
3. The Nvidia company page has 175 tab stops in its content (every citation is a button). It is correct and
   reachable; it is long to walk by Tab alone. Headings and regions give screen-reader users a faster route.
4. A typed question with no bot-check token is refused before any model call, and the refusal reads:
   "Bot check failed - reload the page and try again. (the bot check below has not completed yet - wait a moment and click Ask again)". It is in the status line of the Ask card, in the error colour, set once.

## Rows

| # | Req | Expected (guide) | Observed (keyboard and accessibility tree; not speech) | Result |
|---|---|---|---|---|
| Ask-1 | A7 | title Ask - semigraph | Ask - semigraph | PASS |
| Ask-2 | A7 | one level 1 heading, Ask | Ask | PASS |
| Ask-3 | A7 | banner, navigation Main, main, regions 'Benchmark questions (instant, cached)' and 'Ask the graph', all named | banner 1; navs Main; main 1; regions Benchmark questions (instant, cached) / Ask the graph | PASS |
| Ask-4 | A4 | question box 'Your question', select 'Retrieval strategy', every field named | [{"tag":"TEXTAREA","label":"Your question"},{"tag":"SELECT","label":"Retrieval strategy"}] | PASS |
| X-2a | A7 | first Tab stop is 'Skip to content'; Enter moves to the main area | A:Skip to content -> {"hash":"#main","active":"BODY","id":""} | PASS |
| Ask-5 | A3 | Enter on an example gives the answer; the status region (polite) changes once to its final text; the answer text is not a live region | question "What was Nvidia's total revenue for the fiscal yea"; status <OUTPUT aria-live=polite> texts ["retrieving from the graph...","served from cache - no model call"]; answer aria-live=null role=null | PASS |
| Ask-6 | A1 | each citation chip is a button with a readable name, not a raw id | [{"name":"XBRL revenue FY ended 2024-01-28","id":"xbrl:1045810:revenue:2024-01-28","type":"button","title":"open evidence xbrl:1045810:revenue:2024-01-28"}] | PASS |
| Ask-7 | A1, A5 | Enter and Space open the drawer: a complementary landmark named Evidence; focus on 'Close evidence' | Enter: {"visible":true,"inert":false,"tag":"ASIDE","label":"Evidence","focus":"Close evidence","focusTitle":"close","focusInside":true}; Space opens: true | PASS |
| Ask-8 | A5 | with the drawer open another chip works; the drawer is not a trap | Answer with two different chips. Drawer open on the first: Tab left the drawer after 2 presses; the second chip took focus (True); Enter showed the second chip's evidence (True); the page was not inert (True); Escape returned focus to the second chip (True) | PASS |
| Ask-9 | A2, A5 | Esc closes the drawer and focus returns to the chip that opened it | first chip: true; after the second open: true | PASS |
| Ask-10 | A5 | closed drawer: inert, hidden from the accessibility tree, no 'Close evidence' tab stop | {"inert":true,"ariaHidden":"true","hidden":true}; tab stops inside the closed drawer in a full lap: 0 | PASS |
| Ask-11 | A3, A10 | a refusal appears in the status line under the Ask button (the one live region), said once | empty question: status "served from cache - no model call", 0 request(s); typed question without a bot-check token: "Bot check failed - reload the page and try again. (the bot check below has not completed yet - wait a moment and click Ask again)" (error co... | PASS |
| Ask-12 | A3, A10 | one live paid question through the bot check | needs a person to pass the bot check; not run | NOT TESTED |
| Ask-13 | A3 | research steps list (paid agent ask) | not checked (needs a paid agent ask) | NOT TESTED |
| Ask-14 | A10 note | whether the bot-check widget can be reached and completed with a screen reader | the bot-check script was blocked in this run on purpose; needs a person | NOT TESTED |
| X-1 | A5, A7 | drawer open on Ask, then the Company link: the drawer closes and focus goes to the new page's heading | {"drawerHidden":true,"focus":"H1 Nvidia (NVDA)","title":"Nvidia (NVDA) - semigraph"} | PASS |
| Co-1 | A7 | title Nvidia (NVDA) - semigraph | Nvidia (NVDA) - semigraph | PASS |
| Co-2 | A7 | one level 1 heading; blocks are level 2 headings in order | Nvidia (NVDA); Filings / Metrics / Active risks / Relationships / Federal Register rules / Compared risk disclosures | PASS |
| Co-3 | A7 | banner, navigation Main, main, navigation Companies, six named regions | navs Main, Companies; regions 6 | PASS |
| Co-4 | A7 | 13 company links by name and ticker; the current one is marked current page | 13 links; current: Nvidia (NVDA) | PASS |
| Co-5 | A7 | Enter on AMD (AMD): focus moves to the new level 1 heading, the title changes | {"focus":"H1 AMD (AMD)","title":"AMD (AMD) - semigraph"} | PASS |
| Co-6 | A3, A10 | 'loading...' in a status region while it loads | The company page has a status region; the text 'loading...' was not captured on a fast load, so the spoken-once part is not shown here | NOT TESTED |
| Co-7 | A1, A2, A5 | a chip is a button with its label; Enter opens the drawer with focus on 'Close evidence'; Escape returns focus to the chip | [{"where":"Relationships","name":"Item 1A (para) 0007","role":"button","focusOnOpen":"Close evidence","focusBack":true},{"where":"Compared risk disclosures","name":"Item 1A (para) 0020","role":"button","focusOnOpen":"Close evidence","focusBack":true}] | PASS |
| Co-10 | content | hedged labels, never 'removed'; a pair that could not be compared says why; passages start with their filing | labels present: No matching risk factor found in the earlier filing / Reworded; the word 'removed' appears as a label: false; 'comparison not available': false | PASS |
| Co-8 | content | the keyword-match sentence above the rules list | Federal Register rules The links to export-control rules are keyword matches to external Federal Register rules: outside events, not statements the company made. AMD AF | PASS |
| Co-9 | A7 | made-up ticker: title 'Page not found - semigraph', one h1 'Page not found', a link 'Back to the start page' | Page not found - semigraph; h1 Page not found; back link 1 | PASS |
| Fr-1 | A7 | title Freshness - semigraph | Freshness - semigraph | PASS |
| Fr-2 | A7 | one level 1 heading; banner, navigation Main, main | Freshness; navs Main | PASS |
| Fr-3 | A7 | a list named 'Freshness fields'; each row reads as label then value | {"tag":"UL","rows":["Statusok","Data as of2026-09-24","Last checked4 h ago (2026-11-10T03:49:59.945788+00:00)","Next checkin 2 h (2026-11-10T09:49:59.945788+00:00)","Filings pending9","AMD 10-Q filed 2026-11-04 - 0000002488-26-000191INTC 10-Q filed 2026-10-... | PASS |
| Fr-4 | A3, A10 | loading once then the data, or the error text | A status region exists and is empty once the data is shown; the loading text was not captured on a fast load | NOT TESTED |
| Fr-5 | A8 | status is words, never only a colour | Statusok | PASS |
| Fr-6 | A7 | only the header links are tab stops; no trap | 6 stops; controls inside main: 0 | PASS |
| Me-1 | A7 | title Method & Evidence - semigraph | Method & Evidence - semigraph | PASS |
| Me-2 | A7 | one h1 then the two h2 in order, no skipped level | H1 Method & Evidence > H2 What the checks mean > H2 How reliable are change claims? | PASS |
| Me-3 | A7 | banner, navigation Main, main, two named regions | What the checks mean / How reliable are change claims? | PASS |
| Me-4 | note | links plan, review, Freshness: can you tell where they go from the sentence around them | [{"text":"plan","target":"_blank","href":"https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-g","sentence":"Accuracy figures shown on earlier versions of this page are withdrawn: the benchmark behind them could not detect false "dropped ri... | PASS |
| Me-5 | content | reads as sensible prose; says plainly that earlier figures are withdrawn | Accuracy figures shown on earlier versions of this page are withdrawn: the benchmark behind them could not detect false "dropped risk" claims. | PASS |
| Me-6 | A7 | Enter on Freshness: same tab, focus on the new level 1 heading | H1 Freshness | PASS |
| Ws-1 | A7 | title Workspace - semigraph; one h1; banner, navigation Main, main | Workspace - semigraph; h1 Workspace; navs Main | PASS |
| Ws-2 | A4 | restore fields 'workspace id' and 'token' (a password field); the fields of an open workspace need one | fields [{"type":null,"label":"workspace id"},{"type":"password","label":"token"}]; buttons ["Create a workspace","Restore"] | PASS |
| Ws-3 |  | open a workspace (bot check) | needs an open workspace, which needs a person to pass the bot check; not run (covered by the automated browser tests on the fixture server) | NOT TESTED |
| Ws-4 | A6 | drop zone is a named button; Enter and Space open the picker | needs an open workspace, which needs a person to pass the bot check; not run (covered by the automated browser tests on the fixture server) | NOT TESTED |
| Ws-5 | A3 | upload progress spoken through the status region | needs an open workspace, which needs a person to pass the bot check; not run (covered by the automated browser tests on the fixture server) | NOT TESTED |
| Ws-7 | content | examples disabled while a workspace is open | needs an open workspace, which needs a person to pass the bot check; not run (covered by the automated browser tests on the fixture server) | NOT TESTED |
| Ws-8 | A1, A2, A5 | chip in the what-changed view | needs an open workspace, which needs a person to pass the bot check; not run (covered by the automated browser tests on the fixture server) | NOT TESTED |
| Ws-6 | A3, A10 | a refusal on Restore is next to the fields and announced | [{"text":"workspace not found","live":null,"nearRestore":true}] | PASS |
| X-2 | A7, A9 | one full Tab lap per page: focus always visible, you can always leave | Fresh load, then Tab until focus left the page. ask: 63 stops (57 in the page content), first 'Skip to content', 0 unnamed, 0 without a visible ring, 0 off screen, 0 inside the closed drawer, lap completed; company-NVDA: 181 stops (175 in the page content),... | PASS |
| X-3 | A9 | at 200% zoom (640 px wide): single column, nothing cut off | ask: sideways 0 px; company/NVDA/: sideways 0 px; freshness/: sideways 0 px; method/: sideways 0 px; workspace/: sideways 0 px | PASS |
| X-4 | A8 | dark and light: text readable, status not by colour alone | axe (WCAG 2.2 AA, both schemes) on the live pages: ask-with-error:light 0, ask-with-error:dark 0, company-AMD:light 0, company-AMD:dark 0, freshness:light 0, freshness:dark 0, method:light 0, method:dark 0, workspace:light 0, workspace:dark 0 | PASS |
| X-5 | A3, A8 | one plain freshness line in the banner: not a link, not announced by itself | [{"text":"semigraph","live":null,"role":null,"isLink":false},{"text":"Data as of 2026-09-24 - checked 4 h ago - 9 filings pending","live":null,"role":null,"isLink":false}] | PASS |

## By requirement

| Req | What | Keyboard and tree | Screen reader (NVDA) | Pages |
|---|---|---|---|---|
| A1 | Citation chips are named buttons, Enter and Space work | PASS | not done | Ask, Company |
| A2 | Focus returns to the right chip on close | PASS (the replaced-answer case is covered by `web/e2e/ask-drawer.spec.ts`, not rerun live) | not done | Ask, Company |
| A3 | One polite status region announces answer-ready, errors and upload states | PASS for Ask (cached answer, refusal); upload states NOT TESTED live | not done | Ask |
| A4 | Every control has a label | PASS for Ask and the Workspace start controls; the open-workspace controls NOT TESTED live | not done | Ask, Workspace |
| A5 | The drawer is a named, non-modal landmark; closed it is out of the tab order | PASS | not done | Ask, Company |
| A6 | The drop zone works from the keyboard | NOT TESTED live (needs an open workspace) | not done | Workspace |
| A7 | Route focus, titles, landmarks | PASS | not done | all five |
| A8 | Contrast in both schemes; status not by colour alone | PASS (axe on the live pages, both schemes: 0 violations) | n/a | all five |
| A9 | Nothing moves; single column when narrow | PASS at 640 px (a 1280 px window at 200% zoom) and at 390 px: no sideways scroll | n/a | all five |
| A10 | Error text next to its control and announced | PASS for Ask (bot-check refusal) and Workspace Restore ("workspace not found") | not done | Ask, Workspace |
| A11 | One keyboard walk and one NVDA pass by a person | keyboard walk: done by the agent for the free flows; NVDA pass: NOT DONE | NOT DONE | - |

## How it was run

Chromium (Playwright 1.65), 1280x900, with the bot-check host unresolvable so that no bot check was loaded or passed. Requests:
page loads, four cached example questions (free), evidence reads, one Restore with made-up values (answered "workspace not
found"), and one typed question that the server refused for the missing bot-check token (no model call, no cost).
