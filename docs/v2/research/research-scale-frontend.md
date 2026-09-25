# v2 discovery, tasks A and B: 10k-concurrent scale and frontend

Tags: [V] means seen live, with the source named. [U] means recalled, secondary-source only, or unconfirmed. The repo is untouched (`git status` is clean).

Two headline findings:
- **The LLM tier is the constraint.** Compute costs about $1–2 per peak hour. The LLM costs about $460–6,500 per peak hour.
- **The v1 answer path cannot scale horizontally.** It has 2 answer slots today.

## A1. Load arithmetic

Little's law: streams = λ·S, where λ = 10,000/(think time T + stream time S).

| Case | T, S | Questions/s | Concurrent streams |
|---|---|---|---|
| Light | 300 s, 15 s | 31.7 | 476 |
| Base | 180 s, 17.5 s | 50.6 | 886 |
| Heavy | 120 s, 20 s | 71.4 | 1,429 |

With a 1.5× burst, Heavy reaches about 107 q/s and 2,143 streams.

- **Token load at Heavy, before caching.** Assume 16k input, 1k output and 2 calls per answer, which matches v1's measured 12–21k prompt tokens and $0.035–0.056 per answer (README.md:260). That is 4,286 answers/min, 68.6M input tokens/min (ITPM), 4.3M output tokens/min (OTPM) and 8.6k requests/min (RPM). A ReAct-style loop with 3 calls multiplies the input by roughly 1.9.
- **Cache.** I assume 40% of questions are served from exact-match cache or static starter answers. That is not a measurement. Static UI assets are 100% CDN. Semantic-cache hit rates are unknown and need real traffic, and a semantic false hit returns a wrong risk answer.
- **After 40% cache:** 41M ITPM, 2.6M OTPM, 857 streams. Cached answers replay in about 0.3 s (README.md:261).

## A2. v1 code that blocks horizontal scale

- `routes.py:164-201`: `_paid_stream` is a sync generator that holds a threadpool thread for 15–20 s. AnyIO's default limit of 40 threads is [U].
- `main.py:86` and `config.py:57`: `BoundedSemaphore(max_concurrent_answers=2)`.
- `guard.py:5-7,24-44`: the rate limiter is process-local, and its docstring says "deliberately ONE machine".
- `store.py:60-66,98-106` and `routes.py:145,152-154`: the ledger, answer cache, kill switch and daily budget all read or write Neo4j on every request.
- `fly.toml:29-32` sets soft/hard limits of 20/40. `fly.toml:18` sets `ONNX_THREADS=1` for the in-process embedder.

## A3. Provider capacity (16k input + 1k output per answer)

| Provider | Verified limit | Answers/min | q/s |
|---|---|---|---|
| Anthropic Start | 2M ITPM, 400k OTPM, 1k RPM, $500/mo cap [V platform.claude.com/docs/en/api/rate-limits] | 125 | 2.1 |
| Anthropic Build | 5M ITPM, 1M OTPM, 5k RPM, $1,000/mo cap [V] | 312 | 5.2 |
| Anthropic Scale | 10M ITPM, 2M OTPM, 10k RPM, $200k/mo cap [V] | 625 | 10.4 |
| OpenAI gpt-5-mini ($0.25/$2) Tier 4 | 10M TPM, 10k RPM [V developers.openai.com/api/docs/models/gpt-5-mini] | 588 | 9.8 |
| OpenAI gpt-5-mini Tier 5 | 180M TPM, 30k RPM; needs $1,000 paid [V] | 10.6k | 176 |
| Gemini Tier 3 | Spend cap $200 per 10 min; needs $1,000 paid + 30 days [V ai.google.dev rate-limits]; per-model RPM/TPM is only in AI Studio [U] | 2,740 at 3.5 Flash-Lite pricing | 45.7 |
| DeepSeek flash | 2,500 concurrent, 500 for v4-pro, free increase on request [V api-docs.deepseek.com/quick_start/rate_limit] | n/a | covers 1,429 streams |
| Groq | Free gpt-oss: 30 RPM, 8k TPM [V]; Developer tier "10×" [U] | n/a | n/a |
| OpenRouter | Free: 20 RPM and 50–1,000 requests/day; paid has no platform cap, upstream limits apply [V] | n/a | n/a |

