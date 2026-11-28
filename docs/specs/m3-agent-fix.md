# M3 deep agent: fix it so it clears its ship gate

Status: **APPROVED by the owner 2026-11-28 ("Approve all (Recommended)": OD1-OD7 as recommended, $12 cap)** (owner, 2026-11-28: "fix this up very first"; earlier for the agent:
"fix it up correctly, make sure it works"). Path: **full** (LLM prompts and tools are on the sensitive list).
Diagnosis: workflow wf_6b426589-f46 (three Opus analysts over the failing gates; plan by fable-architect; Opus review; two root
causes spot-checked: X1 and T4). Production: `AGENT_ENABLED = "false"` (fly.toml) stays until the gate passes.

## Owner summary

**What is wrong.** The planner picks suitable tools and no tool returned wrong data. Three things fail the gate:
1. **The agent's writer sees a thinner context than the fixed path.** `without_detail_lines` (`src/semigraph/agent/merge.py:153-161`,
   applied at `agent/stream.py:97` and `:167`) drops Federal Register rule abstracts and edge quotes. That causes X1 and A22 (a "50%"
   figure only the dropped abstract supports becomes "ungrounded", which also triggers a slow Sonnet escalation; both are among the
   runs over 15 s) and part of X4 (the writer saw only the rule title).
2. **The shared writer prompt** (`src/semigraph/artifacts/prompts/answer.txt`, used by both paths) lets the writer obey "begin your
   answer with token X" (A18; `answer.txt:8-11` says to follow other formatting requests), repeat the asker's false figure while
   refuting it (A19), and volunteer "reworded" counts on yes/no change questions (T4, T7, T9, T15; against `answer.txt:81-84`).
3. **The planner prompt is stale against the tools** (A01, A14, R2, T11: the 4-company cap, over-long queries, `compute_change`
   across two companies, retries after a refusal).

**Also found (not agent code).** The correctness judge misapplies its own count tolerance (T4, T7, T8 and part of T15 were judged
wrong although within it), T9's gold note says 14 reworded where the data says 18, and the fixed path itself moved from 21/23 to
19/23 between two runs: the gate compares two single runs, so noise alone can fail it.

**Amendment 2026-11-28 (owner).** Decision "Rules as system message (Recommended)". The production check (`tests/security/promptfoo/m3_production_check.py`, results in `m3_production_results.json`) showed the escalation writer `anthropic/claude-sonnet-5` breaking the new rules R2-R4 (38/60) because the rules, the untrusted QUESTION and the evidence were sent as ONE user message, while GPT-6 Luna passed 57/60. For prompts rendered from `answer.txt` (the fixed path and the agent path share it) everything before the `QUESTION:` line (the Rules) is now sent as a SYSTEM message, and the QUESTION plus all context blocks as the USER message (`answerer.prompt_messages`, used by `llm_text`, `TextStream` and `AsyncTextStream`; other templates, the planner and the judge keep one user message). The two texts re-join to the rendered prompt exactly, so the metered and estimated character counts do not change. The answer-rule edits were also reduced to minimal wording inside the 9,600-character template allowance (the earlier output-rewriting guard was removed); that edit moved `template_fingerprint()` from `183eba08e7` to `7eea855d45` (fusion on: `53c88d4fdd` to `433d463331`), and the pins were updated.

**Cost.** About $3.5-3.8 per paired session (fixed-path re-benchmark about $1.5 + agent gate about $2.0-2.3), worst case about $10.

**Decisions for the owner (recommendation first).**
- **OD1 Context parity:** render the agent's context exactly like the fixed path (detail lines on), falling back to off only when a
  company's context would exceed the existing 50,000-character allowance; the $1.02 per-ask ceiling is unchanged (amends decision
  005's "detail lines off" choice). *Recommended.*
- **OD2 Shared writer-prompt fix:** edit `answer.txt` (both paths). Consequence: a paid fixed-path re-benchmark, regenerated
  examples and a production release (Release A). *Recommended* (an agent-only prompt would break the like-for-like gate).
- **OD3 Gate run as a paired session:** the fixed path and the agent run in the same session on the same commit, and the gate reads
  that pair. If the first session fails only on judged correctness by 2 or less, one repeat session is allowed (pre-registered
  here), never a relabel. *Recommended.*
