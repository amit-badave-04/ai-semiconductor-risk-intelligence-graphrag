# 002: R4 Workspace and Method, owner decisions on copy (2026-11-11)

Context: docs/specs/m5b-redesign.md R4 (RD26, RD27, the final copy sweep). The R4 plan (workflow wf_0e7be316-1e6: three Opus
mappers, a Fable planner, two Opus critics, a Fable revision) checked RD26's approved privacy wording against the code and found
two sentences that are not exactly true, and one control that cannot do what its name suggests. Asked on 2026-11-11; the owner
chose the recommended option for each.

| # | Question | Decision |
|---|---|---|
| 1 | Restore: the page never shows a workspace's key (it stays in the tab's session storage), so Restore cannot reopen a workspace from another tab or device | Keep Restore behind a disclosure, as RD26 says, triggered by "I have a workspace id and key"; the access sentence adds "It stays open in this browser tab." No security change (FS S8 unchanged) |
| 2 | Deletion: access ends exactly 24 hours after creation, but the clean-up that deletes the data runs only while the site is running (about every 15 minutes) | "Access ends 24 hours after you create the workspace; everything in it is then deleted automatically. You can delete it sooner with Delete workspace." (RD26's "within about 15 minutes" is dropped) |
| 3 | Providers: the question and the matching passages are sent, possibly to more than one configured model | "When you ask a question, the question and the matching passages from your documents are sent to the AI model providers that write the answer." (providers stay unnamed: they are configuration) |
| 4 | The withdrawn-accuracy notice (Ask footer and Method page) | Method page only, as the unheaded note beside the technical explanation; removed from the Ask footer |

Separate from R4 (a fast-path item for later): a clean-up sweep in the start-up pass (uploads/jobs.py) would close the gap after a
restart, though not while the site is stopped.
