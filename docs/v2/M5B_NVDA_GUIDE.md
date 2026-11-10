# M5b I5: the screen-reader pass, step by step (NVDA)

Status: INTERNAL working guide for the owner. It states limits plainly. Do not copy it into the README or the site.
Spec: `docs/specs/m5b-frontend.md` (A1-A11, D14, D15, I5 in the increment table, B6/P8 in the gate map).
Written 2026-11-08 and refreshed the same day against the I3 pages (Ask, Workspace, the evidence drawer, the header line)
as they are in `web/src`. The labels below are the ones the code carries; if NVDA says something different, write down what
it said and compare it with the meaning in the "Expect" column. A label in a table that was changed after the I3 build is
a finding about this guide, not about the page.

## 1. What this pass is for, and how long it takes

Automated checks (axe, Lighthouse, Playwright) cannot catch everything a blind or low-vision user meets. Spec A11 asks for
one human pass: a keyboard-only walk of every flow, plus one screen-reader pass. This guide is both in one sitting.

- You will listen to the five pages under `/v2/` with NVDA, a free screen reader for Windows, and tick a table.
- Time: 30-45 minutes once NVDA is installed. Add 10 minutes the first time, for the install and for getting used to the voice.
- You do not need to be good at it. If something sounds confusing to you, that is a finding, even if you cannot say why.
- This pass covers requirements A1-A7 and A10 directly, and A8/A9 only by ear and by eye (see 6). It does NOT replace the
  automated gates and it is NOT the P8 persona walkthrough or any buyer feedback. See section 8.

