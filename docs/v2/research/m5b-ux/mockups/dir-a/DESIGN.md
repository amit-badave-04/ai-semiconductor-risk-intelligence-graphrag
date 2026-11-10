# Direction A: The filing, made readable

## The one idea: the highlighter

Yellow appears only behind words copied exactly from a source. A citation opens the source page with the quoted
sentence highlighted inside its paragraph and the paragraph number (¶162) in the margin. The first screen shows a small
specimen, so a beginner sees the product before reading about it. Everything else stays quiet.

## Tokens

| Name | Light | Dark | Use |
|---|---|---|---|
| Paper | #F3F4F1 | #11151B | Background, cool stone |
| Sheet | #FFFFFF | #191E26 | Source pages, inputs |
| Ink | #17202D | #E5E8EC | Text, primary button |
| Graphite | #4E5765 | #A6AFBB | Secondary text (6.3:1 and 7.8:1) |
| Docket | #1E4B8E | #94B5EF | Links, citations, focus |
| Register | #1B5A4A / #E6F0EB | #86CDB5 / #15302A | Government rules only |
| Highlighter | #FFE066 | #EBCB52 | Verbatim text only, with a darker bottom edge |

**Type.** Production: Source Sans 3 and Source Serif 4 (OFL, self-hosted). Mockup: Segoe UI and Cambria/Georgia.
**Serif is the source's words; sans is ours.** All numbers are in sans with tabular figures (Georgia's old-style figures
misread). Scale 14/15/17/19/21-28/40, body 17/1.5. Lines under 75 characters, sentence case.

**Layout.** A text column plus a margin, like a well-set report. The margin holds the evidence.

```
[logo] Ask Companies Workspace      Filings through Sep 24, 2026  How it works
 H1 + grounding sentence                  | What an answer looks like
 [ Your question ............ ][Ask]      |  ¶162 [highlighted quote]
 Cost line / More options                 | What it covers
 6 examples, "Show all 51 by topic"       |
```

On the answer page the margin becomes the source sheet. At 390 px it is a bottom sheet, with the answer's start visible.

## Review against the defaults

1. **Serif display, warm off-white, red-brown accent** (the cream and terracotta default). Now cool paper, blue-black ink,
   and a yellow with a job. Headlines are sans, so serif keeps one meaning.
2. **Broadsheet hairlines.** The document feel comes from the measure, the ¶ margin and a white sheet on grey instead.
3. **Academic [1][2] footnotes.** Visitors found them cryptic. Each citation now names its source ("10-K 2024, ¶162") and
   still opens in the margin.
4. **Shadowed cards for at-a-glance.** Replaced by one band with dividers.
5. **Template chrome.** Middle-dot strings are now phrases. Ids are not set in mono; they sit behind "Filing reference
   numbers". No caps eyebrows, no link arrows.
6. **Accessory removed:** a green status dot by Freshness. It used colour alone.

## Copy deck

- **Headline:** Ask about 13 chip and AI companies.
- **Grounding:** Every answer quotes the SEC filing, reported number or government rule it came from. Select a citation
  to read the source word for word.
- **Cost:** Free, no account needed. Examples answer instantly. New questions are answered live: 5 every 10 minutes per
  address, with a daily cap.
- **Checks** (categories, never percentages):
  - "The cited passages were found for this question."
  - One of "All figures matched the filings", "Some figures could not be matched: open a citation to see the source
    number" or "No figures in this answer to match".
  - "The change claim comes from an automatic comparison of the two filings, so it is worded with care."
  - Always: "These checks do not prove that each sentence is supported by its passage."
- **Government rule:** the label "Government rule", a building icon and a green band. Text: "Linked to Nvidia by keyword
  match. Not statements by Nvidia."
- **Empty box:** "Type a question first, or pick an example below."
- **Not found:** "We could not find this in the filings we hold. Try one of these nearby examples."
- **Rate-limited:** "You have asked 5 new questions in 10 minutes. You can ask again at [time]. Examples still answer
  instantly."
- **Error:** "The answer could not be finished. Your question is still in the box. Try again."
- **Missing data:** "Not in the data we hold for recent years. The latest figure is from fiscal 2012."

## How power features are reached

- **Strategy:** under "More options". Standard (recommended), Deep research (slower), and Passage search only (cannot
  answer what-changed questions).
- **Workspace:** in the nav. The first-time page has three privacy promises, three steps, one button, and "Reopen it".
- **As-of date and Compare versions:** inside an open workspace, next to "Answering from 2 of 2 documents". They are not
  in the public box.
- **Freshness:** always in the header ("Filings through Sep 24, 2026, 9 newer being added"). It links to the full page.
- **Method & Evidence:** "How it works" in the header, plus "What the checks mean" under every answer.
- **Ids and exact values:** ids sit in a disclosure on the source sheet. $215.9B shows $215,938,000,000 on hover and focus,
  and in its accessible name.

The mockups use placeholder links instead of the sec.gov and federalregister.gov URLs; production uses the real ones.

## What to test with real users

1. In 10 seconds, can a beginner say what the site is, and read yellow as "copied from the filing"?
2. Does anyone attribute a government rule to the company?
3. Does the phone bottom sheet hide too much of the answer?
4. Do beginners grasp "$215.9B" with plain definitions, and do analysts find exact values?
