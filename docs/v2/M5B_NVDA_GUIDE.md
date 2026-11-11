# M5b I5: the screen-reader pass, step by step (NVDA)

Status: INTERNAL working guide for the owner. It states limits plainly. Do not copy it into the README or the site.
Spec: `docs/specs/m5b-frontend.md` (A1-A11, D14, D15, I5 in the increment table, B6/P8 in the gate map) and the redesign
`docs/specs/m5b-redesign.md` (RD3-RD27).
Written 2026-11-08 against the I3 pages and refreshed 2026-11-11 against the redesigned pages (Ask, Companies, Freshness, How it works,
Workspace, the evidence drawer, the header) as they are in `web/src`; the words are in `web/src/copy/deck.ts`. The labels below are the
ones the code carries; if NVDA says something different, write down what it said and compare it with the meaning in the "Expect" column.
A label in a table that was changed after this refresh is a finding about this guide, not about the page.

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
   switcher "Companies" and the in-page links "On this page" (Company page), and the drawer "Evidence".
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
content" link; Enter on it moves you to the main area. Next comes the "semigraph" home link. The links in the navigation are Ask,
Companies, Workspace, a freshness link (its name starts "Freshness:" and then one line about the data's date; it only appears once the
data has loaded), and "How it works". The current page's link is announced as "current page" (or similar). The Turnstile bot-check box,
when it is shown, sits after the main area at the bottom of the page, outside the landmarks.

Page titles come from the code as "<page name> [dot] semigraph" (the code uses a middle dot between the two parts; this guide writes it
as [dot]; NVDA may say "dot" or nothing). The Ask page's title is just "Ask" before the dot, although its heading is a longer sentence.

### 5.1 Ask  (`/v2/`)

