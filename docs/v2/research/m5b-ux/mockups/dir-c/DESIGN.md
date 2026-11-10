# Direction C: Guided by questions

## The one idea: proof marks

Four small marks say what kind of proof stands behind a claim. Each starter group on the first screen is headed by the mark its answers carry; the same marks then label every citation, source panel and Company section.

- **Passage** (quotation sign): the company's own words in a filing.
- **Number** (ledger grid): a figure the company reported (XBRL).
- **Change** (two offset pages): two filings compared.
- **Government rule** (columned building, squared double border, the words "Government rule"): a Federal Register rule, not the company.

Everything else stays quiet.

## Tokens

| Token | Light | Dark | Role |
|---|---|---|---|
| paper | #F4F6F4 | #0F1513 | page |
| surface | #FFFFFF | #161E1C | panels |
| ink | #18211F | #E5ECE9 | text |
| muted | #4D5A56 | #A3B1AC | secondary text |
| oxide | #0E6B5E | #6CCBB8 | accent, company-source chips |
| federal | #3A4588 | #AEB7F2 | Government rule only |

Text pairs compute at 5.4:1 or better.

**Type.** Production: Public Sans (OFL, built for US government services) for UI and all numbers, with tabular figures (confirm the subset keeps `tnum`). Source Serif 4 is used **only for verbatim source text**, so the typeface itself says "these are the filing's words". Mockup: Segoe UI and Sitka Text. Scale: 13, 15, 17 (body), 21, 28, 44. Measure at most 68ch. Sentence case.

```
semigraph  Ask Companies Workspace How it works     Data checked 4 h ago
Questions about chip and AI companies, answered from the source.
grounding sentence
[ Your question .......................................... ][Ask]
More options   Free, no account. Limits in plain words.
Not sure what to ask? Start with what you need.
| Understand a company | Check a number | Track export rules | See what changed |
|  mark, "You get..."  |                |                    |                  |
|  2 real questions    |                |                    |                  |
Show all 51 example questions      Or open a company: Nvidia AMD ...
```

## Review against the defaults

- **Four rounded cards with soft shadows** for the job groups (SaaS kit): replaced by one band split into columns by space. The marks carry identity.
- **Warm off-white with a serif headline** (cream cluster): replaced by a cool grey-green page and a sans headline. The serif is kept only for quoted sources.
- **ALL-CAPS group labels** (NUMERIC, REFUSAL): replaced by job names in sentence case.
- **"A · B · C" meta strings** from the data: replaced by labelled pairs and sentences.
- **Monospace id chips**: replaced by identity chips ("Nvidia 10-K FY2024, risk factors"). The paragraph number and source id moved under "For analysts".
- **Accessory removed after critique:** a dotted wafer-grid behind the headline.

## Copy deck

- **Headline:** Questions about chip and AI companies, answered from the source.
- **Grounding:** Ask about Nvidia, TSMC, ASML and 10 more. Every claim in an answer links to the filing passage, reported number or government rule it came from, so you can read it yourself.
- **Cost:** Free, no account needed. Example questions answer instantly. Your own new questions: up to 5 every 10 minutes, and a daily limit. Covers 13 companies only; not financial advice.
- **Answer opener:** "In short", then one plain sentence with its own citations.
- **Checks:** Saved answer, shown instantly / Every source shown was found for this question / All figures matched the filings / Some figures could not be matched: open a citation to see the source number / No figures in this answer / Change found by comparing two filings: it may have moved into another risk factor.
- **Empty box:** Type a question first, or pick one of the examples below.
- **Not found:** We could not find this in the filings we hold. It may be outside these 13 companies or newer than our data. Try one of these instead.
- **Rate limit:** You have asked 5 new questions in 10 minutes. You can ask again at 14:32. Example questions still answer instantly.
- **Daily limit:** New questions are paused until tomorrow. Example questions still work.
- **Error:** Something went wrong while writing the answer. Your question is still in the box. Try again.

## How the power features are reached

- **Depth** (was hybrid / vector-only / agent): "More options" under the box. Choices: Standard (recommended); Text match only (for comparison, cannot answer what-changed questions); Deep research (plans extra lookups, slower). Kept in the URL.
- **Ask as of a date, Compare versions:** beside the Workspace question box once two documents exist.
- **Workspace:** a nav item, never in the first-run path.
- **Freshness:** "Data checked 4 h ago" in the header, and a scope line under every answer naming the 9 filings still being added.
- **How it works** (Method & Evidence): the nav, and a link from each check.

## What I would test with real users

1. **Five-second test.** Can a newcomer say what the site does, and that it is free?
2. **Marks.** After one answer, with the legend closed, can people say a Government rule is not the company speaking?
3. **Analysts.** Are the exact value, paragraph and source id found fast enough under "For analysts"?
4. **Phones.** Is the evidence bottom sheet better than a full-screen source view?