- **Anthropic.** Sonnet 5 and Haiku 4.5 share these numbers. Only uncached input counts toward ITPM, and Anthropic warns that sharp ramps can return 429 ("acceleration limits") [V]. Even Scale is 3.7–6.9× short on ITPM at Heavy with 40% cache. Start's $500 cap is gone in about 12k answers.
- **Gemini.** Tier 3 at Gemini 3.8 Flash pricing gives about 21 q/s.
- **DeepSeek.** This is the only provider whose published concurrency limit covers Heavy. It is China-hosted, which needs a data-governance decision for user questions.
- **Sonnet 5 price.** Standard price is $2/$10 [V]. The tokenizer produces about 30% more tokens than pre-4.7 models [V].

## A4. Recommended lineup

| Layer | Choice |
|---|---|
| Frontend | Static export on Cloudflare Pages, free. Unlimited static requests [V]. |
| Edge | Cloudflare proxy plus Turnstile. Turnstile is free with unlimited challenges [V]. The free WAF allows 1 rule, IP-based, 10 s window [V], so real limiting lives in Valkey. Cloudflare's proxy read timeout is 125 s [V]; send SSE heartbeats. |
| API | Async FastAPI + LangGraph OSS library (MIT) + LiteLLM Router used as a library, on Fly. LiteLLM `max_parallel_requests` returns 429 rather than queueing [V], so add an app-level queue. |
| Cache and limits | Valkey for the limiter, exact cache and admission. |
| Graph | FalkorDB as immutable read-only copies loaded from the same dump. |
| Checkpointer | None; keep history client-side. `langgraph-checkpoint-redis` needs RedisJSON/RediSearch, which Valkey lacks [U, secondary]. |

- **Agent Server.** Do not use the LangGraph Agent Server. `langgraph-api` is Elastic-2.0 with a production license key [U, secondary].
- **FalkorDB.** It is SSPLv1 [V]. Reads scale by single-primary replicas with `GRAPH.RO_QUERY` [V]. A single graph cannot be sharded [V]. Vector queries "don't combine well with property filters" [V], so over-fetch and post-filter. The client supports asyncio [V]. FalkorDB Cloud has no HA on the free tier and costs $73/GB/mo on paid tiers [V].

**Platform comparison**

| Platform | Verified facts |
|---|---|
| Cloud Run [V] | Concurrency up to 1,000 per instance. Default 100 max instances. Timeout up to 60 min. Free tier: 2M requests, 180k vCPU-s, 360k GiB-s. $0.000024/vCPU-s request-based, $0.000018 instance-based. Cannot host FalkorDB. |
| Fly [V] | performance-1x 2 GB is $0.00001242/s (about $0.045/hr). shared-1x 1 GB is about $5.9/mo. Egress $0.02/GB. `fly-autoscaler` v0.3.2 (2026-06-19, 54 stars) only starts machines. |
| Railway [V] | $0.000463/vCPU-min. Pro allows 42 replicas. |
| Render [V] | Horizontal autoscaling only on the Pro workspace. Bandwidth $0.15/GB after 25 GB. |
| Koyeb | Pro $29/mo [V]. CPU prices not obtained [U]. |
| Hetzner [V] | After the 15 Jun 2026 increase: CAX21 €10.49, CAX31 €20.99, CX33 €8.49 per month. No managed autoscaling. |
| Oracle Always Free | Arm halved to 2 OCPU/12 GB from 15 Jun 2026 [U, InfoQ; Oracle returned 403]. Idle-reclaim risk. Not for the API tier. |

Pick Fly because the API needs private networking to FalkorDB and Valkey, and v1 already runs there. Use pre-created performance machines with Fly Proxy autostart and a derated `hard_limit`.

## A5. Measured benchmark

I ran one uvicorn worker with FastAPI, LangGraph `astream` (messages + custom modes) and a mock chat model at about 32–40 tokens/s per stream, with 100 ms frame coalescing. The machine was Windows 11 on a Core Ultra 9 275HX with Python 3.13, so the numbers are optimistic for a cloud vCPU.

| Concurrent streams | Worker CPU | TTFB p95 | Other |
|---|---|---|---|
| 50 | 12% | 4 ms | |
| 250 | 49% | 11 ms | RSS 378 MB |
| 500 | 92% | 769 ms | saturated, durations doubled |
| 1,000 | 97% | 4.4 s | 8 connection errors, RSS 1.1 GB |

