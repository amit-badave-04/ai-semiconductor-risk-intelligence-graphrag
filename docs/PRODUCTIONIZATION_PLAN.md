# Productionization plan — semigraph as a live, cost-capped web product

Written 2026-09-09 at the start of the productionization session; each phase is ticked when it is
verified, not when it is written. The reference for "how we productionize" is the sibling project
[clinic-voice-agent](https://github.com/amit-badave-04/clinic-voice-agent): FastAPI on Fly.io,
secrets pushed from `.env`, a kill switch, a runbook with START/STOP blocks, CI with tests + secret
scan, an ADR that records the stack decision with evidence.

## Where the project stood (verified, not assumed)

| Fact | Evidence |
|---|---|
| SDK complete, 114 tests, LLM mocked | `uv run pytest -q` |
| Graph exists only in Neo4j Desktop (Enterprise 2026.05.0, `db.query.default_language=CYPHER_25`) | started headless, counts match PROJECT_STATUS: 8,082 RiskFactor, 2,492 EvidenceSpan, 495 Deleted lineages, 731 Metric, 13 ExportControl, 21 AFFECTED_BY |
| Query-time embedding needs torch (491 MB) + Qwen3-Embedding-0.6B fp32 (1.2 GB) | site-packages + HF cache sizes |
| One hybrid query: ~14k prompt tokens, ~$0.04 on Sonnet 5, 80 s wall-clock | live probe, 12 citations, 0 hallucinated |
| No Docker locally; flyctl 0.4.71 logged in as the owner; one existing Fly app (`clinic-voice-agent`) | `flyctl auth whoami`, `flyctl apps list` |

## Services — same as the reference where it fits, open-source elsewhere

| Concern | clinic-voice-agent | This project | Why |
|---|---|---|---|
| Compute | Fly.io Machines (sjc, always-warm) | Fly.io Machines (`sin`), API auto-stops when idle | same account/tooling; API can scale to zero because nothing is mid-call |
| Database | Neon Postgres (managed free tier) | **Neo4j Community 2026.x self-hosted on a second Fly machine + volume**; AuraDB Free documented as the $0 swap | Neo4j is the schema (vector indexes + `SEARCH` clause). Aura Free = $0 but needs an account/instance created by the owner and pauses after 72 h idle; self-host is deliverable now and STOP/START fits the ops pattern |
| LLM | GPT-4.1 (Retell-hosted) | Claude Sonnet 5 via LiteLLM (unchanged — benchmark-proven) | the 100 %/0.865 numbers are tied to this model; cost is capped instead of downgraded |
| Embeddings | — | Qwen3-Embedding-0.6B via **onnxruntime int8** in-process (torch-free) | open-source, no new vendor; fidelity verified against sentence-transformers before shipping |
| Bot/abuse control | Turnstile (fails closed) + per-IP + daily caps + kill switch | per-IP sliding window + daily query ceiling (persisted in Neo4j) + kill switch + optional Turnstile | every question costs real money |
| Secrets | `scripts/push_fly_secrets.py` | same script, adapted | values never enter argv/shell history |
| CI | pytest + gitleaks + pip-audit | same | |
| Runbook | `scripts/ops_runbook.md` + START/STOP blocks | `docs/RUNBOOK.md` + `scripts/ops.ps1` | requested explicitly |

## Phases

- [x] **P0 Spike (de-risk the two unknowns)** — ONNX embedder fidelity vs sentence-transformers on the
      20 benchmark questions (cosine + top-8 evidence overlap against the live graph) and RSS; Neo4j
      dump/load round-trip into Community edition with `CYPHER_25` default.
- [x] **P1 SDK changes (minimal, additive)** — pluggable `Embedder` backends (`local` | `onnx` |
      `openai_compatible`), lazy pandas import so the serving image needs no pandas/torch,
      request-scoped LLM retry budget + streaming for the answerer, usage/cost capture.
- [x] **P2 Service** — `semigraph.serve`: FastAPI app (`/`, `/api/ask` SSE, `/api/examples`,
      `/api/evidence/{chunk_id}`, `/api/stats`, `/healthz`), guard (rate limit, daily ceiling, kill
      switch, Turnstile), answer cache, static single-page UI with clickable citations.
- [x] **P3 Tests + CI** — API tests with mocked retrieval/LLM, guard tests, embedder-backend tests;
      GitHub Actions (pytest, gitleaks, pip-audit).
- [x] **P4 Deploy** — `deploy/neo4j` (Dockerfile, fly.toml, volume, memory tuning, dump restore),
      `Dockerfile` + `fly.toml` for the API (remote build, model baked into the image), secrets push,
      smoke test against the live URL.
- [x] **P5 Ops + docs** — `scripts/ops.ps1` (start | stop | status), `scripts/kill_switch.py`,
      `docs/RUNBOOK.md`, `adr/0001-production-stack.md`, README "Live demo" section, `.env.example`
      additions, memory notes.
- [x] **P6 Verification** — verifier agent review of the diff, live smoke, cost table from measured
      usage, final report with the Fly URL.

## Cost controls (design)

- Per-IP: 5 questions / 10 min. Global: `MAX_QUERIES_PER_DAY` (default 150 → worst case ≈ $6/day
  at the measured $0.04/query). Both counted in Neo4j so restarts do not reset them.
- Answer cache (question + strategy → answer, 24 h) and the 20 benchmark answers served from the
  committed eval run — free clicks for visitors.
- Question length cap (500 chars), single-flight per IP, 90 s LLM timeout, no blocking 300 s backoffs
  inside a web request.
- API machine auto-stops after idle; Neo4j machine stopped by the STOP script; both ≈ $0.75/mo when off.

## Outcome (2026-09-09)

- P0: 8-bit block-wise ONNX = cosine min 0.998 / mean 0.999 vs sentence-transformers on the 20
  benchmark questions; top-8 evidence overlap 0.96; 1.3 GB RSS → 2 GB machine. Dump converted to the
  Community "aligned" format with `--copy-schema`; counts identical after restore on Fly.
- P2: live hybrid answer measured end-to-end through the service: ~15 s streamed, 12 verified
  citations, 0 hallucinated, 21k/1.4k tokens ≈ $0.056; repeat served from the cache.
- P3: 148 tests (34 new) — gates, SSE framing, cache, admin, TextStream, answer_stream, spoofed
  forwarding headers, spend logged on mid-stream failure, slot accounting.
- P4: both apps live on Fly (`semigraph`, `semigraph-neo4j`, region sin); the first API deploy
  needed `flyctl ips allocate-v4 --shared` + `allocate-v6` by hand, and the remote builder needed
  the quantizer to save weights as external data (single-file save was OOM-killed).
- P6: Opus verifier review found 3 confirmed cost-control defects (spoofable client-IP header,
  ledger writes before any gate + unbounded stats scan, spend lost on mid-stream failure) plus a
  slot leak, a short health grace, and an unverifiable fidelity claim — all fixed and covered by
  tests before the final deploy.
