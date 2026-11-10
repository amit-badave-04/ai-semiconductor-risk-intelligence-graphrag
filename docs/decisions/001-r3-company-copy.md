# 001: R3 Company page, owner decisions on copy and scope (2026-11-10)

Context: docs/specs/m5b-redesign.md R3 (RD19-RD25, D-R2). The R3 plan (workflow wf_86be9b04-097: three Opus mappers, a Fable
planner, two Opus critics, a Fable revision) found five points that need the owner's yes before slices 4-6, because they add
visitor-facing wording or amend docs/specs/m5b-frontend.md. Asked on 2026-11-10; the owner chose the recommended option for each.

| # | Question | Decision |
|---|---|---|
| 1 | A metric with no value in any year we hold (Qualcomm capex, Amazon R&D): what the Key numbers row and the At a glance tile show | Show "Not in our data for these years." (the first half of the D-R4 sentence; there is no year to name) |
| 2 | Passage chips under each government rule on the Company page | Dropped: a rule row shows its "Government rule" stamp, date and title only, as in answers. Amends m5b-frontend.md R25's chip line for rules only |
| 3 | The At a glance rule count (the server lists at most 8 rules; Nvidia has 8) | "N government rules listed below", counting the rules the page shows, linked to the Government rules section |
| 4 | New wording that states no limit | Approved as written: "Fiscal years are the company's own. Each column shows the date its year ended."; the Ask pre-fill "What risks does <company> describe in its latest annual report?"; "On this page"; "Show exact values"; "In the filing's own words"; "Competitors named in its filings" |
| 5 | `web/src/lib/pages.generated.ts` filer rows gain `annualForm` and `quarterly` (m5b-frontend.md R5 amendment) | Approved, so the header can say "Files an annual report on Form 20-F; no quarterly 10-Q" for TSMC and ASML |

D-R2 (four fiscal years, `DOSSIER_METRIC_PERIODS = 4`), D-R3 (the empty-rules sentence) and D-R4 (the limit wordings) were
already approved with the spec ("all as recommended").