Money: Section 5 uses only the free, cached example questions and read-only pages. One optional live question is described
in 5.1 (row Ask-12). A live question is a paid call, goes through the Turnstile bot check in your browser, and counts
against the $5 ceiling of D14 (the spec's cost section puts a typical live answer at about $0.001-0.002). You do not need
to ask more than one. Creating a workspace and uploading a file (5.5) make no paid model call.

## 2. Install NVDA

1. Open your browser and type `https://www.nvaccess.org/download/` in the address bar (this is NV Access, the makers of
   NVDA). The page may first ask for a donation: a donation is optional, and the page offers a way to download without one
   (wording varies, for example "Skip donation this time"). Do not download NVDA from any other site or from a link
   someone sends you.
2. Save the file (it is named `nvda_` followed by a version number, ending in `.exe`). Open it from the browser's downloads list. If Windows asks "Do you
   want to allow this app to make changes?", choose Yes.
3. NVDA starts talking and shows its licence window. Read it; if you agree, tick "I have read and agree to the license
   agreement". Then choose one of the buttons (both are fine for this pass):
   - "Continue running": a temporary copy; nothing is installed, and quitting NVDA leaves nothing behind. Easiest to undo.
   - "Install NVDA on this computer": permanent. Pick this only if you want NVDA again later.
   Recommended: "Continue running". (The third button, "Create portable copy", is not needed.)
4. A "Welcome to NVDA" window may appear. Choices (verify the wording on your screen; versions differ):
   - Keyboard layout: pick "Desktop" if your keyboard has a numeric keypad; pick "Laptop" if not. The layout only changes
     some shortcuts (see section 3); it does not change which key is the NVDA key.
   - "Use CapsLock as an NVDA modifier key": tick it if your keyboard has no Insert key, or if you prefer Caps Lock.
     Ticking it is harmless: Insert keeps working as the NVDA key too.
   - "Start NVDA after I sign in" (older versions: "log on"): leave UNticked (not needed for this pass; may be missing on
     a temporary copy).
   - "Show this dialog when NVDA starts": untick if you like. Then press the OK button (Tab to it, press Enter).
5. If a "Usage statistics" or "Help improve NVDA" question appears, choose the privacy-preserving answer (No / Don't allow).
6. Slow the voice down (the default is fast):
   - Press NVDA key + Ctrl + V to open the Speech settings. If nothing opens, press NVDA key + N, then Preferences, then
     Settings, then choose "Speech" in the list on the left.
   - Tab to "Rate" and press the Left Arrow key a few times (or Page Down for bigger steps) until it is comfortable, for
     example about 40. Then Tab to OK and press Enter.
7. Optional but strongly recommended: show what NVDA says as text, so you can copy it into your notes.
   NVDA key + N, then Tools, then "Speech Viewer". A small window shows every phrase. Move it to a screen corner.
   Do not rely on Speech Viewer alone: listen too.
8. Quit and stop:
   - Stop the voice mid-sentence: press Ctrl (the left or right one). This stops speech only; it does not turn NVDA off.
   - Quit NVDA: press NVDA key + Q, then press Enter on the "Exit NVDA" window.
   - Pause and resume speech: Shift.

The "NVDA key" is Insert (either the main Insert key or the 0/Ins key on the number pad), and also Caps Lock if you ticked
that option. This is the same in both keyboard layouts. In this guide "NVDA+" means "hold the NVDA key and press".

## 3. The few keys you need

In a web page NVDA has two modes. Browse mode (the default on a page): the arrow keys read the page and single letters jump
around; typing does nothing. Focus mode: your keys go to the control you are on (a text box, a select list). NVDA usually
switches by itself (a short low or high beep or a spoken "browse mode"/"focus mode"); NVDA+Space switches by hand.

| Key | What it does |
|---|---|
| NVDA key | Insert (main or number pad), and Caps Lock if you ticked that option |
| Tab / Shift+Tab | Next / previous control (link, button, field). This is the keyboard-only walk |
| NVDA+Space | Switch browse mode and focus mode by hand |
| Down / Up arrow (browse mode) | Read the next / previous line |
| NVDA+Down arrow | Read everything from here ("say all"). Laptop layout: NVDA+A. Stop with Ctrl |
| NVDA+Up arrow | Say the current line again. Laptop layout: NVDA+L |
| H / Shift+H | Next / previous heading (1-6 jump to that heading level; 1 jumps to level 1) |
| L / Shift+L | Next / previous list |
| D / Shift+D | Next / previous landmark (banner, navigation, main, complementary...) |
| K / Shift+K | Next / previous link |
| B / Shift+B | Next / previous button |
| F / Shift+F | Next / previous form field |
| E / Shift+E | Next / previous edit (text) field |
| NVDA+F7 | Elements list: a window with a type choice at the top (Links, Headings, Form fields, Buttons, Landmarks). Esc closes it |
| Enter or Space | Activate the link or button you are on |
| Esc | Close something that is open (the evidence drawer should close on Esc) |
| NVDA+Tab | Say what has focus right now |
| NVDA+T | Read the window title (the browser tab name; this is how you check the page title) |
| Ctrl | Stop speaking |

Notes on keys. These are the standard NVDA commands from the NVDA User Guide (written and reviewed from knowledge, not
checked live in this session). If a key does not do what the table says, check it with NVDA's own help: press NVDA+1
(input help on), press the key to hear what NVDA says it does, press NVDA+1 again to leave. Or open NVDA key + N, Help,
User Guide (its "Commands Quick Reference" lists every key).
1. In laptop layout, say-all is NVDA+A and "current line" is NVDA+L. Every other key in the table is the same in both layouts.
2. If NVDA+Ctrl+V does not open the Speech settings, use the NVDA menu route in step 2.6.
3. In Chrome and Firefox, "D" may land on landmarks that are not named. The code names the top `nav` "Main", the company
   switcher "Companies" (Company page), and the drawer "Evidence".
4. NVDA may announce "clickable" or "link" extras that depend on its settings. Judge meaning, not the exact extras.

## 4. Which browser, and the Narrator fallback

Use Chrome or Firefox on Windows 11 (D15). Both work well with NVDA.
Pick ONE and write it in the result table. If you open the browser after starting NVDA you avoid a known snag: if a page
was already open when NVDA started and NVDA seems silent in it, reload the page (F5) or restart the browser.

Address to test: the preview deploy's `/v2/` path, https://semigraph.fly.dev/v2/ (it exists once the I4 preview deploy is
done; if you get "not found" or the old page, stop and tell the build session). The old page at `/` is not part of this pass.

Fallback only if NVDA cannot be installed or does not work: Narrator with Edge (built into Windows 11). Its keys, which I
am less sure of, so check with Narrator's own list: press Narrator key + F1 for the command list (Narrator key is Caps
Lock or Insert).

