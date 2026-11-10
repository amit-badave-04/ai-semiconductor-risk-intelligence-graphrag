# Direction B: analyst workbench, friendly front door

## The one idea: source stamps

Each kind of source has a stamp you can tell apart without colour:

- **Filing passage**: page icon, solid border, rounded corners.
- **Reported number**: bar icon. In tables the number itself is the button, with a dotted underline.
- **Government rule**: columned-building icon, double-rule border, square corners, and the words "Government rule".
- **Your file**: plain grey stamp.

The first screen teaches the stamps. The grounding sentence is built from three live stamps, and each opens a real example. A second rule follows: **serif means the source's own words** (quotes, rule summaries, the evidence panel). Everything we write is sans. The rest stays quiet.

## Tokens

| Name | Light | Dark | Role |
|---|---|---|---|
| Paper | #F6F7F9 | #121820 | page |
| Ink | #17202A | #E7ECF2 | text |
| Graphite | #4F5A67 | #A9B3BF | secondary text |
| Hairline | #D8DDE3 | #2E3843 | rules |
| Solder green | #13654B / #E4F2EC | #74D3AB / #15302A | actions, company sources |
| Seal gold | #7A4E00 / #FBF1D9 | #F0C463 / #2D2412 | government rules only |

Focus ring: #1D4ED8 in light, #8AB4FF in dark, 3 px. Gold never means "warning".

**Type.** Production: Source Sans 3 for the UI and Source Serif 4 for verbatim text (OFL, self-hosted, tabular figures). The mockup uses Segoe UI/system-ui and Charter/Georgia. Scale: 13/15/17/21/28/44. Sentence case, `tabular-nums`, lines under 80 characters.

**Layout.** Left-aligned working column with a reference rail.

```
semigraph  Ask Companies Workspace     Data as of Sep 24, 2026  How it works
Ask about 13 chip and AI companies.          | Companies covered
...from: [filing] [number] [government rule] | Nvidia NVDA  Microsoft MSFT
[Your question ...................] [Ask]    | ...
Free, no account needed. 5 every 10 min      |
> More options  Depth: Standard              |
Or try an example: 6 ruled rows              |
```

- **Answer page:** answer on the left, evidence panel docked on the right. Below 900 px the panel stacks under the answer.
- **Company page:** header, then one ruled strip, then numbers on the left and government rules on the right.

## Review against the defaults

- At-a-glance figures started as 4 shadowed rounded cards (the SaaS kit). They are now one strip divided by hairlines.
- Starter pills became ruled rows, because real questions are 15 words long.
- Accession numbers and XBRL tags were in monospace. They are now tabular sans, inside "Details for analysts".
- The yellow highlighter clashed with Seal gold. Cited text now uses a green tint with an underline and a text label.
- No finance blue, purple, ALL-CAPS eyebrows, dot-joined meta or gradients.
- **Accessory removed:** the info icon on the cost line, because it read as a warning.

## Copy deck

- **Headline:** Ask about 13 chip and AI companies.
- **Grounding:** Every answer shows where it came from: a [filing passage], a [reported number] or a [government rule]. Click one to read the original.
- **Cost:** Free, no account needed. Examples answer instantly from saved answers. New questions run live, so they are limited to 5 every 10 minutes from one address, with a daily cap.
- **Status:** Saved answer, shown instantly.
- **Checks:** "All figures matched the filings." / "Some figures could not be matched: open a citation to compare." / "No dollar amounts or percentages in this answer." / "Every source shown was found for this question." / "Change found by comparing two filings automatically." Saved answers skipped the number check, so they never say "matched".
- **Rule notice:** These are US export-control rules from the Federal Register, linked to Nvidia by matching keywords. They are actions by the government, not statements Nvidia made.
- **Empty:** Not in our data for these years. The latest we hold is FY2012.
- **Not found:** We could not find this in the filings we hold.
- **Rate limited:** You have asked 5 new questions in 10 minutes. You can ask again at [time]. Examples still answer instantly.
- **Error:** The answer could not be finished. Your question is kept. Try again. Focus moves to the message.

## Power features

- **Depth:** under "More options". Standard (recommended), Deep research (slower), or Basic text search (comparison only).
- **Ask as of a date:** same row, disabled with the note "Works on documents in your Workspace".
- **Workspace:** in the nav. Covers privacy, 3 steps and the accepted file types. Restore sits behind a disclosure. Compare and as-of are inside an open workspace.
- **Freshness:** top-bar line on every page; scope line under each answer.
- **Method & Evidence:** "How it works" in the top bar and beside the checks.
- **Exact values:** every number opens its fact. The table also has a "Show exact values" toggle.

Mockup links are `#`; production links to sec.gov and federalregister.gov, loading nothing external.

## Test with real users

1. **10-second test:** can a first-timer say what the site is, and that it is free?
2. **Stamp recall:** after one answer, can people pick out the government rule without reading its label? Check the double rule in forced-colours mode.
3. **Numbers:** do beginners read "$215.9B" correctly, and do analysts find the exact value?
4. **Hedges:** after T7, does anyone say Nvidia "removed" a risk?
