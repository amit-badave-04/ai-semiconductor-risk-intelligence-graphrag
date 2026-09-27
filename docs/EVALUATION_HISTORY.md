# Evaluation record (engineering history)

The full measurement history of the project, moved verbatim from the README so that the README stays a product page. Nothing was edited except relative links.

## Status note as of 2026-09-27

> **Status (2026-09-27): v2, milestone M1b built and measured; the change-over-time claims are hedged to what was
> measured, and four of the plan's ten quality gates still fail.** An independent review (2026-09-26) found that the
> "risks dropped from the latest annual report" answers contained false drops and that the benchmark could not see
> that. M1b replaced the drop layer with a text-grounded comparison of individual risk items and sentences between
> consecutive annual filings (all consecutive pairs are in the graph; a question names the pair(s) it wants), and
> re-measured with a source-text-grounded instrument. The service words every change claim only as strongly as its
> measured precision ("no longer appears as a separate risk factor", "wording was not found in the newer filing; a
> differently worded version may exist"), never "removed". **Not done yet:** the agent with tools (M3), document upload
> and freshness (M4), the new frontend and the 1,000-user serving layer (M5), and the search upgrade (M2).
> Read [docs/v2/M1B_PLAN.md](v2/M1B_PLAN.md) L.10-L.13 for the numbers and how they were obtained, and
> [docs/v2/REVIEW_2026-09-26.md](v2/REVIEW_2026-09-26.md) for what was wrong before. Figures from the earlier
> instrument are kept below as history only.

## Results

### v2 (2026-09-27): the deployed configuration on the 60-question benchmark

Production configuration (GPT-6 Luna answers, Sonnet 5 on escalation and for the correctness judge), graph snapshot
`snap-20260924-97c6597d58`; four runs, reports in `artifacts/eval_report.v2{,b,c,d}-deployed.json`, the full account (every
change made between runs, the judge's history, an independent closing review) in [docs/v2/M1B_PLAN.md](v2/M1B_PLAN.md)
**L.13 and L.14**.

| | first run (before fixes) | **final run (run 4)** |
|---|---|---|
| mechanical checks (41: numbers, citations, refusals, misattribution) | 38 / 41 | **40 / 41** (the same first-run answers score 41 / 41 under the final checks) |
| citation validity | 100 % | 100 % |
| open questions judged correct (23) | 10 / 23 (first judge) | **17 / 23** (accepted judge cj-v4); the first-run answers score 16 / 23 under the same judge |
| escalations to Sonnet | 1 / 60 | 2 / 60 |
| answers that fail their own checks | 12 | 5 |
| answers for 60 questions | $0.83 | **$0.85 (about $0.014 each, 4.3 s average)**, against about $0.047 each on Sonnet alone (a list-price estimate for 16k input / 1.5k output tokens, not a measurement) |

**How to read this.** The mechanical gain is entirely the checker: it was too brittle (it rejected correct refusals and
correctly hedged answers). The judged gain is mostly the judge: under one fixed judge the first-run answers score 16 and
the final ones 17, so the product effect on judged correctness is about one question. What the product work did change is
measurable elsewhere: answers failing their own checks fell from 12 to 5, questions about an older filing pair now read that
pair, and an empty-list sentence no longer cites unrelated text. The judge was revised three times after seeing failures and
is accepted against 19 frozen adversarial probes, but that acceptance is not blind, no probe covers a count that is within its
tolerance, and in use it misapplies that tolerance (T6 in the final run, and T4, T6, T7 when run 3's answers are re-judged), so the
final 17 contains at least one judge arithmetic error. Not met: mechanical 100 % (one answer stated revenue as $60,922,000,000 and the check
looks for "60.9"). Still failing: multi-year synthesis (T1, T3), a reworded-risk-factor count that differs from the annotators'
by more than two (T9: 18 against 14; T6 is a judge error), a retrieval miss (X4, M2 scope). The plan's held-out gates for the change layer
**do not all pass** (sentence-level removal precision 0.882 vs the 0.90 gate; risk-factor-level removal and "new" precision
1/3 and 6/12), so the service words changes as "wording was not found ..., a differently worded version may exist"
([L.11](v2/M1B_PLAN.md)). There has been **no human validation**: gold labels come from blind LLM annotators with
machine-checked quotes, and the owner's spot check was delegated to an LLM council (L.9).

> **Historical, withdrawn as current claims.** Every table below this line was produced by an evaluation
> instrument that could not detect false "dropped risk" claims or numeric errors on temporal questions (its
> judge was calibrated by AI labellers who saw the same retrieved context as the model, not the filing; the
> two example answers the review found wrong were rated correct unanimously). The benchmark is being
> **re-measured with a source-text-grounded instrument** in M1b; new numbers will be added from a committed
> artifact only. Details: [docs/v2/REVIEW_2026-09-26.md](v2/REVIEW_2026-09-26.md) sections 1-2 and
> [docs/v2/M1B_PLAN.md](v2/M1B_PLAN.md) section E. Mechanical checks (numeric values, citation ids,
> refusals) are unaffected; the correctness and temporal figures are the ones in question.

**M6 gold benchmark (v1, historical)** — 20 questions (numeric, dependency, regulatory, temporal, risk,
refusal), Claude Sonnet 5 answering and judging ([`artifacts/eval_report.json`](../artifacts/eval_report.json)):

| metric | hybrid GraphRAG | vector-only baseline |
|---|---|---|
| correctness (judge) | 20 / 20 | 16 / 20 |
| faithfulness (claims supported by context) | 0.865 | 0.943 (inflated by hedging and refusals) |
| citation validity (cited ids ∈ retrieved context; mechanical) | 100 % | 100 % |
| temporal questions (graded against notes that assumed the drop layer was right) | 3 / 3 | 0 / 3 |
| numeric questions | 5 / 5 | 4 / 5 (XBRL metrics vs prose) |
| context precision (relevant share of retrieved chunks) | 0.275 | 0.325 |

**v1.1 re-measurement (2026-09-26, historical)** — the same 20 questions on the graph rebuilt from data
through 2026-09-25 (snapshot `snap-20260924-7feaaf9bfe`: 74 filings, 166 BIS rules), Sonnet 5 answering and
judging ([`artifacts/eval_report.v2-baseline.json`](../artifacts/eval_report.v2-baseline.json)), same
instrument and same caveat; the table above is v1's original measurement:

| metric | hybrid GraphRAG | vector-only baseline |
|---|---|---|
| correctness (judge) | 19 / 20 (13 / 13 mechanical) | 15 / 20 |
| faithfulness | 0.892 | 0.903 |
| citation validity (mechanical) | 100 % | 95 % |
| context precision / recall | 0.275 / 0.745 | 0.325 / 0.657 |
| answer cost (list price, provider-reported tokens) | $0.037 | $0.023 |

Read honestly: the "correctness" and "temporal" rows above are the ones the review invalidated. The single hybrid
miss (TSMC supply-chain risks) was a *judge-unstable* item, and the "three blind labellers" behind
[`artifacts/judge_labels.json`](../artifacts/judge_labels.json) were AI passes that saw only the retrieved context,
so they could not check a claim against the filing (the M1b plan marks those labels superseded and relabels
from the source text). The vector numeric drop is the freshness model working as designed: a superseded annual
report's MD&A is no longer retrievable by default, and vector-only has no XBRL metric block to fall back on.

**Cross-judge re-scoring (historical)** — the same 40 answers re-judged by a *different model family* (Claude
Haiku 4.5) on 2026-09-09, with the new context-recall metric
([`artifacts/eval_report.haiku.json`](../artifacts/eval_report.haiku.json)):

| metric (Haiku judge) | hybrid | vector |
|---|---|---|
| correctness | 90 % | 85 % |
| faithfulness | 0.927 | 0.919 |
| **context recall** (facts the grading notes require that were actually retrieved) | **0.870** | 0.644 |
| temporal correctness | 0.67 | 0.33 |
| temporal context recall | 0.61 | 0.33 |

Read honestly: the second judge narrows the hybrid lead (it disagreed with Sonnet on two hybrid
answers and agreed with one more vector answer), the ranking holds, and the new recall metric shows
*where* the remaining gap is — temporal questions, where even hybrid retrieval surfaces only about
60 % of the required facts. Judge re-scoring is not fully deterministic: two runs of the same
command differed by up to 0.02 on faithfulness and by one labelled failure.

**Failure taxonomy** ([`artifacts/error_analysis.json`](../artifacts/error_analysis.json)) — 11 failing
runs (correctness, faithfulness < 0.8, or an invalid citation), 36 % labelled mechanically from the
scores, the rest by one Haiku call each:

| system | labels |
|---|---|
| vector | 3 × REFUSAL_OVERTRIGGER (declined although the answer was retrievable), 2 × RETRIEVAL_MISS |
| hybrid | 2 × STALE_RISK_LEAK (a dropped risk presented as current), 2 × UNGROUNDED_CLAIM, 1 × RETRIEVAL_MISS, 1 × NUMERIC_MISMATCH (judge label; the evidence text describes a truncated sentence, so treat this one as noisy) |

The two `STALE_RISK_LEAK` cases were read at the time as a prompt problem (the answerer receives the
dropped lineages as a separate block yet sometimes narrates them as current). The review found the deeper
cause: many of those "dropped" lineages were still in the newer filing, so the block itself was wrong
(section 1 of [the review](v2/REVIEW_2026-09-26.md)); M1b fixes the block first.