| Key | What it does |
|---|---|
| Win+Ctrl+Enter | Start or stop Narrator |
| Narrator key + Esc | Quit Narrator (verify; Win+Ctrl+Enter also toggles it) |
| Caps Lock + Space | Turn "scan mode" (the equivalent of browse mode) on or off |
| H, D, K, B, F in scan mode | Headings, landmarks, links, buttons, form fields (verify in the command list) |
| Tab / Shift+Tab | Controls, as in NVDA |
| Ctrl | Stop speaking |

If you use Narrator, tick "Narrator (Edge)" in the result table and expect the wording to differ from NVDA's more than
usual. Narrator announces live regions less consistently; a miss there is a note, not automatically a fail.

## 5. The walk: what to do on each page

Start NVDA first, then the browser, then the `/v2/` address. For every page do the same short routine:

1. After the page loads, press NVDA+T. Write down the title.
2. Press Ctrl to stop the speech. Press `1` to jump to the first level 1 heading, then press `1` again: there should be
   no second one (exactly one level 1 heading per page).
3. Press D repeatedly. Write down each landmark name you hear.
4. Put the keyboard only to work: from the top of the page, press Tab until you have reached everything. Check you can always
   get out (no trap) and you never hear the same text twice in a row.
5. Then do the page-specific items below.

Expected landmarks on every page (A7): banner (the header), navigation named "Main", main. The first Tab stop is a "Skip to
content" link; Enter on it moves you to the main area. The top navigation links are Ask, Company, Workspace, Freshness,
Method & Evidence; the current page's link is announced as "current page" (or similar). Under the navigation the banner
carries one plain line of text about the data's freshness (see X-5); it is not a link or a control and is not announced by
itself. The Turnstile bot-check box, when it is shown, sits after the main area at the bottom of the page, outside the landmarks.

Page titles come from the code as "<page name> - semigraph" (the code uses a middle dot between the two parts; NVDA may say "dot" or nothing).

### 5.1 Ask  (`/v2/`)