- **OD4 Spend cap:** $12 for this milestone, stop after two failed paid sessions. *Recommended.*
- **OD5 T9 gold:** correct the gold note to the data's count (18) with the evidence, or leave it and accept the miss on both paths.
  *Recommended: correct it*, recorded as a gold fix, applied to both paths.
- **OD6 Judge noise (T4, T7, T8):** a separate judge-quality task; no judge or gate edit here. *Recommended.*
- **OD7 CP's `lookup_company` change:** stays with CP and its own gate run. *Recommended.*

Note: `semigraph bakeoff` (src/semigraph/eval/bakeoff.py) keeps the single-message shape on purpose, to stay comparable with its
saved v2 baseline; it is not part of the M3 gates and does not reach production.

## 1. Goal and fixed constraints

Clear every **gated** check of `src/semigraph/eval/agent_eval.py` in one paired session (OD3), judge cj-v4, 3 votes.
- `agent_eval.py` and `src/semigraph/artifacts/prompts/correctness_judge.txt` are **not edited**. `mechanical` and `misattribution`
  are reported, not gated (`agent_eval.py:792`); they are still fixed here and still reported as they are.
- `tests/test_m2a_spend_budget.py` keeps its $1.02 ceiling and 50,000 allowance assertions unchanged.
- A planner prompt change bumps `PLANNER_PROMPT_VERSION` (`agent/planner.py:30`).
- `stream.py:5-6` promises a byte-identical prompt when the agent adds nothing; that becomes true (R1).

## 2. Requirements and acceptance (each failing gate has one)

| ID | Gate(s) and questions | Requirement | Acceptance test(s) |
|---|---|---|---|
| R1 | ungrounded_numbers, checks_clean (A22, X1), misattribution (X1, X4), p95 | One context renderer for both paths: detail lines on, off only past the per-company allowance; used at `agent/stream.py:97`, `:167` and `serve/stream_async.py:416`, so the writer and the verifier see the same sources | `test_agent_context_equals_fixed_when_tools_add_nothing`, `test_detail_lines_fall_back_only_past_allowance`, `test_verifier_sources_match_writer_context`; $0 replay: the 60 shared questions rendered on local Neo4j, byte-diff empty wherever no tool changed the context |
| R2 | injection (A18) | The writer follows only length, tone and list-style requests; it never emits a string the question dictates | `test_answer_prompt_rules` (rule text present); promptfoo `m3_writer_rules.yaml`: dictated-token cases, 3 runs each, pass rate 100% on the local model; A18 in the paid gate |
| R3 | injection, ungrounded_numbers, checks_clean, mechanical (A19) | When refuting a figure only the question states, the writer does not restate it | promptfoo cases (false-premise figures), 100%; A19 in the paid gate |
| R4 | judged correctness (T4, T7, T9, T15) | On yes/no change questions the writer gives no "showing x of y" total unless asked how many | promptfoo cases on rendered T-type contexts, 100%; the four questions in the paid gate |
| R5 | trajectory (A01, A14, R2, T11) | Planner prompt v2: the answer-wide 4-company cap, a query of 300 characters or fewer as keywords, `compute_change` for one company's own two period ends, after a refusal fix one argument or stop; all calls in the first turn. `prefetch_summary` shows companies covered, room left and edge relations per company (allowlisted view only). `compute_change` refuses a two-company request with a clear message | `test_agent_planner_v2_*` (mocked LLM), `test_prefetch_summary_shows_coverage`, `test_compute_change_refuses_two_companies`; $0 replay of the 82 recorded agent runs through the new validators |
| R6 | p95 latency (15.6 s vs 15 s) | Covered by R1 (no futile escalations) and R5 (fewer retries); span timings are persisted in the run rows so the cause is measurable | `test_eval_rows_carry_span_timings`; the gate's p95 in the paid session |
| R7 | judged correctness (overall) | Clears in the paired session (OD3) with no gate or judge edit | the paid report: `clears_all_gates: true` |

## 3. Security requirements and abuse cases (checkable)

- S1 Direct injection: instructions in the question (dictated tokens, "ignore the rules", false figures) are not obeyed or echoed:
  promptfoo `m3_writer_rules.yaml` + gate cases A18, A19, A22.
