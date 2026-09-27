# M3-A: thin RAG agent - implementation plan (2026-09-26, fable-architect review of branch v2)

Status: design only. The agent is OPT-IN (`strategy=agent`, `AGENT_ENABLED=false` by default); the fixed retrieve -> answer path stays the default and the safe fallback. Any live agent run needs the rebuilt M1b graph.

## 0. Constraints verified in the code
- `serve/routes.py` calls `answer_stream` in exactly one place (`_paid_stream`); `guard.STRATEGIES = ("hybrid", "vector")` gates the request; `store.cache_key` already includes the strategy.
- The M1b contract lives in `retrieval/verify.py` (`answer_checks`, `verify_answer`, `failed_check_names`), `retrieval/context_layout.py` (`removal_supported_ids` reads the temporal block BACK from the context string) and `answerer.build_blocks` / `sources_from_context`. Everything the verifier knows it learns from the rendered context, so the agent must end by rendering the standard six-block context and must not invent a new answer format.
- The static page's SSE handler ignores unknown event names, so `step` events are backward compatible.
- `_paid_stream` is a sync generator in a threadpool (`max_concurrent_answers=2`); no async rewrite here (that is M5).

## 1. Architecture: the agent is a retrieval planner, not an answer writer
The planner (cheap model with tools) only decides which extra read-only lookups to run; tool results merge into the retrieval dict `r`; then `build_blocks(r)` -> `render_prompt` -> the existing buffered draft / verify / escalate. The planner never writes prose and never sees filing prose (ids, headlines, metric values, counts only): the main prompt-injection defence.

New package `src/semigraph/agent/`: `state.py` (`AgentState` TypedDict), `tools.py` (pydantic arg models -> OpenAI function schemas; closures over driver + embedder reusing retriever constants), `graph.py` (LangGraph `StateGraph`: `prefetch -> plan -> tools -> plan ... -> finalize`), `planner.py`, `stream.py` (`agent_answer_stream`, same signature and event grammar as `answer_stream` plus `step` events and an `agent` object on `done`).

Tools (all read-only, parameterised Cypher, NO text-to-Cypher on the public path; args validated; errors returned as `{"error": ...}`, never raised): `lookup_company`, `search_filings` (EXCERPTS_QUERY / VECTOR_QUERY; merges into `r["chunks"]`, cap 16), `financial_metrics` (METRICS_QUERY), `risk_changes` (TEMPORAL_QUERY + select_temporal + PASSAGES_QUERY; per-company replace), `relationships` (edges + rule edges, cap 40), `active_risks`, `compute_change` (pure code; emits a `computed: +x.x% vs fiscal year ended ...` line in the grammar `verify._COMPUTED_PERCENT_RE` grounds).

Limits (settings): `agent_max_tool_calls=4`, `agent_max_model_calls=3`, `agent_time_budget_s=25`, LangGraph `recursion_limit=12`. Exceeding a limit goes to `finalize` with what was gathered. A planner exception or unsupported tool calling sets `fallback_reason` and `r` = plain `hybrid_retrieve`: the agent degrades to today's behaviour, never errors because of the agent. `needs_strong_model(question)` still decides the WRITER; the planner stays cheap either way.

Shared-surface edits (step 0, serial): factor `answer_stream`'s tail into public `stream_answer_for_context(...)`; `build_blocks` appends `r.get("computed", [])` to METRICS (data, not template: `template_fingerprint` must not change); config flags (`agent_enabled`, planner model, limits, Langfuse settings with `repr=False` secrets); `guard.validate_strategy`; dependency pins.

Seam: `_paid_stream`: `stream_fn = agent_answer_stream if strategy == "agent" else answer_stream`; `/api/stats` adds `agent_enabled`.

