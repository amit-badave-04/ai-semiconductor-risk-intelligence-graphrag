# AI Semiconductor Risk Intelligence — GraphRAG

A GraphRAG system that ingests SEC EDGAR filings (10-K / 10-Q / 8-K + XBRL) and BIS/Federal Register
export-control announcements for 14 keystone companies (Nvidia, AMD, Intel, Broadcom, Qualcomm, TSMC,
ASML, Micron, Samsung, Apple, Microsoft, Amazon, Alphabet, Meta), builds a **bitemporal Neo4j knowledge
graph with full provenance** (every edge backed by an `EvidenceSpan` of source text), and answers
multi-hop supply-chain / export-control questions with citations.

Feasibility research: see [docs/](docs/) (two studies; verdict: BUILD, 8.4/10).

This README covers the **project**: lifecycle, milestones, results, environment, repo map, roadmap.
The **package documentation** (install, configuration, API, CLI, module map, design rules) lives in
[src/semigraph/README.md](src/semigraph/README.md) and ships inside the wheel.

## The problem this solves

Questions about AI-semiconductor supply-chain and export-control exposure are **relationship and
time questions**, not text-similarity questions: *who depends on whom, since when, under which BIS
rule, and did that risk survive into the latest annual report?* Plain vector RAG over filing text
fails at exactly this — in our benchmark it scored **0% on temporal questions** (it happily presents
a withdrawn 2023 risk as current) and misses multi-hop chains that are never stated in one passage.
LLM-parsed financial figures are similarly untrustworthy.

`semigraph` answers these questions from a knowledge graph in which **every edge is backed by a
verbatim SEC evidence span**, risk disclosures carry bitemporal state (active / deleted, with
first-seen and end dates), and all financial numbers come from XBRL — never from LLM parsing of
prose. Answers cite chunk ids that are post-verified against the retrieved context (0 hallucinated
citations across the benchmark), and the system declines when the corpus lacks the facts.

## What the SDK can be used for

- **Multi-hop dependency tracing** — e.g. Query D: Meta's AI plans → accelerator vendors → TSMC →
  HBM suppliers → the export-control regime, every hop cited to a filing excerpt.
- **Export-control exposure screening** — which companies are AFFECTED_BY which BIS/Federal Register
  rules, with disclosure evidence.
- **Risk-evolution / time-travel analysis** — risks newly introduced or dropped between annual
  reports; "risk factors active as of 2024-06-30" (the bitemporal layer).
- **Deterministic financial lookups** — revenue/capex/R&D per fiscal period from XBRL facts.
- **Audit-grade cited Q&A** — every factual sentence carries a `[chunk_id]` resolvable to the exact
  SEC source text and URL.
- **Rebuilding or extending the corpus** — the CLI re-runs the whole pipeline (ingest → build-graph
  → eval) for a different filer universe or newer filings, with paid stages cost-estimated and
  confirmation-gated.
- **Retrieval research** — the 20-question gold benchmark + judge harness compares retrieval
  strategies reproducibly (that is how the hybrid-vs-vector numbers below were produced).

## Approach

**Notebook-first DSML lifecycle.** Every stage was proven in a numbered Jupyter notebook; after
notebooks 01–14 ran clean end-to-end (M0–M6), the logic was refactored into the `semigraph` SDK
(`src/semigraph/`, M7). Notebooks 00–14 are frozen as the historical/experimental record;
[15_sdk_inference_driver.ipynb](notebooks/15_sdk_inference_driver.ipynb) is the thin inference
driver that reproduces the flagship Query D using only `import semigraph`.

## Results (M6 benchmark, 20 gold questions — `artifacts/eval_report.json`)

| metric | hybrid GraphRAG | vector-only baseline |
|---|---|---|
| correctness | **100%** | 80% |
| faithfulness | **0.865** (target ≥0.85) | 0.943 (inflated by hedging/refusals) |
| citation validity | **100%** | 100% |
| temporal questions | **100%** | **0%** — the bitemporal layer is the entire difference |
| numeric questions | **100%** | 80% (XBRL metrics vs prose retrieval) |

## Stack

