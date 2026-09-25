# Stack discovery: agent, observability and framework layer (verified 2026-09-25)

Assumption: "langfloe" is Langfuse. VERIFIED means seen live. Version and date data come from PyPI JSON (https://pypi.org/pypi/PKG/json) and the GitHub API. Nothing was written to the repo.

## 1. LangChain and LangGraph
- **Versions (VERIFIED).**
  - langchain 1.4.2 (2026-09-18).
  - langchain-core 1.6.5 (2026-09-24).
  - langgraph 1.2.12 (2026-09-21).
  - langgraph-checkpoint-postgres 3.1.2 (2026-08-07).
  - langgraph-checkpoint-redis 0.5.2 (2026-08-20).
  - All are MIT.
- **Agent API (VERIFIED, https://docs.langchain.com/oss/python/migrate/langgraph-v1).**
  - `langchain.agents.create_agent` is the recommended API. It takes `system_prompt`, `response_format`, `middleware`, `checkpointer` and `state_schema`.
  - The v1 migration guide says `create_react_agent` is deprecated. No removal date is given.
  - It still imports in 1.2.12. I did not test whether it warns when called.
- **Built-in guardrail middleware (VERIFIED).** `ToolCallLimitMiddleware`, `ModelCallLimitMiddleware` (both take `run_limit`, `thread_limit`, `exit_behavior`), `ModelFallbackMiddleware`, `ToolRetryMiddleware`, `HumanInTheLoopMiddleware`, `PIIMiddleware`.
- **Streaming (VERIFIED).**
  - Modes are values, updates, messages, custom, checkpoints, tasks and debug.
  - `version="v2"` yields uniform `{type, ns, data}` chunks.
- **Interrupts (VERIFIED).**
  - `interrupt(value, *, response_schema=None)` is present in 1.2.12. I checked the signature and did not run it.
  - It needs a checkpointer and a `thread_id`. The node restarts from its beginning on resume.
- **Durability (VERIFIED by running it).** `astream` has `durability` with values `'sync'|'async'|'exit'`.
- **LangGraph Server (VERIFIED, https://docs.langchain.com/langsmith/deploy-standalone-server).**
  - A standalone Agent Server needs `LANGGRAPH_CLOUD_LICENSE_KEY`, plus Postgres and Redis.
  - The Developer plan has "no deployment access". Plus is $39/seat/month with 1 free small serverless deployment (https://langchain.com/pricing).
  - Whether a free Lite tier exists is UNVERIFIED.
  - Self-host the graph inside our own FastAPI. That path uses only the MIT libraries.
- **MCP (VERIFIED).**
  - `langchain-mcp-adapters` is archived (last release 0.3.2, 2026-08-06). MCP moved into `langchain[mcp]` as `langchain.mcp` (https://github.com/langchain-ai/langchain-mcp-adapters).
  - `langchain.mcp` is beta and pulled in `mcp 2.2.0`.
- **Provider packages (VERIFIED).**
  - langchain-litellm 0.9.0 is official under langchain-ai (`ChatLiteLLM`, `ChatLiteLLMRouter`, needs Python >=3.11).
  - langchain-openai 1.6.6.
  - langchain-anthropic 1.7.4.
  - langchain-google-genai 4.4.0.
  - langchain-groq 1.1.3.
  - langchain-openrouter 0.2.9.

## 2. Graph integrations
- **Neo4j.** `langchain-neo4j` 0.10.0 (2026-06-10), MIT, official langchain-ai repo.
- **FalkorDB package (VERIFIED, https://github.com/FalkorDB/langchain-falkordb).**
  - `langchain-falkordb` 0.2.0 (2026-07-06) is MIT and vendor-owned.
  - It has 10 stars and a single release.
  - It provides `FalkorDBGraph`, `FalkorDBVector`, `FalkorDBQAChain` and `FalkorDBSaver`. Async support for the saver is undocumented.
- **FalkorDB server (VERIFIED).** v4.20.7 (2026-09-24), SSPLv1.
- **Vector filtering (VERIFIED).** The docs say "No support for filtering" on vector queries (https://docs.falkordb.com/cypher/indexing/vector-index.html).
  - The HNSW index takes 1–4096 dimensions, so our 1024 fits.
  - This is a real gap against filtered vector search in Neo4j's Cypher 25 `SEARCH`. You would over-fetch and post-filter.
- **Read-only queries (VERIFIED).** `GRAPH.RO_QUERY` rejects writes and accepts a `timeout` in milliseconds.
- **Recommendation.** Build the tools on the raw `falkordb` client with parameterized templates and `RO_QUERY`. Skip `FalkorDBQAChain`, which needs `allow_dangerous_requests`. Skip `FalkorDBSaver`.

## 3. Observability: Langfuse as primary
- **Versions and license (VERIFIED).**
  - SDK 4.15.6 (2026-09-24). Server v4.45.4 (2026-09-25), with v3.225.x maintained in parallel.
  - MIT except `/ee`. The `/ee` features are project RBAC, protected prompt labels, retention policies, audit logs and SCIM (https://langfuse.com/self-hosting/license-key).
  - Tracing, evals, prompts, experiments and annotation are MIT with no limits.
  - The team has been part of ClickHouse since 2026-01.
- **Cloud pricing (VERIFIED, https://langfuse.com/pricing).**
  - Hobby is free: 50k units/month, 2 users, 30-day retention.
  - Core is $29/month for 100k units with 90-day retention.
  - Overage is $8 per 100k units.
  - A unit is a trace, an observation or a score. LLM-judge scores count too.
- **v4 self-host minimums (VERIFIED).**
  - ClickHouse >=25.12 (26.4 recommended), Postgres >=15, Redis >=7, and S3.
  - Web and worker containers need at least 2 CPU and 4 GB total.
  - The cheapest realistic setup is one VM with Docker Compose, which the docs say has no scaling or backups.
  - ClickHouse sizing is UNVERIFIED.
- **Integration (VERIFIED).**
  - `from langfuse.langchain import CallbackHandler` attaches through `config={"callbacks": [...]}`.
  - Session, user and tags go in via `langfuse_*` metadata keys.
  - OTLP goes to `/api/public/otel` over HTTP only (no gRPC). It needs Basic auth and the header `x-langfuse-ingestion-version: 4`. Python SDK 4.7.0 or later is required for real-time writes.
- **Untested.** I built the handler with tracing disabled, so end-to-end ingestion is untested.
- **Cost tracking (VERIFIED).**
  - Predefined prices cover OpenAI, Anthropic and Google.
  - For other models (DeepSeek, Groq, OpenRouter) you either add a custom model definition or send `cost_details` yourself.
- **Evals and prompts (VERIFIED).**
  - `run_experiment(task, evaluators, run_evaluators, max_concurrency)` writes scores back to Langfuse.
  - Managed LLM-judge evaluators and versioned, client-cached prompts are available.
  - Sampling is `LANGFUSE_SAMPLE_RATE`, applied per trace.
- **Unit load per request.**
  - I counted LangChain callbacks on a fake one-tool `create_agent` run (2 model calls, 1 tool call).
  - That gave 7 observations plus the trace, 8 units. With two limit middlewares it was 13 plus 1, 14 units.
  - Retriever, router and verifier steps are not included in that count.
  - A real multi-tool agent is likely 30–60 units, so Hobby covers roughly 800–1,700 questions a month. This is an ESTIMATE, and Langfuse's own mapping was not measured.
  - At 10k users, sample traces or self-host.
- **Phoenix (VERIFIED).**
  - Version 20.16.0 (2026-09-23), license **Elastic-2.0**. That is source-available, not OSI open source, and it bars offering Phoenix as a hosted service.
  - It runs as a single container on SQLite, or on Postgres for scale.
  - The `openinference-instrumentation-langchain` 0.1.76 package imports fine.
  - Skip it as a second layer.
- **LangSmith (VERIFIED).**
  - The Developer plan is free with 5k base traces/month and 1 seat.
  - Self-hosting is an Enterprise add-on.
  - The overage price per 1k traces is UNVERIFIED because the pricing page did not show it.
  - The SDK is MIT but the platform is closed. Skip it as well.
  - agentevals 0.0.9 is stale (2025-07-24).
- **Recommendation.** Run one layer: Langfuse.

## 4. Evaluation libraries (import tests in the ephemeral environment)
- **deepeval 4.2.6:** imports, including `FaithfulnessMetric`. Apache-2.0, released 2026-09-24. Use it as the CI gate.
- **trulens 2.14.0:** imports.
- **ragas 0.4.3:** **import fails.**
  - `ragas/llms/base.py:12` imports `langchain_community.chat_models.vertexai`, which no longer exists in langchain-community 0.4.2.
  - The last release was 2026-01-13 and the last push 2026-02-24, so it looks stalled.
  - The Langfuse Ragas guide depends on it. Do not adopt Ragas unless it is patched or pinned.

## 5. LiteLLM
- **Supply chain (VERIFIED, https://docs.litellm.ai/blog/security-update-march-2026).**
  - Versions 1.82.7 and 1.82.8 were compromised on PyPI on 2026-03-24.
  - Clean releases start at 1.83.0. Our 1.102.1 pin is later.
  - Hash-lock with `uv.lock`.
- **Proxy (VERIFIED).**
  - Budgets need Postgres. Multi-instance TPM/RPM limits need Redis.
  - Model-specific budgets and rate-limit tiers are Enterprise-gated.
- **Router (VERIFIED).** Routing strategies, cooldowns, retries and ordered fallbacks are available.
- **Recommendation.** Start with the in-process Router through `ChatLiteLLMRouter`, plus `ModelFallbackMiddleware`. Add the Proxy only when you need per-key budgets.

## 6. Web search for events after the corpus date
Gate this tool behind the router and cap it at about 2 calls per question.

| Provider | Free tier | Paid | Notes |
|---|---|---|---|
| Tavily (VERIFIED) | 1,000 credits/month | $0.008/credit | 1 credit basic, 2 advanced |
| Brave (VERIFIED) | $5/month credit | $5 per 1k | Storing results needs a storage-rights plan |
| Exa (VERIFIED) | $10 credit | $7 per 1k | Free credit is described as for new accounts; treat as UNCERTAIN |
| ddgs (VERIFIED) | free | none | MIT; metasearch scraper labelled "educational purposes only" |
| SearXNG (VERIFIED) | self-host | none | AGPL-3.0; depends on upstream engines that rate-limit scrapers |

At 10k users only Tavily or Brave paid tiers are enterprise-viable.

## 7. RAG agent design (my recommendation, UNVERIFIED as a design)
Use a hybrid built as a LangGraph `StateGraph`:
1. A cheap-model router classifies each question by structured output: numeric, relationship, risk narrative, export rule, post-corpus, or off-topic.
2. Most questions take a fixed retrieve-then-generate path. Deterministic XBRL lookups need no LLM loop.
3. Multi-hop questions go to a bounded `create_agent` tool loop.
4. A citation-verifier node runs last. Every claim must map to a returned evidence ID, and numbers must match tool output exactly.
5. If there is no evidence, refuse. Regenerate at most once, then strip the unsupported claim.

**Tools** are read-only, parameterized and return evidence IDs: `graph_neighbors` (depth <= 2), `xbrl_metric`, `risk_timeline` (active vs dropped lineages), `export_rules`, `hybrid_search` (BM25 plus vector, combined by reciprocal rank fusion) and `web_search`.

**Guardrails:** `ToolCallLimitMiddleware` (run_limit about 6; web at most 2), `ModelCallLimitMiddleware` (about 8), a recursion limit, timeouts, and fallback models.

**Text-to-Cypher risks:** write injection; unbounded or cartesian traversals; prompt injection carried in filing text; plausible but wrong schema guesses.
- Keep it out of the public path.
- If you offer it at all, use `RO_QUERY` with a timeout and human approval via `interrupt`.

**Streaming and persistence:**
- Stream `astream(stream_mode=["updates","messages","custom"], version="v2")` through `sse-starlette`, and emit citation events on the `custom` stream.
- Single-turn Q&A needs no checkpointer, or `durability="exit"`. Otherwise every user writes to Postgres on every step.
- Use `AsyncPostgresSaver` only for multi-turn threads and interrupts.
- The Redis saver is MIT and maintained by Redis Inc. It needs Redis 8 or Redis Stack.

## 8. Pinned-stack resolution test
Command: `uv run --no-project --python 3.13 --with "langchain[mcp]==1.4.2" --with langgraph==1.2.12 ...` in the scratchpad.
- **Passed.** All imports succeeded. An offline agent run also passed: `create_agent` with a fake model, a tool call, `InMemorySaver`, two limit middlewares, and `astream` in updates and messages modes.
- **Not tested.** Real model calls and real Langfuse ingestion.
- **Versions that worked:**
  - Python 3.13.13
  - langchain 1.4.2, langchain-core 1.6.5
  - langgraph 1.2.12, langgraph-checkpoint 4.2.0, langgraph-checkpoint-postgres 3.1.2
  - langfuse 4.15.6
  - litellm 1.102.1, langchain-litellm 0.9.0
  - falkordb 1.7.1, langchain-falkordb 0.2.0
  - fastapi 0.141.1, pydantic 2.13.5, uvicorn 0.54.0, sse-starlette 3.4.11
  - langchain-tavily 0.2.18, mcp 2.2.0, openai 2.54.0, opentelemetry-sdk 1.45.0, psycopg 3.3.6
- **Wider set: resolves only.** These resolve together with the core set but were not all import-tested (see section 4 for the ones that were): langchain-openai 1.6.6, langchain-anthropic 1.7.4, langchain-openrouter 0.2.9, langchain-google-genai 4.4.0, deepeval 4.2.6, ragas 0.4.3, trulens-core 2.14.0, openinference-instrumentation-langchain 0.1.76, arize-phoenix-otel 0.17.1.

Scratch files, all outside the repo, in `C:\Users\amit1\AppData\Local\Temp\claude\C--Users-amit1-OneDrive-Documents-Projects-ai-semiconductor-risk-intelligence-graphrag\7e3d5304-3ec1-40b7-9bfd-29c48c46aa97\scratchpad\`: `stack_smoke.py`, `stack_gaps.py`, `stack_reqs.in`, `stack_full.txt`.
