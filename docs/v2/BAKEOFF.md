# Answering-model bake-off (2026-09-26)

**Decision:** answer with **GPT-6 Luna by default**, route *change-over-time* questions straight to **Claude Sonnet 5**,
verify every cheap draft before it is shown, and escalate a rejected draft to Sonnet. Measured on the 20-question
benchmark through the deployed code path: **13/13 mechanical, 100 % citation validity, 6/7 open questions (19/20
overall, the same as the Sonnet-only baseline), $0.0069 per answer against $0.037, 6.0 s average against 8.6 s.**
Reverting is two settings (`LLM_MODEL` and `ESCALATION_MODEL` both `anthropic/claude-sonnet-5`, or `ESCALATION_MODEL` empty).

## Method

Every candidate answers the same 20 questions from the **saved retrieved context of the Sonnet baseline run** through a
byte-identical prompt (`eval/bakeoff.py::build_prompt`, round-trip checked on all 20 contexts), so a difference in the
answer is a difference in the model, not in retrieval. Order of work, cheapest first: answers (paid, checkpointed, capped)
-> free gates (mechanical correctness on the 13 deterministic questions, citation validity, the deterministic verifier)
-> the paid Sonnet correctness judge, **majority of 3 votes**, only for candidates that cleared the free gates. The
baseline is scored the same way. Prices come from LiteLLM's cost map (not the providers' pages); reasoning tokens are
included because providers bill them as output.

## Results (corrected scoring; the strict first pass is in `artifacts/bakeoff.strict-first-pass.json`)

| model | mechanical | citation validity | verifier rejects | $ / answer | avg latency | open questions (judge, 3 votes) |
|---|---|---|---|---|---|---|
| **baseline: Claude Sonnet 5** | 13/13 | 1.00 | 0.00 | 0.0371 | 8.6 s | **7/7** |
| **GPT-6 Luna** | 13/13 | 1.00 | 0.00 | **0.0013** | 5.0 s | 6/7 |
| Gemini 3.5 Flash-Lite | 13/13 | 1.00 | 0.15 | 0.0044 | 2.0 s | 6/7 |
| Gemini 3.8 Flash | 13/13 | **0.95** | 0.15 | 0.0110 | 3.0 s | not judged (citation gate) |
| DeepSeek V4 Flash (DeepInfra) | **12/13** | 1.00 | 0.15 | 0.0009 | 5.2 s | not judged (mechanical gate) |
| DeepSeek V4 Pro (DeepInfra) | 13/13 | 1.00 | 0.05 | 0.0135 | 3.6 s | 4/7 |
| Claude Haiku 4.5 | 13/13 | 1.00 | 0.05 | 0.0123 | 4.6 s | 4/7 |
| Qwen3.8-27B (DeepInfra) | abandoned after 8 answers: 51 s average, unusable against a 90 s request timeout | | | | | |

No candidate strictly clears the pre-registered gate (open-question correctness no worse than the baseline). Luna clears
every free gate and misses the paid one by a single question.

## Findings

1. **One stable quality gap, and it is the flagship.** Every cheaper model (Luna, Flash-Lite, DeepSeek V4 Pro, Haiku)
   scored 0/3 on `T3` "how has Nvidia's disclosed risk profile evolved" while Sonnet scored 3/3. The cheap answers
   relay the dropped-lineage block as fact (export-control risk "dropped"); Sonnet reasons past it (export-control risk "more
   prominent"). Temporal questions are the product's differentiator, so they are routed to Sonnet by a deterministic
   pattern (`retrieval/router.py`); with that route `T3` scores 3/3 on the deployed path.
2. **The remaining open miss is the judge, not the model.** `Q1` (TSMC supply-chain risks) is unstable even for Sonnet's
   own answer (2/3 votes; 1 of 3 in a separate re-judging). The judge sees only the answer, never the valid-id list,
   and its knowledge stops before 2026, so it calls 2026 accession numbers "fabricated". On direct reading the deployed Luna answer is correct (six valid
   citations to TSMC's 20-F, no hallucinated id). Three blind AI labellers rated the Sonnet answer correct unanimously.
   Follow-up: tell the judge that citations were mechanically verified, and re-validate it on `artifacts/judge_labels.json`.
3. **Cost is not only price per token.** Gemini 3.x thinks by default and bills the hidden tokens (312 reasoning tokens
   for a trivial question; `reasoning_effort="low"` removes them). Haiku 4.5 is only 3x cheaper than Sonnet and scored 4/7.
   DeepSeek V4 Flash is the cheapest ($0.0009) but missed a multi-step numeric question (`M1`) it could have answered.
4. **Call shapes differ per provider** (`llm_shape.py`): GPT-6 takes `max_completion_tokens` and rejects `max_tokens`;
   Gemini needs `reasoning_effort="low"`; the Anthropic path is unchanged.

## A scoring change made mid-run, disclosed

The first pass used the verifier and refusal pattern as originally written. Reading the failures showed two defects that
also hit the Sonnet baseline: (a) a correct XBRL-metric answer ("revenue was $215.9 billion") was flagged *uncited*
although the METRICS block has no chunk id to cite, and (b) correct refusals worded "do not state" / "I can't
determine" (typographic apostrophe) failed the notebook's refusal regex. Both were fixed for **every model
uniformly**: an uncited answer is accepted only when every dollar figure in it is found in the retrieved context, and the
refusal pattern was widened. No historical benchmark verdict changes (re-checked on all 80 saved v1/v2 runs). The strict
first-pass report is kept for audit. Before the fix the escalation rate was 0.05-0.35 for the candidates and 0.10 for the
baseline; after it, 0.00-0.15 and 0.00.

## Deployed path, end to end (`semigraph eval-deployed`, artifacts/eval_report.deployed.json)

17 questions answered by Luna, 3 routed to Sonnet, 0 escalations, 0 errors; mechanical 13/13; citation validity 100 %;
open 6/7 (miss = `Q1`, above); average $0.0069 per answer (cheap-routed answers cost about $0.0013, routed ones about
$0.037); average latency 6.0 s. A cheap draft is buffered until verified, so the first token appears after generation
(about 5 s) instead of streaming from about 2 s; the retrieval status line shows immediately.

## Limits

- n = 20 questions, 7 open; one open question is 0.14, above the plan's 0.05 tolerance, so the paid gate is coarse.
- Judge and labellers are the same model family as some candidates (self-preference risk); labels are AI-assigned.
- GPT-6 Luna is four days old at the time of writing; the rejected-draft escalation also covers a provider outage
  (a draft error escalates to Sonnet), but an OpenAI-wide outage would move all traffic to Sonnet.
- The verifier cannot catch a wrong but well-cited answer; the router and the benchmark are the mitigations, and a
  broader fresh-question comparison belongs with the M2 benchmark growth.
- Prices are LiteLLM's table, not the providers' pages; judge spend is estimated (calls are not metered).
- Spend: about $1.0 answering + $0.7 judging (bake-off), $0.14 + $0.15 (deployed run); project total about $8 of the ~$30 budget.