## 2. Dependencies (verify by import on Windows / py3.13 and in the slim image)
ADOPT `langgraph >=1.2.12` (MIT); ADOPT Langfuse cloud Hobby, SAMPLED, via a fail-open `serve/tracing.py` (never sends raw IP, tokens or keys; needs owner sign-off + one privacy line on the page); ADOPT `deepeval >=4.2.6` in the dev group only (always pass a `LiteLLMModel`, `DEEPEVAL_TELEMETRY_OPT_OUT=YES`, plain pytest + `assert_test`); REJECT `langchain` `create_agent` for now (would re-learn `llm_shape.py` battle scars through LangChain's param mapping) and `langchain-litellm`; REMOVE `ragas` from the eval extra (import broken); DEFER web search; align `litellm >=1.100.0`.

## 3. Evaluation (DeepEval wraps, does not replace, the M1b instrument)
- T0 every PR, $0: numeric, citation validity, numbers grounded, refusal, misattribution, tool trajectory (`expected_tools` / `forbidden_tools` / `max_steps` in `artifacts/agent_benchmark.json`), limits, fallback, 6 canned prompt-injection chunks. Gate 100%.
- T1 rescoring saved runs, about $0.7.
- T2 weekly / paid, about $2-2.5 per run (ask first): 60 benchmark + ~20 agent questions through `agent_answer_stream`, then T1 scoring, compared against the fixed path.
- Gates: mechanical 100%; citation validity 100%; 0 ungrounded numbers; misattribution 4/4; refusals; judged correctness >= fixed path minus one question; p95 latency <= 15 s; blended cost <= 2x fixed path.

## 4. Work breakdown (disjoint files)
0 main (serial): pyproject, requirements, config, answerer factor, guard, stubs. A: `agent/*`, planner prompt, agent tests. B: `serve/routes.py`, `main.py`, `tracing.py`, `static/index.html` (agent option + step timeline only when enabled), tests. C: `eval/agent_eval.py`, `eval/deepeval_metrics.py`, `cli.py eval-agent`, `artifacts/agent_benchmark.json`, `scripts/probe_tool_calling.py`, tests. Then main: probes, local Neo4j integration, T2 run, Opus verification, flag on.

Live probes (< $0.05 each, ~$0.10 total): Luna tool calls (`max_completion_tokens`, parallel calls, `role: tool` round trip); Sonnet 5 with `thinking` disabled + `allowed_openai_params` (does the allow-list interfere with `tools`?); `tool_choice` auto / none; one masked Langfuse trace; one DeepEval metric via `LiteLLMModel`.

Estimate: 8-9 engineering days (3 workers in parallel ~ 3 calendar days + 2 integration), $4-6 API (only T2 crosses the $1 ask line). If only 60% fits: Langfuse -> local JSON trace writer behind the same interface; DeepEval -> T0 wrappers only; drop `compute_change`; keep the loop with 5 tools, limits, fallback, the routes seam behind the flag, injection tests, agent T0 and one T2 run.

## 5. Obsolete in the earlier plan after M1b
`risk_timeline` "active vs dropped lineages" tool (read RiskItem/RiskPassage via TEMPORAL_QUERY/PASSAGES_QUERY); an LLM router (keep the deterministic `needs_strong_model`); a separate citation-verifier node (verify.py already does it); structured claims `{text, cite_ids}` (would change the prompt and invalidate seeded examples); LiteLLM Router as the fallback layer (the fallback IS draft -> verify -> escalate); AI SDK stream encoder (M5).

## 6. Interface contract and acceptance requirements (fixed 2026-09-27, before the workers started)

Step 0 is committed (`0152842`, `05f40f6`, `a8b6c71`): `stream_answer_for_context`, the opt-in `strategy=agent` (guard, lazy import in `routes._stream_fn`, `agent_enabled` in `/api/stats`), `computed` lines in METRICS, the agent and Langfuse settings, the optional `agent` extra, and `scripts/probe_tool_calling.py`. The live probe (`artifacts/agent_tool_probe.json`, ~$0.013) found: **`openai/gpt-6-luna` rejects function tools on `/v1/chat/completions` unless `reasoning_effort="none"`** (a 400 otherwise); with it, single, parallel (two calls in one turn), `role: tool` round trip and `tool_choice="none"` all pass; Sonnet 5 passes all four unchanged.

**The one entry point** (`semigraph/agent/stream.py`, imported lazily by the route):

```python
def agent_answer_stream(question, driver, embedder, strategy="agent", *, timeout=None, max_tokens=1200,
                        escalation_model=None, settings=None, planner=None, tracer=None,
                        llm_stream=None, escalation_stream=None, **stream_kwargs) -> Iterator[dict]
```
Events: zero or more `{"event": "step", "n": int, "tool": str, "args": dict, "summary": str, "ok": bool}` (summary = counts / ids / fiscal years only, never filing prose), then exactly the `answer_stream` grammar (`retrieval`, `delta`*, `done` | `error`). `done` carries `agent: {"tool_calls": [{"tool", "args", "ok"}], "model_calls": int, "elapsed_s": float, "fallback_reason": str | None, "planner_model": str, "planner_usage": {...}, "planner_cost_usd": float}`. `planner` is an injectable callable (tests use a scripted fake); `tracer` follows `semigraph.agent.trace.Tracer` (duck-typed: `span(name, **attrs)` context manager whose handle has `set(**attrs)`, `event(name, **attrs)`, `generation(*, name, model, usage, cost_usd, input_chars, output_chars)`, `flush()`; none of them may ever raise). `settings` supplies the limits.

**Acceptance requirements (each one needs a test):**
1. **Spend is complete.** `_paid_stream` writes the ledger row and the daily ceiling from `done` / `error` `usage` and `cost_usd`; `stream_answer_for_context` prices only the writer. `agent_answer_stream` folds the planner's cost into BOTH terminal events: each model priced at its own rates (`llm_shape.KNOWN_PRICES_PER_MTOK`), dollars added, never tokens summed and priced once. Tests: a fake planner that reports usage, on the `done` path and on the `error` path.
2. **`compute_change` is fully wired or absent.** It accepts only facts that are already in `r["metrics"]` (fetched through `financial_metrics`), its line carries BOTH `[xbrl:...]` ids (so `valid_ids` and `sources_from_context` see them), and one end-to-end test through `verify.answer_checks` shows an answer citing the computed figure passes and one with the figure moved by a point fails (`verify._COMPUTED_PERCENT_RE` is the grammar).
3. **The Luna finding is enforced.** The planner call passes `completion_params(model, budget, reasoning_effort="none")` for `openai/` models; a unit test pins the kwargs sent to `litellm.completion`. Every happy-path T0 case asserts `fallback_reason is None` (a silent 400 -> fallback must not be scored as an agent answer).
4. **The time budget is real.** LangGraph runs synchronously here, so every planner call gets `timeout=min(remaining_budget, planner_call_cap)` and a test with a slow fake model shows the fallback fires inside the budget.
5. **Fail fast.** When `AGENT_ENABLED=true`, `serve/main.py`'s lifespan imports `semigraph.agent.stream` so a missing langgraph stops the service at boot, not on the first agent question.
6. The template fingerprint stays `ed30fa9cc9` (pinned in `tests/test_agent_seam.py`).

**Owner gates:** Langfuse cloud is NOT enabled by default (`tracing.py` is a no-op unless public key, secret key and host are all set, and needs the owner's sign-off plus a privacy line on the page; the default trace carries lengths and hashes, never the question or answer text). The T2 paid run (~$2-2.5) needs an ask. Every subagent call names its model (`sonnet`; `opus` for verification); at most 3 run at once; files are disjoint (A: `src/semigraph/agent/**`, `tests/agent/**`; B: `serve/{routes,main,tracing}.py`, `static/index.html`, serve tests; C: `eval/**` additions, `artifacts/agent_benchmark.json`, `cli.py` additions, DeepEval evaluation). Run tests with `.venv/Scripts/python -m pytest` (never `uv run`: OneDrive locks `.venv` while it rebuilds the project).