- **Per stream.** About 55–75 ms CPU per stream and about 1 MB RSS per stream. Do not use the N=1000 CPU ratio, since that run was saturated.
- **Duration.** At N=50 the 21.7 s stream against 17.8 s ideal is a Windows timer artifact, not server cost.
- **Frames.** Per-token frames cost only about 17% more CPU than coalesced ones.
- **Capacity.** One fast core saturates at about 450–500 streams. Budget 150–200 per cloud vCPU (derating is [U]).
- **Not included.** Provider SSE parsing, the LiteLLM wrapper, TLS, network, and the embedder.
- **Embedder is unmeasured and probably the largest API CPU cost.** Order the pipeline as exact cache first, then embed.
- **Scripts:** `C:\Users\amit1\AppData\Local\Temp\claude\C--Users-amit1-OneDrive-Documents-Projects-ai-semiconductor-risk-intelligence-graphrag\7e3d5304-3ec1-40b7-9bfd-29c48c46aa97\scratchpad\bench_server.py` and `bench_client.py`.

## A6. Load-test plan

- **Tools.** Use Locust (MIT, 2.46.6). k6 is AGPL-3.0 and needs the third-party `k6/x/sse` extension [V].
- **Mock LLM.** `CopilotKit/llmock` has configurable time-to-first-token and tokens/s [V].
- **Target and generators.** Hit the origin, not Cloudflare's edge. Use at least 3 generator hosts because of ports and file descriptors.
- **Test shapes.** Ramp to 10k virtual users over at least 10 min, run a 3× spike in 30 s, and soak for 1 h. Inject 429s and slow responses.
- **Pass criteria.**
  - p95 TTFB at most 1.5 s.
  - At least 1,500 streams held with 0 dropped.
  - Error rate under 0.5%, excluding deliberate degradation.
  - Inter-chunk p99 at most 500 ms.
  - CPU at most 70%.
  - Retrieval-only degrade path emits a valid UI stream with 0 LLM tokens.
- **Degrade path.** When LLM queue wait exceeds 3 s or LiteLLM cooldown triggers, stream a status line plus `source-document` parts.
- **Proven by the mock:** fleet stream capacity, per-stream CPU and memory, limiter behaviour, FalkorDB read latency on real queries, the degrade path.
- **Not provable without paying:**
  - Real provider TPM and acceleration limits at 10k.
  - Real time-to-first-token under load.
  - Spend-tier qualification.
  - Answer quality of cheaper models.
  - True cache hit rate.

## A7. Cost (Heavy, 16k in + 1k out per answer)

Idle is about $25–30/mo for CF Pages, 2 API machines, FalkorDB, Valkey and Langfuse Hobby. v1 runs at about $17/mo online (README.md:262).

| Model | Per 1,000 answers | One Heavy hour, 154k LLM answers |
|---|---|---|
| Sonnet 5 | $42 | about $6,470 |
| Haiku 4.5 | $21 | about $3,230 |
| Gemini 3.8 Flash | $15.8 | about $2,430 |
| Gemini 3.5 Flash-Lite | $7.3 | about $1,120 |
| GPT-5-mini | $6.0 | about $920 |
| DeepSeek flash | $3.0 off-peak, $6.0 peak | about $460 off-peak, $920 peak |

- **Compute.** About 17 vCPU on Fly is about $0.8/hr, or about $1.2–1.7/hr on Cloud Run.
- **Langfuse.** Billable units are traces plus observations plus scores [V]. At 100% tracing (about 8 units per answer, my estimate), 154k answers/hr is about $94/hr at $8/100k. Sample at 5%, or self-host (Postgres, ClickHouse, Redis, S3 [V]).

## B. Frontend

**Recommendation: Next.js static export + Vercel AI SDK `useChat` + AI Elements + a custom citation drawer.**