Labels in the code (`web/src/copy/deck.ts` (Ask.copy.ts until R4), `Ask.tsx`, `components/EvidenceDrawer.tsx`, `components/Steps.tsx`): the
level 1 heading is "Ask"; two named sections follow, "Benchmark questions (instant, cached)" (the examples) and "Ask the
graph" (the question and the answer, a level 2 heading that takes focus when an answer is replaced under an open drawer); a
box labelled "Your question"; a select named "Retrieval strategy" (options "hybrid GraphRAG (graph + XBRL + text-checked risk
changes + vector search)" and "vector-only: comparison baseline, no temporal reasoning", plus "Deep research (agent): plans
extra lookups first" only when the server offers it); an "Ask" button; and one status line that is a live region.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Ask-1 | NVDA+T on arrival | "Ask - semigraph" | A7 | |
| Ask-2 | Press 1, then count level 1 headings | One level 1 heading, "Ask". After navigating from another page via the nav, focus lands on this heading and NVDA reads it | A7 | |
| Ask-3 | Press D through landmarks | banner, navigation "Main", main, and inside main two named regions: "Benchmark questions (instant, cached)" and "Ask the graph". Any other region has a name | A7 | |
| Ask-4 | Press E (edit field), F (next field) | The question box is announced as "Your question" (not "edit", not blank). The strategy select is announced as "Retrieval strategy". Every field you reach has a name | A4 | |
| Ask-5 | Press Tab to reach the example questions (cached, free) and press Enter on one | The answer appears. NVDA announces something like "answer ready" through the status region, once. The answer text itself is NOT read aloud automatically while it streams | A3 | |
| Ask-6 | Press B to walk the buttons in the answer | Each citation chip is a button with a readable name, not a raw id. The code's labels: a filing passage "Item 1A" + paragraph mark + number, sometimes with the form and year first ("10-K FY2025 ..."; NVDA may say "pilcrow" or "paragraph" for the mark); a number "XBRL revenue FY ended 2026-01-25"; a rule "BIS rule 2025-19001" | A1 | |
| Ask-7 | On a chip press Enter. Repeat with Space | The evidence drawer opens. NVDA announces a landmark named "Evidence" (complementary). Focus moves to a close button named "Close evidence" (its tooltip says "close") | A1, A5 | |
| Ask-8 | With the drawer open, Tab out of it to another chip in the answer and press Enter | The other chip works; the drawer is not a trap; the page behind it was not made unreachable | A5 | |
| Ask-9 | Press Esc in the drawer | The drawer closes. NVDA focus returns to the chip you opened it from. (If the answer was replaced meanwhile, focus goes to the answer heading instead.) | A2, A5 | |
| Ask-10 | With the drawer closed, Tab through the page | Nothing in the closed drawer is reachable: no stray "Close evidence" stop, no "Evidence" landmark in the D list | A5 | |
| Ask-11 | Refusal: submit an empty question, or a question over the limit if you can, or any message the page shows when it refuses | The error text appears in the status line under the Ask button (the one live region) and NVDA says it without you moving. Not announced twice | A3, A10 | |
| Ask-12 | Optional, one live question (paid; see Money in section 1). Solve the Turnstile check yourself in the browser, ask one short question, wait | Progress, "answer ready" and any error are spoken through the status region. If the stream is cut, the "cut" message is spoken | A3, A10 | |
| Ask-13 | The research steps list. Normally SKIP: it only appears for an "agent" ask, the most expensive kind of paid ask. Write "not checked (needs a paid agent ask)" | If you did run one: new steps are announced politely as they appear; the list is named "What the research agent did" | A3 | |
| Ask-14 | Turnstile bot check (a third-party widget, after the main area at the bottom of the page) | Note whether NVDA can reach it with Tab. If you cannot complete it with NVDA, record that as a finding. Do not try to bypass it. This is Cloudflare's widget, not ours; the finding is still worth writing | A10 note | |

### 5.2 Company  (`/v2/company/NVDA/`, then another company)

Heading: the company name and ticker, for example "Nvidia (NVDA)". Switcher: a navigation named "Companies" with 13 links,
the current one marked as the current page. Blocks (level 2 headings, each a named region): Filings, Metrics, Active risks,
Relationships, Federal Register rules and "Compared risk disclosures" (the recent changes between filings; it appears once the
company data has loaded, and says "loading..." until its own data arrives). Under that last heading each pair of filings has a
level 3 heading such as "10-K 2025-02-26 -> 10-K 2026-02-25", then labels: "No matching risk factor found in the earlier
filing", "No longer appears as a separate risk factor", "Not matched", "Reworded". A pair that could not be compared reads
"<pair>: comparison not available (<reason>)." A line "Data as of <date>" may appear. A loading line near the top says
"loading..." and is a status region. The citation ids on this page (including those under "Compared risk disclosures") are
evidence chips: buttons that open the Evidence drawer, as in the Ask answers and the Workspace what-changed view.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Co-1 | NVDA+T | "Nvidia (NVDA) - semigraph" | A7 | |
| Co-2 | Press 1; press H through the others | One level 1 heading (company name). Blocks are level 2 headings, in the order above, each as a named region | A7 | |
| Co-3 | Press D | banner, navigation "Main", main, then inside main a navigation "Companies" and the six named regions (Filings, Metrics, Active risks, Relationships, Federal Register rules, Compared risk disclosures). The two navigations have different names | A7 | |
| Co-4 | Press K through the switcher | 13 company links by name and ticker; the current one says current page | A7 | |
| Co-5 | Activate "AMD (AMD)" with Enter | The page changes; NVDA reads the new level 1 heading "AMD (AMD)" (focus moved there); the title changes. You did not have to hunt for the new page | A7 | |
| Co-6 | While it loads | "loading..." is spoken once (status region). If an error appears it is spoken through the same region; a company answer the page cannot read reads "could not load the company data" | A3, A10 | |
| Co-7 | Tab to a citation chip under Relationships or under "Compared risk disclosures", press Enter, then Escape | Each chip is announced as a button with its label (for example "Item 1", then the paragraph sign and "0320"); Enter opens the Evidence drawer with focus on "Close evidence"; Escape closes it and focus returns to the same chip | A1, A2, A5 | |
| Co-10 | Read "Compared risk disclosures" (press H, then arrow down) | The labels read as hedged statements ("No longer appears as a separate risk factor", never "removed"); a pair that could not be compared says so with its reason; each quoted passage of a reworded risk starts with its filing, for example "10-K 2025-02-26: " before the older wording and "10-K 2026-02-25: " before the newer | content | |
| Co-8 | Read the Federal Register rules block | The line above the list says "The links to export-control rules are keyword matches to external Federal Register rules: outside events, not statements the company made." NVDA reads it | content | |
| Co-9 | Type `/v2/company/XXXX/` (a made-up ticker) in the address bar | Title "Page not found - semigraph"; one level 1 heading "Page not found"; a link "Back to the start page". This is a fresh page load, so focus starts at the top, not on the heading (that is expected) | A7 | |

