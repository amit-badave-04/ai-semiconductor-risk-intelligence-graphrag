**Facts report (verified by reading the files; line numbers are as read).** Two items were not read: the semantics of the hardened `llm_json` (only symbols located in `llm.py:35-66`) and the Neo4j seed and Dockerfile under `deploy/neo4j`.

All paths are under `C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag`.

## (1) Concurrency model

- **Endpoints.** All handlers in `src/semigraph/serve/routes.py` are `async def`. Blocking Neo4j calls go through `run_in_threadpool`, which uses Starlette's default anyio pool of 40 threads (my assumption; no code sets a larger one).
- **`/api/ask` paid path.**
  - It returns `EventSourceResponse(_paid_stream(...))` (`routes.py:161`).
  - `_paid_stream` is a sync generator, so it also runs in the threadpool. Each streaming answer holds one thread for the whole 15–80 s.
  - A `threading.BoundedSemaphore(max_concurrent_answers)` is taken non-blocking inside the generator (`routes.py:173`, `main.py:86`).
  - `max_concurrent_answers` defaults to 2 (`config.py:57`). Fly does not override it (`fly.toml` has no such env var).
  - The third concurrent paid question gets an SSE `error` event with `MSG_BUSY` (`routes.py:39, 174`).
- **Neo4j driver.**
  - The driver is created with `GraphDatabase.driver(uri, auth=...)` and no pool arguments (`main.py:37`).
  - The neo4j Python driver's default `max_connection_pool_size` is 100. That default comes from my knowledge of the driver, not from this repo.
  - `run_cypher` opens a new session per call (`graph/client.py:41`).
  - Each paid answer runs 5 sequential Cypher queries in `hybrid_retrieve` (`retriever.py:76-116`).
  - Each request also does several store queries. Cache-miss paid path: cache lookup, kill-switch check, daily-count scan, ledger write and cache write.
- **Rate limits.**
  - `RateLimiter` is an in-process dict of deques, unlocked and not thread-safe, capped at 20,000 buckets (`guard.py:24-44`).
  - There are three instances: paid, free and read (`main.py:82-85`).
  - Defaults: paid 5 per 600 s, free 30 per 600 s, read 120 per minute (`config.py:55-68`).
  - `guard.py:5-6` states the single machine is deliberate, so the per-process scope is intentional.
- **Global paid ceiling.** `max_queries_per_day` defaults to 150 (`config.py:54`). It is enforced by a Neo4j `count()` on `SvcQuery` (`store.py:54`) with no atomic reservation.
- **Fly.** One `shared-cpu-1x` machine with 2 GB (`fly.toml:34-36`). Its concurrency block sets `soft_limit=20` and `hard_limit=40` requests (`fly.toml:29-32`). The Neo4j machine is `shared-cpu-1x` with 1 GB, a 400 MB heap and a 200 MB page cache (`deploy/neo4j/fly.toml`).
- **Embedder.** The ONNX embedder runs with `ONNX_THREADS=1` (`fly.toml:18`). A query embedding runs in-process during retrieval, inside the thread that holds the answer slot.
- **What breaks first, by load:**
  - **100 concurrent users.** Fly's edge stops at `hard_limit=40` requests. Beyond that, requests queue or get rejected at the edge. The paid path admits only 2 answers at once, and about 3 of 4 paid questions would return "busy". Cached hits are served free, but they still do 2 to 3 sequential Neo4j queries each, plus a ledger write, on a 1 GB JVM with a 200 MB page cache. The daily ceiling of 150 paid answers also runs out fast. At about $0.06 per answer, the ceiling is about $9 per day (`RUNBOOK.md:73`).
  - **1,000 concurrent users.** Even one open SSE stream per user exceeds the edge's 40-request hard limit by a wide margin. Threadpool starvation is likely, because each paid stream holds a thread for 15 to 80 s. `/api/stats` runs a label scan on `SvcQuery` for the all-time count. The result is cached for 30 s (`config.py:61`) and computed per process. Another risk is that `SvcQuery` grows unbounded, since one row is written per query including cache hits.
  - **10,000 concurrent users.** Nothing in the design supports this. The limits are a single machine, a single Neo4j Community instance (no clustering), in-memory rate-limit state that cannot be shared across machines, and 2 concurrent LLM slots. The Anthropic rate limit is also a factor, and $0.04–0.06 per paid answer is the cost driver. `PRODUCTIONIZATION_PLAN.md` describes the design as "cost-capped", not scale-out.
