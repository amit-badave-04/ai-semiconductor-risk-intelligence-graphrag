# M5b frontend redesign: design research brief (2026-11-10)

Research sweep of shipped research and RAG products and AI-UX guidance, run by three research agents (sonnet) with live web
search, synthesised by one agent (opus). Text-only: no screenshots, no logged-in apps. INTERNAL working document.

# semigraph frontend redesign: design research brief

Tags: [V] first-party page text fetched; [S] secondary source only; [U] unverified, disputed or single-source; [J] our judgement. All sweeps were text-only: no screenshots, no logged-in apps.

## 1. First screen

- Scite [V]: headline plus one subheadline naming the action and the grounding (answers grounded in the literature). https://scite.ai/
- AlphaSense [V]: one search bar; scope, files and mode behind a "+" button and a few dropdowns; default timeframe stated. https://help.alpha-sense.com/hc/en-us/articles/41666587181203-Interacting-with-Generative-Search
- SEC EDGAR [V]: keyword box first, filters behind "+ more search options", one-line coverage banner. https://www.sec.gov/edgar/search/
- Elicit [V]: its "every claim is cited" promise sits below the fold. Avoid that. https://elicit.com/
- Guidance [V]: HAX G1/G2 (say what it can do, and how well) https://www.microsoft.com/en-us/haxtoolkit/library/ ; PAIR (limits up front, no tour) https://pair.withgoogle.com/chapter/mental-models/ ; NN/g (name answerable topics, do not overpromise) https://www.nngroup.com/articles/ai-chatbots-design-guidelines/
- Perplexity, ChatGPT and NotebookLM first screens could not be loaded [U].