### 5.3 Freshness  (`/v2/freshness/`)

Heading "Freshness". A status line ("loading..." or an error) is a status region. Then a one-line summary (for example
"Data as of <date> - 2 filings pending"), then a list named "Freshness fields" of
label and value pairs: Status, Data as of, Last checked, Last failed check, Next check, Filings pending, Federal Register,
Unresolved; and a list "Pending filings" when there are any. Rows with no value are left out.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Fr-1 | NVDA+T | "Freshness - semigraph" | A7 | |
| Fr-2 | Press 1; D | One level 1 heading; banner, navigation "Main", main | A7 | |
| Fr-3 | Press L (list) or read with the Down arrow | The list is announced as "Freshness fields" with its item count; each row reads as label then value, for example "Last checked 3 h ago (2026-...)" | A7 | |
| Fr-4 | Watch the loading state | "loading..." once, then the data; or the error text, spoken. Nothing repeated | A3, A10 | |
| Fr-5 | Status meaning | Status is words, never only a colour (it is read to you, so if you hear a word it passes) | A8 | |
| Fr-6 | Tab through the page | Only the header links are tab stops (it has no controls of its own); no trap | A7 | |

### 5.4 Method & Evidence  (`/v2/method/`)

Heading "Method & Evidence". An opening paragraph; a short unheaded note that earlier accuracy figures are withdrawn (it ends
with the links "plan" and "review", which open GitHub pages in a new tab); a link "Freshness" (same tab, to the Freshness
page); then two level 2 sections (each a named region): "What the checks mean" and "How reliable are change claims?". The
page makes no requests of its own.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Me-1 | NVDA+T | "Method & Evidence - semigraph" (NVDA may read "&" as "and") | A7 | |
| Me-2 | Press 1, then H | One level 1 heading, then the two level 2 headings, in order, no skipped levels | A7 | |
| Me-3 | Press D | banner, navigation "Main", main, plus the two named regions | A7 | |
| Me-4 | Press K | After the top navigation links: "plan", "review" and "Freshness". Judge whether, with the sentence around them, you can tell where they go; if not, write it as a Note (link wording is not one of the A-items) | note | |
| Me-5 | Say-all (NVDA+Down) | The page reads as sensible prose: the unheaded note says plainly that earlier figures are withdrawn, as the spec requires. Stop with Ctrl | content | |
| Me-6 | Press Enter on "Freshness" | The Freshness page opens in the same tab; NVDA reads its level 1 heading "Freshness" (focus moved there) | A7 | |

### 5.5 Workspace  (`/v2/workspace/`)

