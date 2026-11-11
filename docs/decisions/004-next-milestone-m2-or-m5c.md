# Decision 004: the next milestone - M2 (search) first, or M5c (buyer features)?

Date: 2026-11-11. Asked by the owner: "llm counsil on should we do m2 first then decide to do m5c or not". Method: the
`llm-council` skill (workflow wf_7e84c17b-774): five advisors (Sonnet: Contrarian, First Principles, Expansionist, Outsider,
Executor) answered independently, five anonymised peer reviews (Sonnet), a chairman (Opus). The framed question gave the
state on 2026-11-11 (M5b live at / as release v23; M2, M5c, M6 not started), M2's and M5c's scope from `docs/v2/PLAN.md` and
`docs/v2/M5_DECISIONS.md`, the measured quality (deployed-path benchmark: mechanical 40/41, judged 17/23; change-claim
precision 0.88 vs 0.90) and the agent-run persona walkthrough findings (`docs/v2/M5B_PERSONA_WALKTHROUGH_RESULTS.md`).

Status: **council advice; the owner decides.** INTERNAL document.

## Chairman's verdict (verbatim)

## Where the Council Agrees

1. **M5c should not go next.** All five advisors reject option B. Export, follow-up and the supply-chain map are built on answers and data that the walkthrough already found defective. A footnoted PDF makes the D4 citation error look more authoritative. A 600-edge map would show the reversed Apple CUSTOMER_OF Alphabet rows to everyone who opens it. Follow-up adds a new injection surface. With no named buyer, nobody has asked for any of these features.
2. **The measured problems are about trust, not missing features.** The cited passage contradicts the answer (D1, X4). A citation does not contain its claim (D4). Percentages show one year only. Edges are reversed. The "numbers not checked" badge shows on figures that do match XBRL. Rules cannot tell "not exposed" from "not matched". Every advisor ranks these above any new capability. This matches the owner's stated priority of correctness and honesty over features.
3. **M5c should depend on a buyer, not on the plan's order.** The council agrees M5c is speculative until a real buyer (P8) asks for something. If one does, PDF export is the cheapest and most likely request.
4. **If forced to choose between A and B, choose A.** Four advisors say so outright. The Contrarian says "neither as framed" but agrees that M5c should not lead.
5. **Buyer outreach (P8) costs no engineering time** and should start now, in parallel with whatever is built.

## Where the Council Clashes

1. **Will M2 fix the defects at all?** The Expansionist and, in part, the Executor call D1, D4 and X4 retrieval and grounding failures that BM25, RRF and rerank would address. The Contrarian, First Principles and Outsider point out that in D1 and X4 the right passage was retrieved and cited, and the model still answered wrongly. That is a synthesis or prompt fault, and reversed edges are an extraction or schema fault. Search work fixes neither. All five peer reviews sided with the second camp, and so do I: the evidence in the brief supports it. M2 may still help D4, the questions that look up quoted rule or ECCN IDs, and recall in general, but nobody has measured that yet.
2. **Answer the question as asked, or reject it?** The Executor and Expansionist pick A and plan it out. The other three say the question is mis-framed and propose a triage step first. Both sides have a point: triage is the right first move, but "neither" leaves no plan. Review 5 settles it: the triage can be the first days of M2 rather than a separate milestone.
3. **Should the benchmark be published?** The Expansionist wants a public methodology page as the product's moat. Everyone else notes that 17/23 judged correct and 0.88 against a 0.90 target are weak numbers to publish. Publishing them also conflicts with the owner's rule that public surfaces show strengths only. Keep the benchmark internal until its numbers are worth showing.
4. **Reversibility (raised only in Review 1).** M5c ships behind flags that are off by default, so it carries little risk in production. M2 changes the live retrieval path. This is true, but it argues the wrong way: a feature switched off delivers nothing either. The real cost of M5c is days spent building something with no consumer. The way to reduce M2's risk is the ablation plus a flag on the new retrieval path, not choosing B.

## Blind Spots the Council Caught

- **The defect evidence is stale.** The persona failures were measured on v20, before the redesign, v22 and v23. Re-run the same questions on v23 before committing days of work. Some may already behave differently.
- **The benchmark is too noisy for the planned ablation.** The ~20-question retrieval benchmark and the 23 LLM-judged items mean one question moves the score by about 4 points. A few-dollar ablation cannot detect a modest gain. Set a minimum detectable effect before spending anything, and grow the benchmark before changing retrieval, not after.
- **Several defects are hotfixes, not milestone work.** The false badge, the reversed edges and not-exposed versus not-matched are live now on the new front page. They should be fixed whichever option is chosen. One caveat the reviewers missed: under the owner's own lifecycle, a change touching the DB or LLM prompts is on the sensitive list and takes the full path. These fixes are not "a day" unless the change stays within the fast-path limits, so scope them honestly.
- **M6 was barely discussed.** The demo script, recorded fallback and runbook are what a first buyer meeting needs. If a meeting gets booked, M6 jumps the queue.
- **There is no stop rule or decision date.** Only the Executor gave one (no ablation gain in a week, move on). M2 needs a time-box and a kill criterion written into its spec.
- **The current evals can already measure the failure that matters.** They can check whether cited passages support their claims, which is cheaper than any new retrieval work as a first signal.

## The Recommendation

**Choose A, but reshaped: M2 first, with a triage front-end and a trimmed scope. M5c is not decided now; a named buyer triggers it, not the plan's order.**

1. **Triage, 1-2 days.** Re-run the known defects on live v23 with retrieval traces logged. Classify each one as retrieval, grounding/prompt, extraction or schema/UI.
2. **Fix the non-retrieval defects first.** These are reversed edges, the badge, not-exposed versus not-matched, and multi-year percentage values. Run each through the lifecycle path it actually qualifies for. Add a "cited passage supports the claim" check to the evals as the regression test.
3. **Trimmed M2, time-boxed (about 1-2 weeks), gated on the triage.** Grow the benchmark first, seeded with the known failures, and cap the labelling effort. Then add BM25 with quoted IDs plus RRF, and run one batched ablation against a minimum detectable effect fixed in advance. Defer edge cap/rank, extra token instrumentation and the stale-citation check unless the data shows they are needed. If the triage finds almost no retrieval misses, cut M2 to benchmark growth plus the grounding fixes. Kill rule: no measurable gain means stop and move to M6.
4. **Run P8 outreach alongside.** M5c reopens only when a named buyer asks for a feature, and then PDF export alone comes first. If a buyer meeting is booked before then, M6 (demo and fallback) comes before any M5c work.

Why: the credibility numbers (17/23, 0.88) and the defects a buyer would see first are the product's weakest layer. M5c would multiply that weakness. M2 as written only partly targets it. The triage costs cents and a day or two, and it decides how much of M2 is worth doing.

## The One Thing to Do First

Re-run the named defect questions (D1, D4, X4, one percentage question, the Apple/Alphabet relationship, one rule exposure on a company page, and one answer with a false badge) against live v23 with retrieval traces logged. Label each one retrieval, grounding/prompt, extraction or schema/UI. That table decides how much of M2 to build.