**Pattern to copy: headline, grounding sentence, question box, few starters** [J, a synthesis of HAX, PAIR and Scite, not one product's verified layout]:
1. "Ask about 13 chip and AI companies."
2. "Every answer quotes the SEC filing, financial data or government rule it came from."
3. The box as hero; placeholder names real topics (risks, revenue, export rules).
4. "Free, no account needed. Example questions answer instantly; new questions are limited to [N] per hour." ([N] from the real config.)
5. About 6 starter pills, then "Show all examples".
6. "Covers 13 companies only. Not financial advice." linking to Method & Evidence.

## 2. Citations and evidence

Every product uses two tiers: a light inline chip and a full panel. https://aiuxplayground.com/pattern/citations [S]

**Inline marker.** Perplexity and ChatGPT show source identity, not bare numbers, with "+N" for several sources [S]. https://www.aiuxplayground.com/teardowns/chatgpt/citations Filings have no favicon, so label by form, company, period: "Nvidia 10-K FY2025 +2". Federal Register rules get a distinct chip with the text "Government rule" and an icon, never colour alone. Full name in aria-label; 4.5:1 text contrast; visible focus ring; target at least 24x24 CSS px (WCAG 2.5.8). Build chips and the source list from one data structure: Kagi users report "Document N" numbers drifting from the list [S]. https://kagifeedback.org/d/7944-sources-hidden-by-assistant

**Preview.** Glean previews context on hover and highlights the cited text when a source opens [V]. https://docs.glean.com/user-guide/assistant/glean-chat/glean-chat-citations/glean-citations shapeof.ai pairs quick preview with click-through [V]. https://www.shapeof.ai/patterns/citations semigraph: first ~140 characters of the verbatim passage, filing date, and an "Open full passage" link. Because it holds a link it must be a non-modal popover, not a tooltip: opens on focus and hover, dismissible, hoverable, persistent (WCAG 1.4.13). Check which Base UI primitive meets this. On phones, tap opens a bottom sheet.

**Panel.**
- A persistent "Sources (N)" control under every answer. One trade article says ChatGPT moved Sources into an overflow menu [U, single source]. https://www.relevantaudience.com/geo/chatgpt-sources-button-buried/
- Highlight the card matching the clicked chip (ChatGPT does not [S]).
- Highlight the passage inside its paragraph and offer "Ask about this filing" [V]. https://help.alpha-sense.com/hc/en-us/articles/52886436185363-Reviewing-Documents-in-AlphaSense
- Filing period and filing date in the header [S]. https://gptprompts.ai/how-to-use-perplexity-for-stock-research

**Verbatim fit.** Mirror Anthropic's model: document, location (character range), cited text; pointers come from retrieval, never model-written text [V]. https://platform.claude.com/docs/en/build-with-claude/citations A stored range makes highlighting and verbatim checks mechanical. Label whole-document citations "Whole document" [V]. https://support.google.com/notebooklm/answer/16269187 Mark unsourced sentences "Not from a source". Say "Every claim links to a passage you can read", never "verified": one study found about 75% of cited results supported their claim [U, single study]. https://arxiv.org/pdf/2605.23921

## 3. Beginner and power user on one site

Ladder [J, following PAIR and NN/g "in context, no tours"]:
- **Everyone:** hero, box, 6 pills, answers with chips.
- **Curious:** collapsed "How this answer was made" (steps, sources searched, checks), like AlphaSense's expandable Research Plan [V].
- **Power:** one "More options" row under the box, defaults preselected. "Depth: Automatic (recommended) / Quick / Thorough (slower)" replaces hybrid/vector/agent. AlphaSense attaches time cost to modes [V]; Consensus puts labelled chips beside the input [V] https://consensus.app/ ; Kagi supports ?q= prefill for bookmarking and sharing [V] https://help.kagi.com/kagi/ai/assistant.html . Neo4j's builder keeps retrieval modes in config and offers a chat-only route [V]. https://github.com/neo4j-labs/llm-graph-builder

Workspaces and uploads:
- A nav item with one line, "Upload your own documents and ask about them"; not in the first-run flow.
- Documents left, chat middle, evidence beside; tabs on phones [V layout, S phone tabs]. https://blog.google/technology/google-labs/notebooklm-new-features-december-2024/
- Scope line above the box: "Answering from 3 of 5 documents", changeable [V]. https://support.google.com/notebooklm/answer/16269187 Scope chip "My documents only / My documents + public filings" (Glean "+" menu [V]).
- Compare and "as of" under More options. A Table view (documents as rows, questions as columns, every cell cited) later [S]. https://dynamicbusiness.com/ai-tools/hebbia-revolutionizes-ai-interface-meet-the-matrix.html
- Before upload: what is stored, how long, who sees it, how to delete (Kagi privacy wording [V], HAX G16 [V]).
- After upload: 3 suggested questions about the file [V]. https://support.google.com/notebooklm/answer/16206563
- Graph view only as an optional drilldown with a list alternative; even experts asked for onboarding [V]. https://arxiv.org/html/2506.13782v1

## 4. Trust, cost and limits in plain words

- Perplexity states free limits as a daily Pro Search count [S]; reports disagree on 3 or 5 [U]. https://llmrefs.com/blog/perplexity-free-plan
- NotebookLM words its paid tier as more quantity, not locked trust features [V].
- Fintool: one-line promise that answers quote filings and you can click through [S, marketing; URL now redirects]. https://fintool.com/products/chat
- Kagi: short declaratives (not used for training; threads expire after 24 hours by default) [V].
- AlphaSense says when nothing was found and lists likely causes [V].

None is free, so semigraph writes its own "you pay nothing" line [J] (section 1, item 4). Show a remaining count only if reliable.

| Today | Plain replacement |
|---|---|
| Benchmark questions (instant, cached) | Example questions (instant, free) |
| served from cache - no model call | Saved answer, shown instantly |
| numbers not checked | Figures not matched yet: open a citation to see the source number |
| filings pending | Filings still being added |
| cited ids were retrieved | Every source shown was found for this question |

**Checks without alarm.** 2 or 3 categorical buckets with defined meanings, never percentages [V] https://pair.withgoogle.com/chapter/explainability-trust/ : "All figures matched the filings", "Some figures could not be matched", "No figures in this answer". Each badge is one sentence on what was checked, linking to Method & Evidence. Neutral styling for unmatched; warning colour only for real failures [J]. Caveats beside the output, not in a footer [V]. https://www.shapeof.ai/patterns/caveat One scope-and-freshness line under each answer, linked to Freshness (PAIR; HAX G18).

## 5. Data-dense company pages

- **At a glance:** 3 to 5 tiles (revenue, net income, risk count, latest filing date) above tabs [S, Perplexity Finance]; each tile opens its XBRL fact.
- **Key numbers:** 6 to 8 rows, then "Show all metrics" [S]. https://www.koyfin.com/help/topic/functionality
- **Formatting**, from Yahoo's NVDA page, whose "thousands" note contradicts its digits [V] https://finance.yahoo.com/quote/NVDA/financials/ : units once in the header, matching the digits; $130.5B with the exact value on hover/focus and in the panel; 16% not 0.16; newest period first; Annual/Quarterly toggle; "FY2026 (ended Jan 25, 2026)"; "Not reported" with a reason, not "--"; tabular figures.
- **Names:** readable metric names; XBRL tag kept in the panel for analysts [J]; no internal ids such as CIK (EDGAR [V]).
- **Every number is a citation** [S] https://daloopa.com/blog/product-updates/announcing-daloopas-partnership-with-rogo ; a cell with no source says "No source found", never blank [S] https://aitoolsatlas.ai/tools/hebbia/tutorial
- **Empty:** "None found in the filings we hold (latest: 10-K FY2025)" instead of "none" [J]. Hide reversed-looking relationship rows or state direction in words [J].
- **Federal Register:** its own section, "Government rules that mention this company", distinct badge, never inside "What the company said" [J; no product covers this].
- **Diffs** [S, search snippet only] https://help.bamsec.com/docs/compare-filings : "Compare with previous filing" on each filing row. A hedged summary first ("appears to add language about X; a wording change does not by itself mean the risk changed"), then a redline with "Added"/"Removed" labels plus underline and strike-through (WCAG 1.4.1). Plain verbs per passage (New wording, Dropped, Reworded) with both passages side by side, after Scite's context labels [V].

No primary source covers beginner financial tables; validate by testing.

## 6. States

| State | Pattern | Source |
|---|---|---|
| Loading | 3 plain steps ("Searching filings", "Checking numbers", "Writing"); polite live region announces steps, not tokens; static under reduced motion | https://www.shapeof.ai/patterns/stream-of-thought [V] |
| Streaming | No autoscroll; answer top stays in view; expand in place; chips appear only with a pointer; announce "Answer ready" once; sticky box or panel never covers focus (WCAG 2.4.11) | https://www.nngroup.com/articles/ai-chatbots-design-guidelines/ [V] |
| Saved | "Saved answer, shown instantly" | [J] |
| Not found | "We could not find this in the filings we hold", likely causes, 2 or 3 nearby examples | AlphaSense [V]; HAX G10 [V] |
| Rate-limited | When they can ask again; examples still usable; never an error without a next step | [J]; PAIR attribution from a chapter not retrieved [U] |
| Error | One plain sentence, Retry, what was kept; focus moves to the message | [J] |
| Partial | "Not from a source" on unsourced sentences; whole-document label; "Some figures could not be matched" | Glean [V]; NotebookLM [V] |

## 7. Anti-patterns

- 51 flat starters or niche jargon examples [V]. https://www.nngroup.com/articles/designing-use-case-prompt-suggestions/
- Tours, modals, popups [V]. https://www.nngroup.com/articles/new-AI-users-onboarding/
- An "AI may make mistakes" footer as the only trust device (shapeof caveat).
- "Verified", "never hallucinated" (Scite) or accuracy percentages such as Fintool's self-reported 98% [U]; raw confidence percentages (PAIR).
- Sources hidden in menus or behind links; citation numbers that drift.
- Retrieval-mode names, LLM logs, internal ids, or a hairball graph on the main path (Neo4j builder, XGraphRAG).
- Vague hero copy (AlphaSense marketing's "limitless" advantage, Elicit's "10x").
- Meters implying a verdict from a count of items (Consensus reviews [S]); colour-only diffs or status.
- Generic AI-UI look [J]: purple gradients, sparkles, glowing orbs, "AI-powered" badges, chat bubbles for everything, typing dots, glassmorphism, Studio-style extras. Aim for a calm research tool: neutral surfaces, one accent, self-hosted fonts with tabular figures, an evidence panel that reads like a document.

## 8. Ranked recommendations

1. **First screen stack:** headline, grounding sentence, box first, cost line, limits line. https://www.microsoft.com/en-us/haxtoolkit/library/ Respects: the noob bar; WCAG labelled input and logical headings.
2. **About 6 curated pills and "Show all examples"**, a library grouped by company and topic behind an aria-expanded disclosure. https://www.shapeof.ai/patterns/suggestions Respects: keyboard; WCAG 2.5.8.
3. **Plain wording and categorical check buckets** (section 4 table); no "verified", no percentages. https://pair.withgoogle.com/chapter/explainability-trust/ Respects: honest, hedged claims.
4. **Identity chips** ("Nvidia 10-K FY2025 +2") with a distinct "Government rule" chip and a separate rules section on Company pages. https://aiuxplayground.com/pattern/citations Respects: clickable verbatim citations; a rule never shown as a company statement; non-colour cue; 4.5:1.
5. **Focus-and-hover preview** as a non-modal popover with the verbatim snippet and "Open full passage"; bottom sheet on phones. https://docs.glean.com/user-guide/assistant/glean-chat/glean-chat-citations/glean-citations Respects: WCAG 1.4.13; keyboard.
6. **Persistent "Sources (N)"**; the panel highlights the passage in context and the clicked card, backed by a stored character range. https://platform.claude.com/docs/en/build-with-claude/citations Respects: verbatim citations, mechanically checkable.
7. **Collapsed "How this answer was made"**: plain steps while streaming, polite live region, no autoscroll, reduced motion honoured. https://www.shapeof.ai/patterns/stream-of-thought Respects: WCAG screen reader and motion.
8. **Depth control replaces the strategy select**, default Automatic, inside "More options", plus ?q= prefill. https://help.alpha-sense.com/hc/en-us/articles/41666587181203-Interacting-with-Generative-Search Respects: simple beginner path; no new dependencies on the React 19 + Base UI stack.
9. **Company numbers:** at-a-glance strip, 6 to 8 key numbers, units in the header, FY end dates, newest first, every number opening its XBRL fact. https://finance.yahoo.com/quote/NVDA/financials/ Respects: clickable evidence; self-hosted fonts under the CSP.
10. **Filing comparison:** hedged plain summary, then a labelled redline with strike-through, launched from the filing row. https://help.bamsec.com/docs/compare-filings Respects: hedged change claims; WCAG 1.4.1.
11. **Honest empty, partial and rate-limit states** with a next step and nearby examples; focus moves to the message. https://help.alpha-sense.com/hc/en-us/articles/41666587181203-Interacting-with-Generative-Search Respects: honesty; keyboard focus.
12. **Workspace:** documents left, chat middle, tabs on phones, "Answering from 3 of 5 documents", plain upload notice, compare and "as of" under More options. https://support.google.com/notebooklm/answer/16269187 Respects: WCAG reflow at 320 px and 2.4.11; no third-party upload widgets under the CSP.

**Before copying any layout:** Perplexity, ChatGPT and NotebookLM first screens, BamSEC, Hebbia and Koyfin were not seen directly. Open them in a browser and take screenshots.