The page shows its content only when the server has uploads switched on (`/api/stats`); if you see just the heading
"Workspace", write "uploads off" and skip the rest (not a pass). Labels in the code (`Workspace.copy.ts`, moved in R4 to `deck.workspace` in `web/src/copy/deck.ts`): a section
"Your documents (private, 24h)" with a "Create a workspace" button, restore fields "workspace id" and "token" (the token
field is a password field), a "Restore" button, a status line; once a workspace is open: a "Delete workspace" button, a date
field named "Ask as of this date instead of the current version", a select labelled "upload as:" (first option "Upload a
new document"), and one drop-zone button whose text starts "Drag a PDF, DOCX, Markdown, TXT or HTML file here". There is no
separate "choose file" button: the drop zone is the real button and Enter or Space on it opens the file picker.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Ws-1 | NVDA+T; press 1; D | "Workspace - semigraph"; one level 1 heading; banner, navigation "Main", main | A7 | |
| Ws-2 | Press F (or E) through the fields | The restore fields are announced as "workspace id" and "token"; the date field as "Ask as of this date instead of the current version"; the upload target select as "upload as:". None is "edit" or blank. The file input (hidden) is named by the drop-zone text | A4 | |
| Ws-3 | To reach the rows below you need an open workspace. Creating one and uploading a file make no paid model call (no money), but each needs the Turnstile check, and each upload counts toward the server's daily upload limit. Asking a question about the uploaded file IS a paid live ask (D14, ceiling $5); you do not need one here. Use a tiny text file you do not mind sharing (persona walkthrough task 6.4 shows how to make one), and click "Delete workspace" afterwards | | | |
| Ws-4 | Tab to the drop zone | It is announced as a button named by its text ("Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one ..."); Enter and Space open the file picker; there is no second button for it | A6 | |
| Ws-5 | After an upload starts | Progress ("uploading...", "queued", "checking", "reading the document", "splitting into sections", "embedding", "comparing with the previous version", "saving", then "ready" or "failed"; on screen "uploading" ends in an ellipsis character, "embedding" adds a count and the time left, and "failed" adds the reason; if the bot-check settings could not be loaded the row reads "could not load the bot check - reload the page and try again", and if they are still loading the upload waits up to 15 seconds for them) is spoken through the status region, once per change | A3 | |
| Ws-6 | A refusal (for example a wrong token on Restore) | The error is next to the field and is spoken without moving | A3, A10 | |
| Ws-7 | While a workspace is open, the examples on Ask | The example buttons are disabled and the strategy select offers only the "hybrid GraphRAG ..." option | content | |
| Ws-8 | Optional: after an upload, open the what-changed view of a second version (the version buttons are named "<document>: v2") and press Enter on a chip in it | The same drawer opens ("Evidence"), Esc closes it and focus returns to that chip | A1, A2, A5 | |

### 5.6 Across pages (3 minutes)

| # | Do | Expect | Req |
|---|---|---|---|
| X-1 | Open the drawer on Ask, then use the "Company" link | The drawer closes and focus goes to the new page's heading (not back to the chip) | A5, A7 |
| X-2 | Press Tab for one full lap on any page | Focus is always visible (you can see a ring); you can always leave; the Skip link works | A7, A9 |
| X-3 | Make the browser window narrow (drag its edge), or zoom in: hold Ctrl and press + (plus) until the zoom shows 200%. Ctrl+0 resets it | Single column; nothing is cut off; you can still reach everything | A9 |
| X-4 | Windows dark mode on, then off (Settings, Personalization, Colors, "Choose your mode": Dark, then Light) | Text stays readable in both; status words never depend on colour alone | A8 |
| X-5 | On any page, read the line under the main navigation in the banner (Down arrow from the nav) | One plain line, for example "Data as of <date> - checked <n> min ago - <n> filings pending" (the words are joined by a middle dot on screen), or "freshness check unavailable" when the monitor is off, or "freshness check failed <n> min ago"; if the freshness service cannot be reached, or answers with an error, the line reads "freshness check unavailable" (the old page showed that line only for an error answer, and no line at all when the service could not be reached). It is read once per page load, it is not a link, not announced by itself, and not repeated on route changes | A3, A8 |

A8 (contrast) and A9 (nothing moves) are checked by the automated audits. Here, just note anything that looks wrong to you.

## 6. Result table and how to record it

Copy this table into a NEW file `docs/v2/M5B_A11Y_MANUAL_PASS.md` (the file A11 names; ask Claude to create it from your
notes if you prefer). One row per requirement.

| Req | What | Pass / Fail / Not built | Pages checked | Notes (exact words NVDA said) |
|---|---|---|---|---|
| A1 | Citation chips are named buttons, Enter and Space work | | Ask, Company | |
| A2 | Focus returns to the right chip on close (and to the heading if the answer was replaced) | | Ask, Company | |
| A3 | The status region speaks progress, errors, cut streams; the streamed text is not read while it streams | | Ask, Workspace, Company, Freshness | |
| A4 | Every control has a spoken name | | Ask, Workspace | |
| A5 | Drawer: landmark "Evidence", focus to close button, Esc closes, no trap, nothing reachable when closed | | Ask, Company | |
| A6 | Drop zone works from the keyboard; a real button exists | | Workspace | |
| A7 | Title, one level 1 heading, landmarks, focus moves on route change | | all five | |
| A8 | Status not conveyed by colour alone (by ear and eye; the audits do contrast) | | all | |
| A9 | Single column when narrow; nothing moves | | all | |
| A10 | Error text next to its control and spoken once | | Ask, Workspace, Company, Freshness | |

Also record at the top: date, the person, Windows 11, NVDA version (NVDA key + N, Help, About), browser and version, the
speech rate, whether Speech Viewer was used, and the address tested. Rule of thumb: a requirement passes only if every row in
section 5 that cites it passes. A "Not built" is not a pass; I5 stays open until it is re-run on the built page.

Put one line in the spec Status for I5, in `docs/specs/m5b-frontend.md`: "A11: NVDA pass <date>, <result by A-item>,
record at docs/v2/M5B_A11Y_MANUAL_PASS.md". Claude should make that edit (the spec is under the I3 build's hands now, so
do it after that build finishes). Do not mark I5 done from this pass alone: the owner's written B6 sign-off and P8 are
separate gates.

