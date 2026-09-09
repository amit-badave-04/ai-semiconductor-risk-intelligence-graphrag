# Evaluation methodology — alignment with Agentic_Evals

Reviewed against [abhineer/Agentic_Evals](https://github.com/abhineer/Agentic_Evals) on
2026-09-09 (all seven notebooks, the tool-use benchmark harness, and the resources list). That
repository evaluates agent *trajectories*: retrieval, tool selection, planning, memory, final answer,
then closes the loop with error analysis. semigraph is not an agent — one question, one
deterministic hybrid retrieval, one cited answer — so only part of the methodology applies.

## Applicability matrix

| Reference technique | Applies? | In semigraph | Status |
|---|---|---|---|
| RAG triad: groundedness (answer supported by context) | yes | `faithfulness` judge sees the **full** context the answerer saw (graph blocks + excerpts) | already correct |
| RAG triad: answer correctness, mechanical checks first, LLM judge only as fallback | yes | numeric ±0.5 %, `any_of` substrings, refusal regex, then the `Correct` judge | already correct (mirrors the reference `scorer.py` philosophy) |
| RAG triad: context relevance — **precision** | yes | `context_precision` per retrieved chunk (Haiku) | already present |
| RAG triad: context relevance — **recall** | yes | **added**: `context_recall` = required facts (from `judge_notes` / `expect`) present in the context, judged on the critic model, refusals skipped | implemented |
| Cost and latency per task | yes | live service ledger already had it; **added** to the offline benchmark: `latency_s`, `usage`, `cost_usd` per run, `avg_cost_usd` / `avg_latency_s` per system | implemented |
| Error analysis: failure taxonomy → targeted fix → re-evaluate | yes | **added** `semigraph.eval.error_analysis`: mechanical labels first (missed / over-triggered refusal, numeric miss, invalid citation, retrieval miss), one Haiku call otherwise; nine labels | implemented |
| Judge self-preference (same model judges itself) | yes | **added** `--judge-model` + `--rescore`: re-score checkpointed runs with another judge family without re-answering; report records `judge_model` | implemented |
| Synthetic evaluation data (dimension-based generation) | yes | benchmark is 20 hand-verified questions | **planned** — see below |
| Prompt A/B evaluation | partially | the answer prompt is locked to the benchmarked version | **planned** — the `systems` hook already accepts variants |
| Tool selection / arguments / sequencing evals and benchmark | no | no tool-calling loop | not applicable |
| Planning (direct vs ReAct) evals | no | no planning loop | not applicable |
| Memory recall / update / forgetting evals | no | stateless | not applicable |

## Running the new pieces

All of these spend API credit (judge calls); the confirmation prompt states which kind.

```bash
# full benchmark (answers + judges) with recall, cost and latency in the report
uv run semigraph eval

# re-score the checkpointed runs with a different judge family (judge calls only)
uv run semigraph eval --rescore --judge-model anthropic/claude-haiku-4-5 --report-suffix .haiku

# label every failing run with the failure taxonomy -> artifacts/error_analysis.json
uv run semigraph eval --rescore --analyze
```

Report fields added to `artifacts/eval_report.json`: `overall.context_recall`, `overall.avg_cost_usd`,
`overall.avg_latency_s`, `judge_model`. `artifacts/error_analysis.json` holds `rows` (one per failing
run: `label`, `mechanical`, `evidence`), `counts` per system per label, and `mechanical_share`.

Estimated cost for the 20-question × 2-system benchmark: recall adds ~40 Haiku calls (≈ $0.10),
the taxonomy a Haiku call per ambiguous failure, a Haiku re-scoring ≈ $1; a full re-answer stays
≈ $2–3 as before. **None of these were executed as part of the implementation** — the existing
`artifacts/eval_report.json` still reflects the M6 run; the new metrics appear on the next paid run.

## Planned (not implemented)

1. **Synthetic benchmark growth (20 → ~50).** Generate candidate questions over the dimension grid
   {question type × filer × XBRL metric × fiscal year × single/cross-filer} with one cheap LLM call
   per cell, then **verify every expected value programmatically against the graph / XBRL parquets**
   before a question is admitted (the project rule: ground truths are verified, never assumed).
   Needs a paid generation pass plus a manual review of judge notes.
2. **Prompt A/B harness.** `run_benchmark(systems=...)` already routes by strategy string; a prompt
   variant becomes a strategy that swaps `ANSWER_PROMPT`. Each variant costs a full re-answer
   (≈ $2–3), so this waits for a concrete prompt change worth testing.