Labels in the code (words in `web/src/copy/deck.ts`; `Ask.tsx`, `AskFirstScreen.tsx`, `AskParts.tsx`, `components/SpecimenStamp.tsx`,
`components/AnswerSample.tsx`, `components/EvidenceDrawer.tsx`, `components/WhatWasChecked.tsx`, `components/AnswerNotes.tsx`): the level 1
heading is "Ask about 13 chip and AI companies." (the tab title is just "Ask"). Under it a sentence "Answers cite their sources: a filing passage,
a reported number or a government rule. Click one to see an example." where the three source names are buttons (stamps) that open a small
example window. Then a level 2 heading "Your question" over a box named "Your question" and an "Ask" button; a cost line ("Free to use, up
to ... live questions every ... minutes ..."), a link "How answers are made"; a disclosure button "More options" (it also reads "Depth:
Standard") that opens a group "Depth" of radio buttons; one status line (a status region). Named regions: "What an answer looks like" (a
static sample, only until the first answer), "Or try an example" (six starter questions in four groups: Understand a company, Check a number,
Track export rules, See what changed; plus a disclosure "Show all examples, grouped by company") and "Companies covered" (13 links). The page
ends with small print (cost, reliability). "Your question" is deliberately not a region.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Ask-1 | NVDA+T on arrival | "Ask [dot] semigraph" | A7 | |
| Ask-2 | Press 1, then count level 1 headings | One level 1 heading, "Ask about 13 chip and AI companies." After navigating from another page via the nav, focus lands on this heading and NVDA reads it | A7 | |
| Ask-3 | Press D through landmarks | banner, navigation "Main", main, and inside main the named regions "What an answer looks like", "Or try an example" and "Companies covered". Any other region has a name | A7 | |
| Ask-4 | Press E (edit field), F (next field) | The question box is announced as "Your question" (not "edit", not blank). Every field you reach has a name | A4 | |
| Ask-4b | Press Enter on "More options" (announced as collapsed, then expanded), then arrow or Tab through the radio buttons | The group is announced as "Depth". Options: "Standard (recommended)", "Basic text search", and "Deep research" only when the server offers it (that option is the paid agent: do not ask with it). Each is a radio button with its hint ("Searches filings, numbers and rules together." and so on) read as its description. The collapsed button reads "More options Depth: Standard" | A4 | |
| Ask-4c | Tab to the three stamps in the sentence under the heading ("filing passage", "reported number", "government rule") and press Enter on one | Each is a button. A window opens named "Example: filing passage" (and so on), focus moves into it, and it shows a short example (it says "Loading the example..." first; if it cannot load it says "The example could not be loaded right now."). A "Close" button closes it; Esc closes it and the page behind stays as it was (check where focus lands) | A1, A5 | |
| Ask-5 | Press Tab to reach the starter questions (cached, free) and press Enter on one | The question is sent at once. The status line says something like "Saved answer, shown instantly." (or "Answered earlier, shown instantly.") once; the answer text itself is NOT read aloud automatically while it streams. The answer is headed by a level 2 heading holding the question | A3 | |
| Ask-6 | Press B to walk the buttons in the answer | Each citation chip is a button with a readable name, not a raw id. The code's labels: a filing passage "Nvidia 10-K, filed 2025 [dot] paragraph 162" once its company and form are known, or just "Filing 2025 [dot] paragraph 162" before; a number "Reported revenue, period ended <date>"; a rule "Government rule 2025-19001". A chip also says whether it is expanded. NVDA may read the tooltip ("Open the source") as a description; that is a Note, not a Fail (it read the raw id before N5, 2026-11-11) | A1 | |
| Ask-6b | Under the answer, press H and D | If present: "Read with care" (level 3, region; it only appears for answers with change claims), "Sources (N)" (list of the same chips), "What was checked" (list of plain sentences, no icon read out) and a "Technical details" disclosure (collapsed). Each is announced once | content | |
| Ask-7 | On a chip press Enter. Repeat with Space | The evidence drawer opens. NVDA announces a landmark named "Evidence" (complementary) with a level 2 heading "Evidence". Focus moves to a close button named "Close evidence" (its visible text is "Close") | A1, A5 | |
| Ask-7b | In an evidence panel that shows highlighted words (rare: only when the answer copied words exactly; the evidence specimen popovers and a cached example may not have one) read the passage with the arrow keys | Note whether NVDA says anything for the highlighted words ("highlighted" or "marked") and reads the line "Highlighted: the words copied exactly from the source" under the passage. If the highlight is silent, write it down: decision 003 asks for this check | A1, decision 003 | |
| Ask-8 | With the drawer open, Tab out of it to another chip in the answer and press Enter | The other chip works; the drawer is not a trap; the page behind it was not made unreachable | A5 | |
| Ask-9 | Press Esc in the drawer | The drawer closes. NVDA focus returns to the chip you opened it from (check where it goes if the answer was replaced meanwhile) | A2, A5 | |
| Ask-10 | With the drawer closed, Tab through the page | Nothing in the closed drawer is reachable: no stray "Close evidence" stop, no "Evidence" landmark in the D list | A5 | |
| Ask-11 | Refusal: press the "Ask" button with an empty box, or send a question over the limit if you can | An empty box says "Type a question first, or pick an example below." in the status line under the box and NVDA says it without you moving (the box is also marked invalid and points to the same sentence; check that it is not heard twice in a row). Other refusals use the same status line | A3, A10 | |
| Ask-12 | Optional, one live question (paid; see Money in section 1). Type a short question, solve the Turnstile check yourself in the browser, press Ask, wait | Progress ("Looking in the filings...", then "Found ... Writing the answer...", then "Done.") and any error are spoken through the status line. If the stream is cut, the "connection lost before the answer finished ..." message is spoken | A3, A10 | |
| Ask-13 | The research steps list. Normally SKIP: it only appears for a "Deep research" ask, the most expensive kind of paid ask. Write "not checked (needs a paid agent ask)" | If you did run one: new steps are announced politely as they appear; the list is named "What the research agent did" | A3 | |
| Ask-14 | Turnstile bot check (a third-party widget, after the main area at the bottom of the page) | Note whether NVDA can reach it with Tab. If you cannot complete it with NVDA, record that as a finding. Do not try to bypass it. This is Cloudflare's widget, not ours; the finding is still worth writing | A10 note | |

### 5.2 Companies  (`/v2/company/NVDA/`, then another company)