| Concern | Choice |
|---|---|
| Python | 3.13 via [uv](https://docs.astral.sh/uv/) |
| LLM | Provider-agnostic via LiteLLM; **Claude Sonnet** extracts/answers/judges, **Haiku** critiques |
| Graph DB | **Neo4j Desktop** (local), native vector indexes, `SEARCH` clause |
| Embeddings | Local `Qwen/Qwen3-Embedding-0.6B` (sentence-transformers, 1024-dim, 32k ctx) |
| SEC ingestion | `edgartools`, `sec-parser`, XBRL Company Facts API (numbers are XBRL-only, never LLM-parsed) |
| Evaluation | Transparent LLM-judge (standard RAG metrics) + programmatic citation/numeric/temporal checks |
| Packaging | `semigraph` wheel (`uv build`), typer CLI, 142 mocked-LLM pytest tests |
| Serving | FastAPI + SSE on Fly.io, Neo4j Community on Fly, ONNX query embedder — see *Production* below |

## Phases & Milestones — ALL COMPLETE

**Status: M0–M7 complete.** Full detail and the battle-scars list: [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).

| Phase | Milestone | Notebooks | Deliverable |
|---|---|---|---|
| 0. Environment & scaffold | M0 ✅ | `00_smoke_test` | env + Neo4j + LLM + embeddings verified |
| 1. Data acquisition (Nvidia) | M1 ✅ | `01_edgar_acquisition`, `02_xbrl_facts` | raw filings + XBRL facts on disk |
| 2. Parsing & chunking | M2 ✅ | `03_semantic_parsing`, `04_chunking` | section-aware chunk store (parquet) |
| 3. Knowledge graph | M3 ✅ | `05_graph_schema` … `09_embeddings_and_evidence` | Nvidia graph w/ provenance |
| 4. Retrieval & answering | M4 ✅ | `10_retrieval_strategies`, `11_answer_generation` | cited Q&A over Nvidia |
| 5. Scale + temporal | M5 ✅ | `12_full_universe_ingestion`, `13_temporal_versioning` | 14-company bitemporal graph, Query D |
| 6. Evaluation | M6 ✅ | `14_evaluation` | benchmark above |
| 7. SDK packaging | M7 ✅ | `15_sdk_inference_driver` | `semigraph` wheel + CLI + tests |

## Live demo

**https://semigraph.fly.dev/** — the graph above behind a small web product: click any of the 20
benchmark questions (served from the committed evaluation run, free) or ask your own (one streamed
Claude Sonnet 5 call, rate-limited per address and capped per day). Every citation chip opens the
verbatim SEC excerpt it points to. The owner takes the demo offline with one script when not in
use — if the page does not load, it is parked.

## Production (how it is deployed)

Mirrors the operating pattern of [clinic-voice-agent](https://github.com/amit-badave-04/clinic-voice-agent):
FastAPI on Fly.io, secrets pushed from a local env file, a kill switch, a runbook with START/STOP
blocks, CI with tests + secret scan, and an ADR with the evidence behind each choice.

| Piece | Where |
|---|---|
| Web service (`/`, `POST /api/ask` SSE, `/api/evidence/{id}`, `/api/stats`, `/healthz`, admin kill switch) | `src/semigraph/serve/`, `Dockerfile`, `fly.toml` |
| Neo4j Community 2026.07 + volume, private network, graph seeded from a dump baked into the image | `deploy/neo4j/` |
| Torch-free query embedder: 8-bit weight-only ONNX of Qwen3-Embedding-0.6B (cosine 0.999 vs sentence-transformers, verified locally: `artifacts/onnx_embedder_fidelity.json`) | `scripts/build_onnx_embedder.py`, `src/semigraph/embeddings_onnx.py` |
| Cost controls: per-address window, daily ceiling + kill switch + answer cache persisted in Neo4j, benchmark answers pre-seeded | `src/semigraph/serve/guard.py`, `store.py` |
| Operations: START / STOP / status, secrets push, kill switch | `scripts/ops.ps1`, `scripts/push_fly_secrets.py`, `scripts/kill_switch.py`, [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Decisions + measurements | [adr/0001-production-stack.md](adr/0001-production-stack.md), [docs/PRODUCTIONIZATION_PLAN.md](docs/PRODUCTIONIZATION_PLAN.md) |

Running both machines 24/7 is ≈ $17/month; parked ≈ $0.75/month; each live answer ≈ $0.06.

## Setup

```bash
uv sync                          # creates .venv with all dependencies (editable semigraph)
copy .env.example .env           # then fill in keys (Anthropic, Neo4j, SEC user-agent)
uv run pytest -q                 # 142 tests, zero API spend
uv run semigraph --help          # CLI: ingest | build-graph | query | eval
uv run jupyter lab               # open notebooks/
```

**Neo4j Desktop (one-time, manual):**
1. Download from https://neo4j.com/download/ and install.
2. Create a project → Add → Local DBMS (2026.x), set a password.
3. Start the DBMS; the bolt URI is `bolt://localhost:7687`.
4. Put the password into `.env` (`NEO4J_PASSWORD`).

Rebuilding the graph from a fresh clone: `semigraph ingest` (network only), then
`semigraph build-graph --extract` (paid LLM extraction — the CLI prints a cost estimate and asks
before spending).

## Layout

```
notebooks/        00-14: frozen DSML record · 15: SDK inference driver
data/raw          immutable downloads (git-ignored, rebuildable)
data/interim      parsed section extracts (git-ignored)
data/processed    chunk stores, extractions, embeddings, eval runs (git-ignored)
artifacts/        schema.cypher, canonical_entities.json, prompts, benchmark, eval reports
src/semigraph/    the SDK — see src/semigraph/README.md
tests/            pure-logic pytest suite (LLM mocked)
docs/             feasibility studies + PROJECT_STATUS.md (status, battle scars, V2 roadmap)
```

## V2 roadmap (accepted TODO; retrieval changes require a paid re-benchmark before merge)

1. **BM25/full-text keyword channel** — Neo4j full-text index on evidence text, merged with the
   vector channel via reciprocal rank fusion (RRF).
2. **Reranker + adaptive k** — cross-encoder (or Haiku) rerank over merged candidates; targets the
   known context-precision gap (~0.3).
3. **Eval judge diversity** — Ragas (after a version bump; the pinned 0.4.3 has a broken
   `langchain_community` import) + DeepEval CI regression, using a *different judge model family*;
   Ragas testset generation to grow the benchmark 20 → 50. Custom graph-path metrics stay custom.
4. **Framework-shaped interfaces, custom internals** — mirror official retriever interfaces where
   useful; do not adopt a framework wholesale (none supports the bitemporal layer, the
   quote-gate + critic extraction, or XBRL-only numerics that produced the results above).