- **No load test found.** I found no load-test artifacts or numbers in what I read.

## (2) Custom components vs. standard replacements

| Component | Location | Custom version does | Standard candidate | Keep or note |
|---|---|---|---|---|
| Rate limiter | `guard.py:24` | Per-IP sliding window with bucket cap, three instances | slowapi or fastapi-limiter (Redis), or an edge WAF | Nothing unique, but needs a shared store for multi-machine. |
| Client IP trust | `guard.py:47-56` | Honors only the configured header (`fly-client-ip`) | Starlette proxy-headers middleware with trusted hosts | Keep the trust logic; tests pin the spoof case. |
| Admission gate ordering | `routes.py:133-161` | Free window, cache, kill switch, daily cap, Turnstile, paid window, slot; no DB write before the first gate | None as a package | Differentiator; the order is pinned by tests. |
| Turnstile | `guard.py:80-105` | Fail-closed when `TURNSTILE_REQUIRED`, httpx verify | Direct Cloudflare API call or a small library | Small; keep fail-closed behavior. |
| Answer cache | `store.py:89-108` | Key is sha256 of strategy plus normalized question; 24 h TTL; benchmark rows never expire | Redis, or LangChain/LiteLLM cache (exact-match; semantic-cache options exist) | Seeded permanent benchmark answers and the "truncated answers not cached" rule. |
| Ledger / daily ceiling | `store.py:54-84` | Per-query rows with tokens and cost in Neo4j | Langfuse, LangSmith, or LiteLLM budgets and spend logs | Persisted global cap survives restarts; a proxy budget can do the same. |
| Kill switch | `store.py:43-49`, `routes.py:211-225` | Policy node plus env flag, admin route with constant-time token compare | Feature-flag service (Unleash, Flagsmith) or LiteLLM proxy | Small. |
| SSE framing | `routes.py:59-61` | sse-starlette `EventSourceResponse` (already standard) | LangGraph streaming events, `astream_events` | The `retrieval`/`delta`/`done`/`error` event schema is a contract with `index.html`. |
| Mid-stream error handling | `routes.py:193-199`, `answerer.py:264-272` | Logs spend on error, returns a generic message, releases the slot | Framework callbacks (LangChain, Langfuse) | Spend-on-failure accounting is a genuine behavior. |
| JSON repair / structured output | `llm.py:49-66` (`llm_json`) | Salvage JSON from prose, correction turn, truncation regenerate, budget doubling up to 8000 | LangChain structured output, Instructor, or LiteLLM `response_format` | Pinned by 11 tests. |
| LLM wrapper | `answerer.py:74-208` (`llm_text`, `TextStream`) | No sampling params, `thinking` disabled, transient-only backoff, empty and truncation retries, usage capture | LiteLLM (already used) or ChatLiteLLM/ChatAnthropic in LangChain | Sonnet-5 quirks are tied to the model. |
| Retriever | `retriever.py:65-133` | Alias-based anchors (no LLM), a fixed 5-query Cypher pipeline, Cypher 25 `SEARCH ... VECTOR INDEX`, bitemporal dropped-risk lineages | LangChain Neo4j retrievers, a GraphCypherQAChain, or FalkorDB with GraphRAG-SDK | The bitemporal queries and the XBRL metrics layer are the domain differentiator. |
| Citation verification | `answerer.py:41, 273-277` | `CITE_RE` extracts ids; `hallucinated = cited - valid_ids` (post-verified against retrieved ids) | None directly; approximated by Ragas faithfulness | Differentiator. |
| XBRL-only numbers | `build_blocks`, `answerer.py:54-55` | Metrics injected from graph facts, not model-parsed | None | Differentiator. |
| Verbatim-quote gate | `tests/test_extraction_gates.py` | Extraction gate rejects fabricated quotes | None | Differentiator; the code is not in the files I read, only its tests. |
| Eval runner | `eval/runner.py:105-142, 244-293` | Answers with a jsonl checkpoint per (id, system), so a paid run resumes; rescore with another judge | DeepEval or Ragas dataset runs, LangSmith `evaluate()` | Checkpoint-and-resume of paid runs. |
| Mechanical checks | `runner.py:69-87, 168-184` | Numeric ±0.5%, `any_of` substrings, refusal regex | DeepEval custom metrics | The generic packages don't ship these. |
| Error taxonomy | `eval/error_analysis.py:30-33` | Nine labels, mechanical labels first, Haiku fallback | None out of the box | Domain-specific. |