Nav link "Companies" opens the first company. Heading: the company name and ticker, for example "Nvidia (NVDA)". Switcher: a navigation
named "Companies" with 13 links ("Nvidia (NVDA)", "AMD (AMD)" ...), the current one marked as the current page. A status line ("loading..."
until the data arrives; a status region). Then lines saying which annual and quarterly reports are held (for example "10-K for the fiscal
year ended <date>, filed <date>") and "Data as of <date>"; two links, "Ask about <company>" and "Compare filings"; and a navigation named
"On this page" with five in-page links: Key numbers, Government rules, Relationships, Filings, Risk changes. Then the blocks, each a level 2
heading and a named region, in this order: At a glance, Key numbers (a "Show exact values" switch, and a scrolling table region named
"Key numbers table"), Government rules, Relationships (with a level 3 heading "Competitors named in its filings"), Filings (a table region named
"Filings table"), Active risks, Risk changes (the recent changes between filings). Under Risk changes each pair of filings has a level 3 heading
such as "10-K 2025-02-26 -> 10-K 2026-02-25" (the code writes an arrow), then labels like "No matching risk factor found in the earlier filing",
"No longer appears as a separate risk factor", "Not matched", "Reworded" (check the exact set in `web/src/pages/CompanyChanges.tsx`). A pair that could not
be compared reads "<pair>: comparison not available (<reason>)." Reported numbers (in At a glance and Key numbers) and the citation ids
(Relationships, Risk changes) are source stamps: buttons that open the Evidence drawer, as in the Ask answers and the Workspace what-changed view.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Co-1 | NVDA+T | "Nvidia (NVDA) [dot] semigraph" | A7 | |
| Co-2 | Press 1; press H through the others | One level 1 heading (company name). Level 2 headings in order: At a glance, Key numbers, Government rules, Relationships, Filings, Active risks, Risk changes, each as a named region | A7 | |
| Co-3 | Press D | banner, navigation "Main", main, then inside main a navigation "Companies", a navigation "On this page", and the seven named regions above (plus the table regions "Key numbers table" and "Filings table"). The three navigations have different names | A7 | |
| Co-4 | Press K through the switcher | 13 company links by name and ticker; the current one says current page | A7 | |
| Co-5 | Activate "AMD (AMD)" with Enter | The page changes; NVDA reads the new level 1 heading "AMD (AMD)" (focus moved there); the title changes. You did not have to hunt for the new page | A7 | |
| Co-6 | While it loads | "loading..." is spoken once (status region). If an error appears it is spoken through the same region; a company answer the page cannot read reads "could not load the company data" | A3, A10 | |
| Co-7 | Tab to a source stamp under Relationships or under Risk changes, press Enter, then Escape | Each stamp is announced as a button with its label (for example "Filing 2026 [dot] paragraph 320", or the company and form first once loaded); Enter opens the Evidence drawer with focus on "Close evidence"; Escape closes it and focus returns to the same stamp | A1, A2, A5 | |
| Co-7b | In Key numbers, Tab to a figure | A button whose name starts with the shown figure, then what it is, for example "$215.9B, Revenue, FY2026. Open source" (check the exact words); Enter opens the Evidence drawer | A1, A4 | |
| Co-7c | Press Enter on a link in "On this page", for example "Filings" | Focus moves to that section's heading (NVDA reads "Filings", heading level 2) | A7 | |
| Co-7d | Press Enter on "Ask about <company>" | The Ask page opens; its question box already holds a question about the company; nothing is sent (check where focus lands) | content | |
| Co-7e | Tab to the "Show exact values" switch and press Space | Announced as a switch (or toggle button) named "Show exact values", on or off; the figures in the table change to exact values | A4 | |
| Co-10 | Read Risk changes (press H, then arrow down) | The labels read as hedged statements ("No longer appears as a separate risk factor", never "removed"); a pair that could not be compared says so with its reason; each quoted passage of a reworded risk starts with its filing, for example "10-K 2025-02-26: " before the older wording and "10-K 2026-02-25: " before the newer | content | |
| Co-8 | Read the Government rules block | A sentence at the top says "These US export-control rules come from the Federal Register and are linked to the company by keyword matching: actions by the government, not statements the company made." NVDA reads it; each rule is marked "Government rule" | content | |
| Co-9 | Type `/v2/company/XXXX/` (a made-up ticker) in the address bar | Title "Page not found [dot] semigraph"; one level 1 heading "Page not found"; the line "There is nothing at this address."; a link "Back to the start page". This is a fresh page load, so focus starts at the top, not on the heading (that is expected) | A7 | |

### 5.3 Freshness  (`/v2/freshness/`)

Heading "Freshness". Then one sentence of intro ("When the filings and the government rules were last checked for anything newer than
our data."), then a status line ("Loading..." or "Data status unavailable: <detail>", a status region). Once loaded: a one-line summary
(the same line as the nav link, for example "Data as of <date>, 2 newer filings being added"), then a list named "Freshness details"
of label and value rows: Checks, Data as of, Last checked, Last failed check, Next check, Newer filings being added, Government
rules, Companies the check could not look up; plus a nested list "Newer filings" when there are any. Rows with no value are left out.
"Checks" is a word: Working, Overdue, The last check failed, Off or Not set up.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Fr-1 | NVDA+T | "Freshness [dot] semigraph" | A7 | |
| Fr-2 | Press 1; D | One level 1 heading "Freshness"; banner, navigation "Main", main. The freshness link in the navigation is announced as the current page | A7 | |
| Fr-3 | Press L (list) or read with the Down arrow | The list is announced as "Freshness details" with its item count; each row reads as label then value, for example "Last checked 3 h ago (<date>)" | A7 | |
| Fr-4 | Watch the loading state | "Loading..." once, then the data; or "Data status unavailable: <detail>", spoken. Nothing repeated | A3, A10 | |
| Fr-5 | Status meaning | "Checks" is a word (Working, Overdue, ...), never only a colour (it is read to you, so if you hear a word it passes) | A8 | |
| Fr-6 | Tab through the page | Only the header links are tab stops (it has no controls of its own); no trap | A7 | |

### 5.4 How it works  (`/v2/method/`)

Nav link "How it works"; the page heading is "Method & Evidence" (`web/src/pages/Method.tsx`). Heading "Method & Evidence", then a plain
lead paragraph ("Answers are written from passages of the companies' SEC filings ..."), a technical paragraph, a short unheaded note
that earlier accuracy figures are withdrawn (it ends with the links "plan" and "review", which open GitHub pages in a new tab), a link
"Freshness" (same tab, to the Freshness page), then two level 2 sections (each a named region): "What the checks mean" and "How reliable
are change claims?". The page makes no requests of its own.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Me-1 | NVDA+T | "Method & Evidence [dot] semigraph" (NVDA may read "&" as "and") | A7 | |
| Me-2 | Press 1, then H | One level 1 heading "Method & Evidence", then the two level 2 headings, "What the checks mean" then "How reliable are change claims?", no skipped levels | A7 | |
| Me-3 | Press D | banner, navigation "Main", main, plus the two named regions | A7 | |
| Me-4 | Press K | After the top navigation links: "plan", "review" and "Freshness". Judge whether, with the sentence around them, you can tell where they go; if not, write it as a Note (link wording is not one of the A-items) | note | |
| Me-5 | Say-all (NVDA+Down) | The page reads as sensible prose: the unheaded note says plainly that earlier figures are withdrawn, as the spec requires. Stop with Ctrl | content | |
| Me-6 | Press Enter on the "Freshness" link in the page | The Freshness page opens in the same tab; NVDA reads its level 1 heading "Freshness" (focus moved there) | A7 | |
| Me-7 | From Ask, press Enter on the link "How answers are made", or on "How reliable change claims are" under an answer that shows "Read with care" | The Method page opens (the second link goes to the "How reliable are change claims?" section; check where focus lands) | A7 | |

### 5.5 Workspace  (`/v2/workspace/`)

The page shows its content only when the server has uploads switched on (`/api/stats`); if you see just the heading
Labels in the code (the words are in `deck.workspace` in `web/src/copy/deck.ts`; `Workspace.tsx`, `WorkspaceActive.tsx`, `WorkspaceUpload.tsx`, `WorkspaceDocuments.tsx`, `WorkspaceChanges.tsx`): a level 2
heading "Your documents"; a short paragraph on what a workspace is; a numbered list of three steps (Create a workspace. Upload a
document. Ask a question about it on the Ask page.); a line on the accepted file types; three short lines on who can open it,
when it is deleted and what is sent to the AI model providers; a "Create a workspace" button; a disclosure button "I have a
workspace id and key" that opens the fields "Workspace id" and "Workspace key" (the key field is a password field) and a "Restore"
button; a status line (a status region). Once a workspace is open: a line "Workspace <id>" with its expiry, a "Delete workspace"
button, a date field named "Ask as of this date instead of the current version", a select labelled "upload as:" (first option
"Upload a new document"), and one drop-zone button whose text starts "Drag a PDF, DOCX, Markdown, TXT or HTML file here". There
is no separate "choose file" button: the drop zone is the real button and Enter or Space on it opens the file picker.

| # | Do | Expect | Req | Result |
|---|---|---|---|---|
| Ws-1 | NVDA+T; press 1; D | "Workspace [dot] semigraph"; one level 1 heading "Workspace"; banner, navigation "Main", main. The "Your documents" section is a level 2 heading and has no region name of its own | A7 | |
| Ws-2 | Press Enter on "I have a workspace id and key" (announced as collapsed, then expanded), then F (or E) through the fields | The restore fields are announced as "Workspace id" and "Workspace key" (the second says it is a password field); then a "Restore" button. After a workspace is open: the date field as "Ask as of this date instead of the current version"; the upload target select as "upload as:". None is "edit" or blank. The file input (hidden) is named by the drop-zone text | A4 | |
| Ws-3 | To reach the rows below you need an open workspace. Creating one and uploading a file make no paid model call (no money), but each needs the Turnstile check, and each upload counts toward the server's daily upload limit. Asking a question about the uploaded file IS a paid live ask (D14, ceiling $5); you do not need one here. Use a tiny text file you do not mind sharing (persona walkthrough task 6.4 shows how to make one), and click "Delete workspace" afterwards | | | |
| Ws-4 | Tab to the drop zone | It is announced as a button named by its text ("Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one ..."); Enter and Space open the file picker; there is no second button for it | A6 | |
| Ws-5 | After an upload starts | Progress ("uploading...", "queued", "checking", "reading the document", "splitting into sections", "embedding", "comparing with the previous version", "saving", then "ready" or "failed"; on screen "uploading" ends in an ellipsis character, "embedding" adds a count and the time left, and "failed" adds the reason; if the bot-check settings could not be loaded the row reads "could not load the bot check - reload the page and try again", and if they are still loading the upload waits up to 15 seconds for them) is spoken through the status region, once per change | A3 | |
| Ws-6 | A refusal (for example a wrong key on Restore) | The error text appears in the status line under the Restore row (the fields point to it) and is spoken without moving. Check that it is not spoken twice | A3, A10 | |
| Ws-6b | Create a workspace, then Delete workspace | After Create, focus is on the heading "Your documents"; after Delete, on "Create a workspace" | A7 | |
| Ws-7 | While a workspace is open, go to Ask | The starter question buttons are disabled and a line says "With a workspace open, the examples are off: each would become a paid live question about your documents." Under "More options" only the "Standard (recommended)" Depth is offered, with the line "With a workspace open, questions use Standard." | content | |
| Ws-8 | Optional: after an upload, open the what-changed view of a second version (the version buttons are named "<document>: v2") and press Enter on a chip in it | The same drawer opens ("Evidence"), Esc closes it and focus returns to that chip | A1, A2, A5 | |

### 5.6 Across pages (3 minutes)

| # | Do | Expect | Req |
|---|---|---|---|
| X-1 | Open the drawer on Ask, then use the "Companies" link | The drawer closes and focus goes to the new page's heading (not back to the chip) | A5, A7 |
| X-2 | Press Tab for one full lap on any page | Focus is always visible (you can see a ring); you can always leave; the Skip link works | A7, A9 |
| X-3 | Make the browser window narrow (drag its edge), or zoom in: hold Ctrl and press + (plus) until the zoom shows 200%. Ctrl+0 resets it | Single column; nothing is cut off; you can still reach everything | A9 |
| X-4 | Windows dark mode on, then off (Settings, Personalization, Colors, "Choose your mode": Dark, then Light) | Text stays readable in both; status words never depend on colour alone | A8 |
| X-5 | On any page, find the freshness link in the main navigation (K from the Workspace link, or Tab) | A link, after "Workspace" and before "How it works". Its name starts with "Freshness:" (said but not shown) and then one plain line, for example "Data as of <date>" or "Data as of <date>, 2 newer filings being added"; other lines: "Data date unknown", "Data as of <date>; the check for newer filings failed" or "is overdue", "Data status unavailable". No link at all appears until the freshness data has loaded. Enter opens the Freshness page, where the link is announced as the current page. It is read once per page load and not repeated on route changes | A3, A8 |

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
- The labels of the Ask, Workspace, evidence drawer, Company, Freshness, Method, header, menu and skip-link controls
  were read from the redesigned code in `web/src` (words in `web/src/copy/deck.ts`) on 2026-11-11; none was heard through
  NVDA by the author. If a later change moves a label, trust what NVDA says and note the difference.
- Where a row says "check", the code does not settle it (for example exactly where focus lands, or whether NVDA reads
  an element twice). Write down what you hear.
- Whether Turnstile's challenge is operable with a screen reader: unknown until you try (Ask-14).