- **AI SDK.** `ai@7.0.114` is Apache-2.0 [V npm]. The stream protocol is SSE with header `x-vercel-ai-ui-message-stream: v1`, and works from a Python backend [V ai-sdk.dev/docs/ai-sdk-ui/stream-protocol]. Parts include `text-*`, `tool-input/output-*`, `source-document`, `data-*` and `start-step`/`finish-step`.
- **Version churn.** Majors 5, 6 and 7 all shipped on 2026-09-24. Pin the version and add a protocol contract test.
- **Python adapter.** `@ai-sdk/langchain` is JS-only, so hand-write an encoder of about 100 lines. Feed it from LangGraph `stream_mode=["messages","custom","updates"]`, with `get_stream_writer()` for step events [V].
- **Citations.** AI Elements `InlineCitation` is a hover card only [V], so build a custom `[n]` chip and a shadcn Sheet drawer.
  - The backend validates `[S3]` markers against retrieved IDs and renumbers them.
  - It emits `source-document` with `sourceId=chunk_id`.
  - The drawer fetches `/api/evidence/{chunk_id}` (routes.py:120-130), which already returns filer, form, date, URL and text.
- **AI Elements.** It needs Next.js, Tailwind and shadcn [V].
- **Hosting.** Vercel Hobby is non-commercial only [V vercel.com/docs/plans/hobby]. Use Cloudflare Pages, where Next static export is supported [V].

**Rejected options**

| Option | Why |
|---|---|
| assistant-ui 0.15.22 (MIT) | Viable alternative. `assistant-stream` is 0.0.36, pre-1.0. |
| CopilotKit 1.73.3 / AG-UI 1.0.0 | AG-UI 1.0.0 is 8 days old. `ag-ui-langgraph` is 0.0.45, pre-1.0. Heaviest option. |
| `@langchain/react` `useStream`, agent-chat-ui | Expect a LangGraph Agent Server [V], which carries the Elastic-licence issue above. |
| Chainlit 2.12.0 | Community-maintained since 1 May 2025, Python <3.14 [V]. |
| Streamlit | One WebSocket session per tab, needs session affinity [V]. |
| Gradio | `default_concurrency_limit=1`, `max_threads=40` [V]. |
| Open WebUI | Branding clause once above 50 users [V]. |

None of Chainlit, Streamlit, Gradio or Open WebUI can sit on a CDN. Only the licence points above are verified.

**Graph visualization (all MIT, maintained [V])**
- **React Flow** (`@xyflow/react` 12.12.0): recommended. It has DOM nodes, keyboard navigation and ARIA, with WCAG 2.1 AA claims [V]. Lay it out with dagre in supply-chain tiers.
- **Cytoscape** 3.34.3: fallback above about 300 nodes.
- **sigma** 3.0.3: for thousands of nodes.
- **Skip:** `react-sigma` (last release 2020) and `react-force-graph` (last release Feb 2026).

**Screens**
1. **Home:** ask box, 6 starter chips, and a coverage strip (companies, filings, data through 25 Sep 2026).
2. **Answer:**
   - Collapsed plain-language step timeline, for example "Searched 3 SEC sources".
   - Answer with `[n]` chips.
   - Status badge for answered, partial or can't-answer, with the reason.
   - "N risks dropped, not supported" badge.
   - Follow-up chips.
3. **Source drawer:** filer, form, filing date, section, highlighted verbatim excerpt, SEC link, copy-citation. It is a side sheet on desktop and a bottom sheet on mobile.
4. **Supply-chain map:** click a node to filter answers and sources. A table-view toggle gives an accessible alternative.
5. **Busy state:** queue and retrieval-only banner, plus the daily-budget message.
6. **Method:** eval scores.

For mobile and accessibility, use a single column with 44 px targets, `aria-live=polite` on the answer, `prefers-reduced-motion`, and focus returning to the chip on drawer close.

Sources: cloud.google.com/run/pricing; docs.cloud.google.com/run/docs/about-concurrency; docs.fly.io/about/pricing; platform.claude.com/docs/en/api/rate-limits; platform.claude.com/docs/en/about-claude/pricing; ai.google.dev/gemini-api/docs/rate-limits; api-docs.deepseek.com/quick_start/rate_limit; docs.falkordb.com/operations/replication; ai-sdk.dev/docs/ai-sdk-ui/stream-protocol; elements.ai-sdk.dev; docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/; developers.cloudflare.com/pages; vercel.com/docs/plans/hobby; registry.npmjs.org; pypi.org/pypi/<pkg>/json.