## (3) Frontend

- **Shape.** It is a single static file, `src/semigraph/serve/static/index.html` (191 lines): vanilla JS and CSS, no framework, no build step. It is served by `GET /` with `__TURNSTILE_SITE_KEY__` replaced at request time (`routes.py:63-67`). A CSP allows inline scripts and Cloudflare's challenge origin (`routes.py:28-30`).
- **Features.**
  - A stats strip showing companies, filings, evidence spans, risks, dropped lineages and BIS rules, plus live questions used out of the daily cap (`index.html:101-112`).
  - A list of the 20 benchmark questions grouped by type; a click asks that question immediately (`index.html:114-124`).
  - A question box limited to 500 characters (`maxlength=500`, matching `max_question_chars`) and a strategy selector, hybrid or vector. The option labels embed the benchmark claims ("100% correct" and "80% correct, 0% on temporal").
  - A Turnstile widget.
  - A streaming status line reading "anchors … relationships … XBRL metrics … generating".
  - A minimal hand-written markdown renderer, headings, bold and lists only (`index.html:84-99`).
  - Dark-mode CSS.
  - Ctrl+Enter to submit.
- **Citations.**
  - `[accession:section:seq]` ids in the answer are rendered as chips showing only the 4-digit sequence (`index.html:87`).
  - Clicking a chip fetches `/api/evidence/{chunk_id}` and opens a right-hand drawer (`index.html:168-178`).
  - The drawer shows filer, form, date and section title, mentioned companies, the verbatim excerpt, and a link to sec.gov.
  - A badge line reports the citation count and "all citations verified against retrieved context" or the count that were not.
- **Weaknesses for a lay audience.** These are my readings from the code:
  - Chips display cryptic numbers like "0042", not a company or filing name.
  - Jargon is unexplained: "bitemporal", "anchors", "XBRL", "dropped risk lineages", "BIS rules".
  - The strategy dropdown is developer-facing.
  - There is no conversation history, no follow-up questions and no share or export.
  - The markdown renderer is minimal (no tables or links).
  - The example list is a flat text list.
  - The retrieval status line is technical.
  - Token and cost badges are shown to end users.
  - The per-address quota is shown as a hint.
  - There is no loading skeleton and no mobile-specific layout beyond a basic responsive grid.
  - I found no accessibility work: no ARIA live region on the answer, and the drawer has `aria-hidden` but no focus handling.

## (4) Eval harness

- **Benchmark.** `src/semigraph/artifacts/benchmark.json` holds 20 questions:
  - numeric: N1, N2, N3, N4, M1 (5)
  - dependency: D1–D4 (4)
  - regulatory: R1–R3 (3)
  - temporal: T1–T3 (3)
  - risk: Q1–Q3 (3)
  - refusal: U1, U2 (2)
  - Each row has `expect.value` (numeric), `expect.any_of`, or `judge_notes`.
