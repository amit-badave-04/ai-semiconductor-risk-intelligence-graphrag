# ADR 0001 — Production stack for the semigraph web service

Status: accepted (2026-09-09). Context: turn the M7 SDK into a live, public, cost-capped product,
reusing the operating pattern of [clinic-voice-agent](https://github.com/amit-badave-04/clinic-voice-agent)
where it fits, open-source where possible.

## Decisions and evidence

| # | Decision | Alternatives weighed | Evidence (measured in this repo) |
|---|---|---|---|
| 1 | **Fly.io Machines** for both the API and the database, region `sin`; API **always-warm while online** (`min_machines_running = 1`, auto-stop off) — amended 2026-09-09 after launch | scale-to-zero (the first choice) | scale-to-zero made every first visit pay a ~10 s cold start (machine boot + 1 GB embedder load), which visitors experienced as a broken link; cost is controlled by the STOP script instead (≈ $11/mo for the API while online) |
| 2 | **Neo4j Community 2026.07 self-hosted** on a second Fly machine with a 3 GB volume, private 6PN only | AuraDB Free ($0, no card, but requires an Aura account + instance created by the owner, auto-pauses after 72 h idle, deleted after 90 days paused); Railway template (Neo4j 5.26 — no `SEARCH` clause) | the retrieval code needs Cypher 25 `SEARCH` over two 1024-dim vector indexes; the Community image accepts `NEO4J_db_query_default__language=CYPHER_25`; running cost ≈ $5.9/mo, stopped ≈ $0.6/mo; swapping to Aura is one `NEO4J_URI` change |
| 3 | Ship the **exact benchmarked graph** as a dump baked into the DB image (restore-on-first-boot), not a rebuild from the data lake | `semigraph build-graph` against the cloud DB | the Desktop store is Enterprise "block" format; converted with `neo4j-admin database copy --to-format=aligned --copy-schema`; counts match to the node (8,082 RiskFactor, 2,492 EvidenceSpan, 495 deleted lineages) |
| 4 | **Query embeddings via onnxruntime, 8-bit block-wise weight-only quantization** of the public fp32 ONNX export of Qwen3-Embedding-0.6B, built at image-build time by `scripts/build_onnx_embedder.py` | torch + safetensors (2 GB image, ~1.6 GB RSS); fastembed (does not support this model); community int8 / q4f16 / uint8 exports (rejected: cosine 0.87 / 0.94 / unusable output); hosted DeepInfra ($0.01 per M tokens, needs a new account) | cosine vs sentence-transformers on the 20 benchmark questions: min 0.998, mean 0.999; evidence top-8 overlap 0.96, top-1 identical 19/20; 1.3 GB RSS (1.9 GB peak at load) → 2 GB machine; ~1.3 s per query on 1 vCPU |
| 5 | **Claude Sonnet 5 via LiteLLM, unchanged** (the benchmark model); cost is capped, not downgraded | Haiku 4.5 for the demo | measured live: ~21k prompt tokens + ~1.4k output ≈ $0.056 per hybrid answer; ~15 s streamed |
| 6 | **Cost controls persisted in Neo4j**; the client address comes only from the header named by `CLIENT_IP_HEADER` (Fly sets `fly-client-ip` at its edge), never from arbitrary forwarding headers (`SvcQuery` ledger for the daily ceiling, `SvcPolicy` kill switch, `SvcAnswer` cache with the 20 benchmark answers pre-seeded); per-IP window and concurrency slots in-process | Postgres/Neon side store | one machine, one database; the ledger must survive auto-stop restarts; benchmark clicks cost $0 |
| 7 | **Turnstile optional, not fail-closed** (deviation from the voice agent) | fail closed like the reference | a fail-closed gate with no keys would make the public link useless; the daily ceiling bounds worst-case spend (150 × ~$0.06 ≈ $9/day); enabling Turnstile is two secrets |
| 8 | **Streaming answers** (SSE) with a request-scoped retry budget (2 attempts, 5/15 s backoff, 90 s timeout) instead of the pipeline's 15/60/180/300 s backoff | non-streaming with regenerate-on-truncation | a browser cannot wait out a 300 s backoff; truncated streamed answers are flagged and not cached |
| 9 | Serving image installs only `deploy/requirements-serve.txt` (compiled from a 12-line `.in`) plus the semigraph wheel `--no-deps`; pipeline extras (torch, pandas, edgartools, sec-parser) stay out | `uv sync` of the whole project | image stays small enough; `graph/__init__` resolves the pandas-based loaders lazily |

## Consequences

- Both apps stopped cost ≈ $0.75/month; running 24/7 ≈ $17/month (API 2 GB $11.1 + DB 1 GB $5.9 +
  volume $0.45) — the API stays warm while the demo is online; the STOP script parks both machines.
- The image build downloads 2.4 GB and quantizes in the Fly remote builder (10–15 min) whenever the
  model layer is invalidated; normal code deploys reuse that layer.
- Re-seeding the graph replaces the service ledger/cache (documented in the runbook).
- Open follow-ups: shrink the ONNX file by storing the 620 MB token-embedding table in fp16
  (→ ~750 MB, 1 GB machine); Turnstile keys; Sentry; a second judge family for the eval (V2 roadmap).
