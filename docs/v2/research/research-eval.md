# Evaluation stack findings (2026-09-25)

**Verdict:** Run DeepEval (offline and CI) plus Langfuse (traces, datasets, online judge, score store) plus our own deterministic domain metrics. Drop Ragas, TruLens, Phoenix and the LangSmith platform. Running all five is wasteful. It is not a compatibility problem: all five plus Langfuse, LangChain 1.4.2, LangGraph 1.2.12 and LiteLLM 1.102.1 co-installed and imported on Python 3.13 (172 packages, tested today). The waste is three overlapping sets of RAG metrics, three tracing UIs and duplicated judge spend.

## Live status (VERIFIED)
Sources: `pypi.org/pypi/<pkg>/json`, the GitHub API for each repo, and ephemeral `uv run --no-project --python 3.13` import tests.

| Tool | Version, date | License | Activity | Py3.13 import |
|---|---|---|---|---|
| DeepEval | 4.2.6, 2026-09-24 | Apache-2.0 | 100+ commits in the last 30 days (query capped at 100) | OK |
| **Ragas** | 0.4.3, **2026-01-13** | Apache-2.0 | last commit and last merged PR 2026-02-24; 0 commits since 2026-03-01 | **FAILS** |
| TruLens | 2.14.0, 2026-09-03 | MIT | 64 commits in 30 days | OK |
| Phoenix | 20.16.0, 2026-09-23 (evals 3.9.0) | Elastic-2.0 (not OSI open source; otel and client packages are Apache-2.0) | 100+ commits in 30 days | OK |
| LangSmith SDK | 0.14.0 | MIT | active | OK |
| openevals / agentevals | 0.2.0 (2026-04) / 0.0.9 (2025-07) | MIT | 7 commits in 30 days / 6 in 90 days | OK |
| Langfuse | 4.15.6, 2026-09-24 | MIT core; enterprise features separate | acquired by ClickHouse 2026-01-16, roadmap stays open source | OK |

- **Ragas is still broken here.** A bare `pip install ragas` followed by `import ragas` fails with `No module named 'langchain_community.chat_models.vertexai'`. Upstream issue #2995 has been open since 2026-09-04, and at least five fix PRs (#2862, #2957, #2979, #2991, #2997) are unmerged.
  - Workaround: pin `langchain-community==0.4.1`. Then imports, agent metrics, testset generation and `ragas.integrations.langgraph` all load. I tested imports only, no metric runs.
  - The legacy `ragas.metrics.Faithfulness` and `LangchainLLMWrapper` are deprecated in favour of `ragas.metrics.collections` with an instructor-based `llm_factory`.
  - `pyproject.toml` has `eval = ["ragas>=0.4.3"]`, which is unimportable on a fresh install.