- **Systems under test.** `hybrid` and `vector` (`runner.py:244`).
- **Checkpoint.** An append-only jsonl at `<processed_dir>/eval_runs.jsonl`. Each row holds id, system, type, q, answer, cited, valid_ids, hallucinated, full context, chunk_texts, `latency_s`, `usage` and `cost_usd` (`runner.py:132-139`). Resume is keyed on (id, system) (`runner.py:120-128`).
- **Mechanical checks.**
  - The refusal regex (`REFUSAL_PAT`).
  - Numeric parsing with scale suffixes, within 0.5% of the expected value.
  - Substring `any_of` matching.
  - Citation validity, meaning `hallucinated` is empty.
  - Citation count, latency and cost.
- **Judges.**
  - Correctness (only for open questions) and faithfulness use `judge_model`, which defaults to `settings.llm_model` = `anthropic/claude-sonnet-5` (`config.py:21`).
  - Context recall and context precision use `settings.critic_model` = `anthropic/claude-haiku-4-5` (`config.py:22`).
  - Faithfulness is judged against the full context, truncated at 24,000 characters. It skips refusal questions.
  - A failed judge call records null for that one metric (`safe_judge`).
  - `--judge-model` and `--rescore` support judge diversity. `ragas>=0.4.3` is an optional extra in `pyproject.toml`, but `EVALUATION.md` and my project memory note that ragas' import is broken.
- **Metrics computed** (`summarize`, `runner.py:217-232`): correct, faithfulness, context_precision, context_recall, citation_validity, avg_citations, avg_cost_usd, avg_latency_s, plus correctness by question type. Outputs are `eval_scores.json`, `eval_report.json` and `error_analysis.json` under `artifacts/`.
- **Error taxonomy** (`error_analysis.py:30-33`): RETRIEVAL_MISS, UNGROUNDED_CLAIM, WRONG_ENTITY, STALE_RISK_LEAK, NUMERIC_MISMATCH, REFUSAL_OVERTRIGGER, REFUSAL_MISSED, FORMAT_OR_CITATION, OTHER. A run counts as failed if incorrect, if faithfulness is below 0.8, or if it has an invalid citation. Mechanical labels come first; otherwise one Haiku call with the `failure_taxonomy` prompt.
- **Recorded results.**
  - The Sonnet-judged M6 report (per `EVALUATION.md` and `runner.py:5-6`): hybrid 100% correct, 0.865 faithful, no hallucinated citations. Vector scored 80% correct and 0.943 faithful.
  - The Haiku-judged rescore: hybrid correctness 0.90, faithfulness 0.927, context recall 0.870 (`EVALUATION.md:50-59`). Judge calls are non-deterministic: repeated rescores differed by up to 0.02.
  - `avg_cost_usd` and `avg_latency_s` are null in the stored report because the checkpointed answers predate that instrumentation (`EVALUATION.md:75`).
- **Mapping to standard metrics.**

| Repo metric | Standard equivalent |
|---|---|
| faithfulness | Ragas `faithfulness`; TruLens groundedness |
| context_precision | Ragas `context_precision`; TruLens context relevance |
| context_recall | Ragas `context_recall` |
| correct (open) | Ragas `answer_correctness`; DeepEval GEval |
| cost, latency per run | LangSmith and Phoenix trace metrics |
| numeric, refusal and substring checks | No standard equivalent; they would be custom metrics |
| citation validity | Approximated by Ragas faithfulness; not equivalent |
| Error taxonomy and STALE_RISK_LEAK | No standard equivalent |
| Per-question-type split | No standard equivalent |

## (5) Tests

