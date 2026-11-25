# Decision 008: CP's displaced-excerpt gate (T-RET)

Date: 2026-11-26. Asked by the owner about the CP spec's rule "more than 10 displaced excerpts = automatic FAIL": "llm counsil it,
but I'm open to give it leeway, as I said i want functionality done fast on priority." Method: the `llm-council` skill (workflow
wf_48f9bde0-60a): five advisors (Sonnet), five anonymised peer reviews (Sonnet), a chairman (Opus).

Status: **applied to docs/specs/cp-add-company.md section 7 (T-RET v2)**, under the owner's leeway instruction. T-RET never
hard-fails; the exact gates (invariant diff, anchor-order golden test, tripwire, the paid benchmark's aggregate tolerance) still do.

## Chairman's verdict (verbatim)

## Where the Council Agrees

- **Reject option A as written.** All five advisors and all five reviews agree that "more than 10 = automatic FAIL with no owner discretion" sets a hard veto on a number nobody has measured. Across 480 excerpt slots (60 questions x 8), 10 is about 2%. That level could come from rebuild noise alone, or from the add working as intended (AI-chip questions now ranking Marvell passages). A false FAIL blocks the first company. A gate that gets waived by habit is a dead gate. Change the reviewer's rule back.
- **Reject option B as well.** T-RET costs $0 and gives a per-question early signal before any money is spent. The paid 60-question benchmark is a coarse, aggregate, late check. Dropping T-RET throws away the only cheap retrieval-level evidence.
- **Displacement is a proxy.** The real question is "did answers for the 13 get worse?" A Marvell passage displacing an existing excerpt is expected. An existing-13 passage displacing another is the real risk signal.
- **Measure the noise floor first.** It costs $0, takes minutes to an hour, and is the only basis for any threshold.
- **Hard fails stay where they are exact:** the per-company graph invariant diff, the anchor-order golden test, the new company's tripwire, and the paid benchmark's pre-registered tolerance. T-RET informs. It does not veto on its own.
- **The final shape is C (warn plus a recorded owner acknowledgement), with D's logic carried by the paid benchmark.** Response E (First Principles) was ranked strongest by all five reviews. Response C (Executor) came second for its per-question concentration trigger.

## Where the Council Clashes

- **Whether to keep 10 as a floor.** First Principles proposes max(noise + margin, 10). Others say 10 has no basis and should not survive even as a floor. Chairman: drop it. A floor nobody measured is still a number nobody measured.
- **The size of the margin.** Executor proposes noise + 5, Expansionist proposes 2x noise, and reviews call both arbitrary. Chairman: if retrieval is deterministic the noise is 0 and the margin is only a tolerance for legitimate churn. Pre-register a small fixed number now (below) and revisit after two adds.
- **Whether D is a hard gate.** Expansionist would fail only when collateral drift and a paid-benchmark regression hit the same questions. Reviews say 60 questions is too coarse per question and the rule conflates two gates. Chairman: use D as an escalation that requires a written reason, not as an automatic fail. This fits the owner's "leeway".
- **The "receipt" as a product feature (Expansionist).** All five reviews call it scope creep against "functionality first". Defer it. The T-RET report file is the receipt, internally.
- **Whether the noise measurement blocks the add.** Contrarian and Executor want it done first. First Principles would run it alongside the add. Chairman: run it first, because it costs less than the MRVL rebuild. If it slips, the MRVL add proceeds with T-RET advisory-only, and that is stated in the run log.

## Blind Spots the Council Caught

1. **Determinism was never checked.** If the replay and rebuild are deterministic (same seed, stable tie-breaks, the same vector index), the noise floor is exactly 0 and any displacement is real signal. If they are not, fix the cause first, for example with a stable secondary sort by chunk id on score ties, rather than widening the threshold. The byte-identical data-file refactor already gives one free data point for the graph, though not necessarily for retrieval order.
2. **Raw counts ignore rank.** Losing a question's rank-1 excerpt is far worse than a swap at rank 8. Track top-1 and top-3 losses separately.
3. **Concentration matters.** A total under threshold can hide one question collapsing, for example 5 of its 8 excerpts replaced.
4. **The "new company = expected" classification is partly circular.** An irrelevant Marvell passage that outranks a good one is exactly the entity-resolution drift the spec fears. So a Marvell replacement at rank 1 on a question that has nothing to do with Marvell's domain is still flagged.
5. **Pre-registration.** Write the rule and its numbers into the spec before the MRVL rebuild runs, and never edit them after seeing the result.
6. **Habitual waivers.** Acknowledgements need an audit trail and a review trigger.
7. **Re-anchoring.** For company 15 onward, the baseline is the last merged corpus, not the original 13.
8. **Process.** The owner's rules require this verdict to be saved to `docs/decisions/NNN-t-ret-gate.md`. "Leeway" means an acknowledgement path. It does not waive the exact gates.

## The Recommendation (an exact T-RET rule)

**T-RET v2 (pre-registered before the MRVL rebuild):**

1. **Determinism precheck ($0).** Run the 60-question replay twice against the same baseline graph, then rebuild the baseline twice on S and replay again. Record the noise N as the maximum number of displaced excerpts across the pairs.
   - If N > 0, add a stable tie-break and re-measure once. If N is still above 0, record it and add N to every threshold below.
2. **Report (always, never a verdict by itself).** For each question: the displaced excerpts with their old rank, and the incoming excerpt with its source labelled NEW (the added filer's chunk) or OLD (an existing-13 chunk). Totals:
   - D: all displaced excerpts.
   - C: collateral displaced excerpts, meaning the incoming excerpt is OLD.
   - T1: number of questions that lost their rank-1 excerpt.
   - Qmax: the most excerpts displaced in any single question.
3. **WARN** if any of these hold:
   - C > N + 5
   - any question loses its rank-1 excerpt to an OLD passage
   - any question has a NEW passage at rank 1 although its pre-registered topic is not the new company's sector
   - Qmax ≥ 4

   A WARN holds the merge until the owner writes a one-line acknowledgement in the add's run log. The acknowledgement names the flagged questions and cites their result in the paid benchmark.
4. **ESCALATE** if a WARN-flagged question also scores worse in the paid benchmark than in the baseline run. The merge then needs a written reason, not just an acknowledgement, stored with the run. There is no automatic FAIL here, because the owner asked for leeway.
5. **Hard FAIL** comes only from the existing exact gates: the invariant diff, the anchor-order golden test, the tripwire, and the paid benchmark's aggregate tolerance. T-RET never hard-fails.
6. **Review trigger.** If WARNs are acknowledged on two consecutive adds, the thresholds are re-derived from the recorded D and C data in a new decision note. Each add's baseline is the previously merged corpus.

Answer to the side question: yes, the threshold waits for the noise floor. The step is cheap, so the wait costs almost nothing. If the measurement slips, T-RET runs as report-only for MRVL, and the run log says so.

## The One Thing to Do First

Run the $0 determinism and noise check on scratch database S before the Marvell rebuild:
- replay the 60 questions twice on the current graph;
- rebuild the 13-company corpus twice and replay again;
- record N.

Then write the T-RET v2 rule above, with N filled in, into the CP spec and `docs/decisions/NNN-t-ret-gate.md`. Ask the owner to confirm in chat that the reviewer's no-discretion rule is reverted before the first add.