- **LangSmith platform:** Developer is free with 5k traces. Plus is $39 per seat with 10k traces and 14-day base retention. Self-hosting is Enterprise only (https://www.langchain.com/pricing). openevals and agentevals work without it.
- **Langfuse cloud:** Hobby is free with 50k units, 30-day retention and 2 users. Core is $29 per month (https://langfuse.com/pricing). Self-hosting needs Postgres, ClickHouse, Redis and S3 (https://langfuse.com/self-hosting).

## Judge plug-in, agent traces, telemetry
- **Non-OpenAI judge**
  - DeepEval: `LiteLLMModel`, `AnthropicModel`, `GeminiModel`. **Gotcha, reproduced:** every metric constructor defaults to OpenAI and raises without `OPENAI_API_KEY`, even the deterministic `ToolCorrectnessMetric`. Pass `model=` on every metric.
  - Ragas: `llm_factory(provider="anthropic")` or the litellm adapter (docs via Context7).
  - TruLens: LiteLLM and Langchain providers. Phoenix: `LLM(provider=litellm|anthropic|google|langchain|openai)`. openevals: `judge=` accepts a LangChain model.
  - Langfuse managed judge: Anthropic, Google, Bedrock, or an OpenAI-compatible base URL, but the gateway must support tool calling.
- **LangGraph traces**
  - DeepEval: `deepeval.integrations.langchain.CallbackHandler` passed in the graph config, with trace-level or span-level metrics.
  - Ragas: `convert_to_ragas_messages`. TruLens: `TruGraph`. agentevals: `extract_langgraph_trajectory_from_thread`. Langfuse: `langfuse.langchain.CallbackHandler`.
  - Phoenix: `openinference-instrumentation-langchain` (import OK). UNVERIFIED that it covers LangGraph fully.
- **Telemetry defaults (all on except TruLens)**
  - Ragas: opt out with `RAGAS_DO_NOT_TRACK=true`. DeepEval (PostHog): `DEEPEVAL_TELEMETRY_OPT_OUT=YES`.
  - Phoenix: `PHOENIX_TELEMETRY_ENABLED=false`. Langfuse OSS: `TELEMETRY_ENABLED=false`.
  - TruLens: no phone-home found in the installed source (grep), and OTEL tracing is on by default.
- **Metric coverage**
  - DeepEval: Faithfulness, AnswerRelevancy, Contextual Precision/Recall/Relevancy, ToolCorrectness, ArgumentCorrectness, TaskCompletion, StepEfficiency, PlanAdherence, PlanQuality, TopicAdherence, GoalAccuracy, GEval, and a Synthesizer.
  - Ragas: has ToolCallAccuracy, AgentGoalAccuracy and TopicAdherence.
  - TruLens: tool_selection, plan_quality and execution_efficiency, but its groundedness is roughly one judge call per answer sentence, each carrying the full context. This is the reason it costs about 3-4x more (see the cost section).
  - Phoenix: Faithfulness, ToolSelection, ToolInvocation, Refusal.
- **Zero-cost functional checks passed:** DeepEval `ToolCorrectnessMetric` scored 1.0, agentevals `superset` trajectory match returned True, and Langfuse `RegressionError` exists.

## Overlap and composition
- **Redundant:** Ragas, DeepEval and TruLens implement the same RAG triad. Langfuse, Phoenix and LangSmith are the same trace and eval UI.
- **DeepEval is the CI gate.** `deepeval test run -n <procs> -c` gives pytest integration, parallel runs and a result cache.
- **Langfuse:** `run_experiment` gives datasets and run comparison. Live traces get a sampled online judge. Push any external score with `create_score`.
- **Optional:** agentevals or openevals as libraries only. Nothing else is needed.

## Mapping the repo harness (`src/semigraph/eval/`)
- **Replace with standard metrics**
  - `runner.py:186-196` faithfulness judge becomes FaithfulnessMetric. Keep the lesson at `:187-190`: pass graph blocks plus excerpts as `retrieval_context`.
  - `runner.py:198-211` recall and precision become ContextualRecall and ContextualPrecision. These need an `expected_output`, so convert `judge_notes` into reference answers.
  - `runner.py:181-184` open-ended correctness (7 questions) becomes GEval.
  - `runner.py:105-142` checkpointing, latency and cost become Langfuse dataset runs plus the DeepEval cache.
  - `runner.py:217-241` summary becomes framework output.
- **Keep as domain metrics**
  - `runner.py:74-87` and `:176-180` (numeric within 0.5% of XBRL, `any_of`).
  - `runner.py:168` citation-id validity.
  - The benchmark's expected-refusal flag. Replace the `REFUSAL_PAT` regex (`:69`) with an LLM refusal detector, since Phoenix `RefusalEvaluator` detects refusals but not whether they were appropriate.
  - `error_analysis.py:30-63` failure taxonomy, fed by framework scores. Wrap the deterministic checks as DeepEval `BaseMetric` subclasses.
- **Benchmark composition:** 13 of 20 questions are already mechanical (4 numeric value, 7 `any_of`, 2 refusal). Only 7 depend on the judge.

## Agentic evaluation and CI gate
- **Golden schema:** add `expected_tools`, `forbidden_tools`, `max_steps` and `must_refuse` to `benchmark.json`.
- **Tool selection:** `ToolCorrectnessMetric` or agentevals trajectory match, both deterministic.
- **Arguments:** `ArgumentCorrectnessMetric`.
- **Goal and trajectory:** TaskCompletion, StepEfficiency and PlanAdherence.
- **Step and cost efficiency:** step count and cost straight from Langfuse traces, which is free.
- **Tier 0, every PR, $0:** numeric XBRL check, citation validity, tool trajectory, refusal flag. Hard 100% on these.
- **Tier 1, prompt or retrieval changes:** rescore the saved answers in `data/processed/eval_runs.jsonl`, which already hold contexts and answers, so nothing is regenerated. Thresholds are floors set at baseline minus one question (0.05).
- **Tier 2, nightly or release:** full matrix through Langfuse `run_experiment`, failing on `RegressionError`.
- **Cost cap and cache**
  - Compute a pre-flight token estimate and abort above `EVAL_BUDGET_USD`.
  - `LiteLLMModel` accepts `cost_per_input_token` and `cost_per_output_token`.
  - Cache judge results keyed on metric, judge id, prompt version and inputs, using `-c` and a LiteLLM disk cache.
- **Noise:** one question is 5 percentage points. Haiku versus Sonnet as judge already moved hybrid correctness from 1.00 to 0.90 on identical answers (`artifacts/eval_report.json` versus `eval_report.haiku.json`). That gap is bigger than any candidate-model difference. Grow the benchmark to 100 or more questions; the XBRL numeric ones are free.

## Costed plan
Prices are VERIFIED:
- Anthropic pricing page: Sonnet 5 $2/$10 per million tokens, Haiku 4.5 $1/$5, batch −50%.
- Google pricing page: Gemini 3.5 Flash-Lite $0.30/$2.50.
- DeepSeek pricing page: V4.1 Flash $0.15/$0.60 off-peak, 2x at peak.
- OpenRouter live list (aggregator prices): gpt-oss-120b $0.15/$0.60, Qwen3.8 Flash $0.15/$0.47.

The token counts are my estimates (chars/4, Claude ×1.3, ±40%). They come from the 36 saved non-refusal runs (18 questions × hybrid and vector), which average about 9.6k context tokens for hybrid and 5.9k for vector. Per candidate that is about 1.35M judge input tokens and 0.13M output tokens.

| Judge (DeepEval 4-metric bundle, per candidate) | Cost |
|---|---|
| Sonnet 5 (current) | $5.24 |
| Haiku 4.5 | $2.62 |
| Gemini 3.5 Flash-Lite | $0.74 |
| DeepSeek V4.1 Flash / gpt-oss-120b | $0.28 |
| Qwen3.8 Flash | $0.27 |

- Ragas costs about the same as DeepEval, Phoenix about half, and TruLens about 3x.
- A reasoning judge with 3x output tokens costs $0.44 for DeepSeek and $1.41 for Flash-Lite.
- Answer generation for 40 runs per candidate (assuming about 500 output tokens): Sonnet 5 $1.13, Flash-Lite $0.15, DeepSeek or gpt-oss $0.06. The saved v1 runs recorded no usage (`cost_usd` null), so this is estimated.
- **Six candidates, judge plus answers:** Sonnet judge $34, Haiku judge $18, Flash-Lite judge $6.9, DeepSeek judge $4.2.
- **Calibration first, under $1:** rescore the 40 saved runs with two or three cheap judges. Compare them against `eval_scores.json`, `eval_scores.haiku.json` and about 40 hand labels before picking one. Use batch pricing and DeepSeek off-peak hours for nightly runs.

## UNVERIFIED or not done
- No live judge calls were made, so cheap-judge quality on this domain is unmeasured.
- TruLens ownership by Snowflake is recalled, not confirmed.
- Phoenix LangGraph coverage is recalled, not confirmed.
- Langfuse's definition of a billable unit is unconfirmed.
- Ragas was tested at import level only.

Scratch scripts and outputs (`t_*.py`, `out_*.txt`, `ev_*.py`) are in the session scratchpad. The repo was not modified.