## 7. If something fails

Stop. Do not fix it yourself and do not tick it. For each failure, write five lines in the Notes column, or in a block under
the table:

1. Page and address (for example `/v2/company/NVDA/`).
2. The step (the row number from section 5, and what you pressed, in order).
3. What NVDA said, word for word (copy it from Speech Viewer), or "silence".
4. What was expected (copy the Expect cell).
5. Browser, NVDA version, and whether it happens every time (try it twice).

A short screen recording or a screenshot of Speech Viewer helps. Then hand the file to Claude, who will turn each failure
into a failing test first and fix it, then ask you to repeat only the failed rows. After 3 rounds with failures still open,
bring them to the owner for a decision (SDD rule).

If NVDA says something odd but the page still works (extra words, "clickable"), write it as a Note, not a Fail.

## 8. What this pass does not do

- It is not P8. Owner 2026-11-08: no named buyers available; at I5, P8 is met by an owner-run persona walkthrough
  (`docs/v2/M5B_PERSONA_WALKTHROUGH.md`), recorded as persona walkthrough; real-buyer feedback pending. That walkthrough is a
  separate document and is not buyer feedback: never write that P8 was met by buyers. This guide does not cover it.
- It is not a legal accessibility conformance claim: one person, one screen reader, one browser, one afternoon.
- It does not test the old page at `/`.

## 9. Things in this guide I am not certain of

- The keys are the standard ones from the NVDA User Guide, reviewed from knowledge but not checked live in this session;
  input help (NVDA+1) settles any doubt in seconds.
- The exact wording of the download page, the licence window buttons, the Welcome dialog options and the Exit window on
  the NVDA version you download.
- Narrator's key list: Caps Lock+Space for scan mode, Narrator+Esc to quit, and that H/D/K/B/F work as in NVDA (check
  with Narrator key + F1).
- The labels of the Ask, Workspace, evidence drawer, Company, Freshness, Method, header line, menu and skip-link controls
  were read from the current code in `web/src` on 2026-11-08 (after the I3 build); none was heard through NVDA by the
  author. If a later change moves a label, trust what NVDA says and note the difference.
- Whether Turnstile's challenge is operable with a screen reader: unknown until you try (Ask-14).
