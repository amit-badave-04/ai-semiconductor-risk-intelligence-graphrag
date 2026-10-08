# tools/mockllm

An OpenAI-compatible mock LLM provider for the staging load test (docs/v2/M5_PLAN.md section 6). Staging only: it is never
deployed to production and nothing in `src/semigraph` imports it.

## Run

```
uv run python -m tools.mockllm                                   # [::]:8000 (dual-stack; 0.0.0.0 without IPv6), real-time pacing
docker build -f deploy/staging/Dockerfile.mockllm -t semigraph-mockllm .
```

Point the service at it with `OPENAI_API_BASE=http://<host>:8000/v1`, `OPENAI_API_KEY=mock-<anything>` and the models
`openai/mock-luna` (draft and planner) and `openai/mock-sonnet` (escalation). Any model id is accepted; a request with `tools` is
the planner, a model id containing `sonnet`, `strong` or `opus` is the strong model, anything else the draft model.

## What it answers

* Drafts the real verifier accepts: each line is one sentence copied from a retrieved excerpt plus that excerpt's id, no number
  of its own, no removal wording (`answers.py`; proved in `tests/test_tools_mockllm.py` against `verify_answer` on recorded
  contexts and adversarial chunks). A well-formed id that the prompt does not hold is added to a draft with probability
  `invalid_id_rate` (draft role only), which the verifier rejects as `invalid_citation` and the service answers by escalating.
* Length and timing from `profiles.json`: time to first token (hidden reasoning time included), decode rate, visible length,
  hidden (billed, not streamed) reasoning tokens, escalation rate. Chunks are `chunk_interval_s` apart, not one per token.
* Planner mode: one tool call (`risk_changes` or `financial_metrics`, with companies read from the planner's first message),
  then `DONE` once a `tool` message is in the conversation.
* Usage: `usage` on the closing chunk when `stream_options.include_usage`, with `completion_tokens_details.reasoning_tokens`.
  Prices are not the mock's business: the service prices `openai/mock-*` as the real models (`serve/estimate.py`).

## Knobs and endpoints

| | |
|---|---|
| `POST /v1/chat/completions` | stream and non-stream; 429 with OpenAI's body and `retry-after` when `rate_429` fires |
| `GET /v1/models`, `GET /healthz` | |
| `GET /metrics` | JSON, contract version 1 (`metrics.py`): `process_cpu_seconds`, `uptime_s`, `cpu_count`, `inflight`, `inflight_max`, `requests_total` (every POST, 400s included), `bad_requests_total`, `rate_limited_total`, `rate_limited_by_role`, `invalid_id_injected_total`, `slow_ttft_injected_total`, `streams_*`, token totals, per-model and per-role counts. CPU % over a window = `(cpu2 - cpu1) / (t2 - t1) / cpu_count`. `?format=prometheus` for text |
| `GET/PUT /admin/knobs` | bearer `MOCKLLM_ADMIN_TOKEN` (closed when unset); JSON subset of `invalid_id_rate`, `rate_429`, `slow_ttft_rate`, `slow_ttft_s`, `retry_after_s`. This is how a fault phase starts: the service forwards no custom header |

Reconciling a fault phase: the service's draft call and planner call run with `num_retries=0`, so an injected 429 on them is one
failed draft (the service escalates) or one planner fallback to the plain retrieval; the strong call runs with `num_retries=2`, so
most of its 429s are retried away by the SDK and never reach the service as an event. Compare the service's events with
`rate_limited_by_role`, not with `rate_limited_total`.

Environment: `MOCKLLM_PROFILE`, `MOCKLLM_SEED`, `MOCKLLM_TIME_SCALE` (1 real time, 0 no waiting), `MOCKLLM_CHUNK_INTERVAL_S`,
`MOCKLLM_ADMIN_TOKEN`, `MOCKLLM_INVALID_ID_RATE` (default: the profile's escalation rate), `MOCKLLM_RATE_429`,
`MOCKLLM_SLOW_TTFT_RATE`, `MOCKLLM_SLOW_TTFT_S`, `MOCKLLM_RETRY_AFTER_S`, `MOCKLLM_HOST`, `MOCKLLM_PORT`, `MOCKLLM_IDS_PY`.
Per request (tests and direct probes): `X-Mock-Invalid-Id-Rate`, `X-Mock-429-Rate`, `X-Mock-Slow-Ttft-Rate`,
`X-Mock-Slow-Ttft-S`, `X-Mock-Seed`.

Measured on a development desktop (Windows, no uvloop): 60 concurrent streams of 3-6 s used 4.5% of one core, 4.2 ms of CPU
per stream. The Fly figure is for the first staging window to report.

## Calibrate

`profiles.json` today comes from the recorded deployed-path rows and the agent rows, so its timings are **provisional upper
bounds** (those rows have no time to first token; the 0.5 s of retrieval inside each latency is an assumption):

```
uv run python -m tools.mockllm.calibrate --v2e data/processed/eval_deployed.v2e.jsonl \
    --agent data/processed/eval_agent.v2.jsonl --out tools/mockllm/profiles.json
```

After the S12 live smoke (`scripts/latency_smoke.py --live`, owner's go, `--max-usd 0.50`) replace them with measured numbers:

```
uv run python -m tools.mockllm.calibrate --base tools/mockllm/profiles.json \
    --s12 artifacts/s12_latency_smoke.json --out tools/mockllm/profiles.json
```

`tests/data/mockllm_contexts.json` (the recorded contexts of the verifier tests) is regenerated with
`uv run python -m tools.mockllm.record_contexts`.