- **Count.** 11 test files, 186 grep matches for `def test_` and similar. The matches include parametrized tests, so I did not verify the exact pytest count.
  - `test_serve_api.py`: 29 matches, FastAPI TestClient tests.
  - `test_eval_pure.py`: 24.
  - `test_chunker.py`: 25.
  - `test_segmentation.py`: 18.
  - `test_retrieval_pure.py`: 17.
  - `test_temporal.py`: 16.
  - `test_serve_units.py`: 15.
  - `test_llm_json.py`: 15.
  - `test_extraction_gates.py`: 10.
  - `test_resolution.py`: 10.
  - `test_error_analysis.py`: 7.
- **What is mocked.** The CI comment says "LLM mocked, no Neo4j, no API spend" (`.github/workflows/tests.yml:1-3`). Tests inject fake `llm`, `judge`, `llm_stream` and litellm `completion`, and `monkeypatch` the store and driver. Nothing runs against a live Neo4j.
- **Behaviours pinned that a migration should preserve** (test names):
  - **Admission-gate ordering.**
    - Cached answers bypass the kill switch and the ceiling.
    - The kill switch returns 503 and the daily ceiling returns 429.
    - The per-IP window applies after its quota.
    - The free tier is gated before any DB write.
  - **Slot accounting.** A busy slot yields an error event and slot accounting stays correct (`test_busy_slot_emits_error_event_and_keeps_slot_accounting`). A stream failure releases the slot.
  - **Client IP.** A spoofed forwarding header is ignored unless configured; a configured header keys the limiter.
  - **Spend and caching.**
    - A mid-stream error still logs spend.
    - Truncated answers are not cached.
    - A stream emits retrieval, delta and done events, and the answer is then cached.
  - **Admin and bot gate.**
    - Admin auth: non-ASCII headers give 404, not 500; admin is disabled without a token; the token toggles the kill switch.
    - Turnstile required fails closed without keys.
  - **Read limits and headers.** Read endpoints are rate-limited, and the index sets the security headers.
  - **Citation rules.**
    - Citations are verified against valid ids, and hallucinated ids are flagged.
    - The citation regex extracts only valid ids.
    - `answer_stream` yields events and verifies citations; a mid-stream failure yields an error event with usage.
  - **LLM call quirks.**
    - Sampling params are never sent.
    - Thinking is disabled.
    - A truncated answer is regenerated with a doubled budget, capped at 8000.
    - Empty responses are retried.
    - The transient-only backoff and giving up after 4 attempts.
    - JSON salvage from prose.
  - **Eval scoring.**
    - Numeric tolerance of 0.5%.
    - Refusals are scored programmatically and skip faithfulness.
    - The judge gets the full context.
    - The Haiku critic model is used with `thinking_off=False`.
    - Resume from checkpoint.
    - Rescore with another judge.
    - A failed judge call yields a null metric.
    - The error taxonomy uses mechanical labels first.
  - **Pipeline.**
    - `test_extraction_gates.py` pins verbatim-quote acceptance: whitespace, case and smart-quote tolerance, and rejection of fabricated quotes.
    - The remaining files cover temporal lineage, entity resolution, segmentation and chunking. The chunker tests check that every chunk is an exact substring at its offsets.

## (6) Deployment

- **Fly apps.**
  - `semigraph` (`fly.toml`), region `sin`, VM `shared-cpu-1x` with 2 GB and 1 GB swap. It is always-warm: `auto_stop_machines="off"`, `min_machines_running=1`. Health check is `/healthz` every 30 s with a 180 s grace period.
  - `semigraph-neo4j` (`deploy/neo4j/fly.toml`), VM `shared-cpu-1x` with 1 GB and 1 GB swap, a `neo4j_data` volume at `/data`, and a TCP check on port 7687. There is no public service block: bolt is reachable only at `semigraph-neo4j.internal:7687` over the private 6PN network.
  - Neo4j runs with `NEO4J_db_query_default__language=CYPHER_25`, because the retriever uses the `SEARCH` clause.
  - Per `RUNBOOK.md:7-8`: the running API machine costs about $0.20/mo when scaled to 0, and the database about $0.55/mo when stopped. START and STOP are done through `scripts/ops.ps1`.