- S2 Indirect injection: text inside tool results, filing excerpts, rule abstracts and uploads is data: abstracts and quotes pass
  `sanitise_untrusted`; the planner view holds only counts, ids and canonical names, never filing or upload text
  (`test_agent_sanitize.py`, `test_agent_isolation.py`); promptfoo cases with instructions inside an abstract.
- S3 Tool misuse: every tool argument stays schema-validated; no tool outside the allowlist; a refused call is not retried with the
  same arguments (`test_agent_tools.py`, `test_agent_planner.py`).
- S4 Limits unchanged and enforced: 4 tools, 3 model calls, 25 s, $1.02 per ask (`test_m2a_spend_budget.py`, existing limit tests;
  gate `limits_respected`, `spend_consistent`).
- S5 No leakage: the tracer and logs carry no question or answer text; no system prompt or PII is echoed (existing tracer tests;
  promptfoo leakage case).

## 4. Increments (test-first; LLM mocked in unit tests)

Shared surfaces first, serially: `answer.txt`, `retrieval/context_layout.py`, `agent/merge.py`.
1. **I1 Context parity (R1, R6 timings).** RED/GREEN as listed; then the $0 render-and-diff on local G.
2. **I2 Writer rules (R2-R4).** Prompt edit + promptfoo eval cases written first (RED = below threshold on the local model).
3. **I3 Planner v2 (R5).** Prompt, summary view, tool descriptions, the two-company refusal; $0 replay of 82 runs.
4. **I4 Regression gate.** Full suite (coverage >= 80%), `ecc:code-reviewer`, `ecc:security-reviewer` (Opus), Opus verifier.
5. **I5 Paid session (after approval).** `semigraph eval-agent --dry-run` and the fixed-path dry run for the quotes; then the fixed-path
   re-benchmark and the agent gate in one session (cap per OD4); one repeat only under OD3's rule.
6. **I6 Releases.** Release A: the shared prompt change with the agent still off, examples regenerated, the fixed path's own
   non-regression on the old 60. Release B (only if the gate passed): flip `AGENT_ENABLED = "true"`, live checks, rollback = the flag.

## 5. Risks (where the obvious implementation is wrong)

- Triggering parity on "zero tool calls" is wrong: `merge_metrics` widens `metric_periods` even at 0 new rows; use the size rule.
- Changing one call site only is wrong: the verifier's sources come from the rendered context, so writer and verifier diverge.
- An agent-only writer prompt avoids the re-benchmark but breaks the like-for-like comparison.
- Prompt fixes are probabilistic; the trajectory gate allows zero tool errors. If T11 recurs, clip the query at the adapter as a
  follow-up (test-first).
- The shared prompt change moves the answer-template fingerprint: the cache and examples are re-keyed (Release A).
- Not yet verified: X1's and A22's own tool calls; that the abstract carries the "50 percent" text (inferred from the fixed row);
  which host measured the gate's latency.

## 6. Paid plan

Dry runs first; printed estimates; the OD4 cap enforced by the existing ledger; stop after two failed sessions. Likely $3.5-3.8 per
session, worst about $10; plus about $1-1.5 for Release A's examples regeneration. Spend is recorded privately, not in the repo.

## 7. Acceptance checks for the verifier

- `git diff --stat` shows no change to `src/semigraph/eval/agent_eval.py` or `correctness_judge.txt`.
- Full suite green, coverage >= 80%; `test_m2a_spend_budget.py` ceiling assertions unmodified.
- The parity diff over the 60 shared questions is empty wherever no tool changed the context.
- promptfoo M3 cases at their thresholds; the paid report shows `clears_all_gates: true` for a paired session, spend within cap.
- `fly.toml` keeps `AGENT_ENABLED = "false"` until that report exists.

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves this spec and OD1-OD7 | **DONE 2026-11-28:** approved as recommended |
| 2 Dev (TDD) | I1-I3 RED then GREEN; reviewers clean of CRITICAL/HIGH | |
| 3 Regression | full suite, coverage, promptfoo thresholds, $0 replays | |
| 4 Security | `/security-review`, gitleaks, semgrep on the diff; owner `/red-team` before Release B | |
| 5 Fix findings | per stage 5 | |
| 6 Re-test and verifier | Opus verifier | |
| 7 Push, CI, deploy | Release A, then the paid session, then Release B if the gate passes | |
| 8 Archify | applies only if the architecture changes (it should not) | |
