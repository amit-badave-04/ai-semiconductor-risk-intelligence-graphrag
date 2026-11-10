# M5b redesign: design direction decision memo (2026-11-10)

Three directions were mocked up by three design agents (opus); three judges (a newcomer, an analyst, an accessibility and
brand specialist; opus) scored them from screenshots; this memo (opus) synthesises. Mockups: `mockups/dir-a`, `dir-b`,
`dir-c`; key screens: `screens/`. The owner decides.

## 1. Scores

Each cell: grasp in 10 s / trust / elegance / power user / phone (1 to 5).

| Judge | A: filing made readable | B: analyst workbench | C: guided by questions | Pick |
|---|---|---|---|---|
| Newcomer | 4/5/4/3/4 | 3/4/4/5/3 | 5/4/4/4/3 | C |
| Analyst | 4/5/4/3/4 | 4/4/4/5/3 | 5/4/3/3/3 | B |
| Accessibility and brand | 4/5/4/3/4 | 4/4.5/4/5/3 | 5/3.5/3/3.5/3 | B |
| Mean (total) | 4.0/5.0/4.0/3.0/4.0 (20.0) | 3.7/4.2/4.0/5.0/3.0 (19.8) | 5.0/3.8/3.3/3.5/3.0 (18.7) | B, 2 of 3 |

## 2. Recommendation: build on B

B alone already serves both halves of your brief. A beginner gets a plain first sentence. An advanced user gets company tabs, an exact-values switch, Compare filings, a Sources list on every answer and a workspace with Upload and Compare versions. Those are the expensive parts. B's weak spots are wording and styling, and A and C already show the fixes.

B must also fix these before launch (all visible in the screenshots):
- On a phone, tapping a source shows nothing; it must open the source.
- Company tabs on a phone wrap onto two rows; use one row that scrolls sideways.
- Citation chips are solid green, which reads as "verified"; use neutral ink.
- "paragraph 0162" becomes "paragraph 162" (C has the same fault; fix it once).
- One date style everywhere (no 2024-02-21 next to Feb 21, 2024).
- Keep B's header: "Data as of Sep 24, 2026, 9 newer filings being added".

## 3. Borrow (six items)

1. From C: starter questions grouped by job, each with its "You get..." line, drawn as B's plain ruled rows.
2. From C: an "In short" lead with its own source chips, plus A's "Read ... with care" note on change claims.
3. From A: the yellow highlighter on copied words, dark text and underline, normal weight (no bold or italic).
4. From A: the "What an answer looks like" sample beside the question box.
5. From A and C: on a phone, the source opens as a bottom sheet with a large labelled Close button.
6. From C and A: plain words: chips that name the section, a one-line meaning under each figure ("Money from sales"), C's fiscal-year sentence.

## 4. Disagreements and rulings

- **A has the highest raw total, yet nobody picked it.** It is weak for the advanced user, and that gap is structural. A's best parts move into B easily; B's tabs and controls would not move into A easily.
- **The newcomer picked C.** C's wins are the starter groups and "In short", and both are borrowed above. C also had the most defects: no side margin on the phone answer, a misaligned column, and "Data checked 4 h ago", which hides that data stops at Sep 24.
- **Tickers on the Ask page.** The analyst liked them; the newcomer was lost. The sample answer takes the right column; the company list moves below the examples.
- **"In short".** Two judges loved it; the accessibility judge warned it can outweigh the evidence. Keep it, but every sentence carries chips and passes the same checks, and it never looks heavier than the quoted source.
- **Yellow highlight vs B's gold rule stamp.** Yellow is only for copied words. The government-rule stamp keeps its double border and square corners in a colour that is not yellow.

## 5. Test with one or two real people

- A beginner opens the link on a phone, taps a starter question, then a source. Pass: within 30 seconds they can say which words came from the filing.
- Ask "Is the latest quarter included?" Pass: they read the freshness line correctly.
- Show a government rule and ask "Did Nvidia say this?" Pass: no.
- Ask whether "In short" is proven. Pass: they know to open a chip.
- An analyst finds Compare filings, Show exact values, the depth setting and workspace Upload unaided.
- Also check what screenshots cannot show: keyboard focus rings, measured dark-mode contrast, and tap targets of at least 24 px on chips.