- **Env vars set in `fly.toml`:** `ENVIRONMENT=production`, `EMBEDDING_BACKEND=onnx`, `ONNX_MODEL_PATH`, `ONNX_THREADS=1`, `LLM_ANSWER_MAX_TOKENS=2400`, `CLIENT_IP_HEADER=fly-client-ip`.
- **Other settings** with defaults in `config.py` (for names and defaults I did not re-verify beyond the lines I grepped): `MAX_QUERIES_PER_DAY` 150, `RATE_LIMIT_*`, `MAX_CONCURRENT_ANSWERS` 2, `ANSWER_CACHE_TTL_HOURS` 24, `TURNSTILE_*`, `ADMIN_TOKEN`, `KILL_SWITCH`, `READ_RATE_LIMIT_PER_MINUTE` 120, `STATS_CACHE_SECONDS` 30, `LLM_REQUEST_TIMEOUT_S` 90. LLM list prices are configured at $2 in / $10 out per million tokens (`config.py:45-47`).
- **Secrets.**
  - Production values live in `.env.fly` (git-ignored). They are pushed with `python -m scripts.push_fly_secrets`, and `--app semigraph-neo4j` targets the database. Values travel on stdin and only key names are printed (`RUNBOOK.md:101-109`).
  - Fly secrets carry `ANTHROPIC_API_KEY`, `NEO4J_PASSWORD`, `ADMIN_TOKEN` and the Turnstile keys.
  - The Dockerfile bakes in no secrets.
- **Dockerfile (root).**
  - Stage 1 quantizes the Qwen3-Embedding-0.6B ONNX model to 8-bit weight-only via `scripts/build_onnx_embedder.py`.
  - Stage 2 is `python:3.13-slim`. It installs the pinned `deploy/requirements-serve.txt` with uv 0.11.20, then the `semigraph` wheel, and runs as a non-root `app` user.
  - The process is a single `uvicorn semigraph.serve.main:app --host 0.0.0.0 --port 8080`. There is no `--workers` flag, so it is one process.
- **CI** (`.github/workflows/tests.yml`), triggered on push to master/main and on pull requests.
  - `unit`: `uv sync --extra serve`, then `uv run pytest -q`, on Python 3.13.
  - `security-scan`: `pip-audit -r deploy/requirements-serve.txt` (advisory, `continue-on-error`) and gitleaks.
  - The paid benchmark and the ONNX fidelity build are manual only (`tests.yml:3`).
  - There is no deploy job; deployment is manual through `flyctl` and `ops.ps1`.

## Other repo details relevant to a migration

- `pyproject.toml` core dependencies: edgartools, litellm>=1.90.2, neo4j>=6.2.0, pandas, pyarrow, pydantic, sec-parser, sentence-transformers, tiktoken and typer. `serve` extra: fastapi, uvicorn, onnxruntime, tokenizers, sse-starlette and httpx. `eval` extra: ragas>=0.4.3. LangChain, LangGraph and Langfuse are not present. FalkorDB is not present either.
- The LLM layer is already LiteLLM-based, so it already supports provider swaps by changing `LLM_MODEL`. The Sonnet-5 quirks are hard-coded in `llm_text` and `TextStream`: `thinking={"type":"disabled"}`, no sampling params.
- Neo4j-specific coupling that would matter for FalkorDB:
  - Retrieval uses the Cypher 25 `SEARCH ... IN (VECTOR INDEX ...)` clause (`retriever.py:95, 113, 129`).
  - `graph/schema.py` applies the schema; I did not read it.
  - The service state (`SvcPolicy`, `SvcQuery`, `SvcAnswer`) also lives in Neo4j, as uniqueness constraints and an index (`store.py:119-128`).
  - `graph_stats` in `main.py:49-55` uses `labels(n)[0]`, and `bootstrap` in `main.py:58-66` calls `apply_schema`.
