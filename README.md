# AI Semiconductor Risk Intelligence — GraphRAG

A GraphRAG system that ingests SEC EDGAR filings (10-K / 10-Q / 8-K + XBRL) and BIS/Federal Register
export-control announcements for 14 keystone companies (Nvidia, AMD, Intel, Broadcom, Qualcomm, TSMC,
ASML, Micron, Samsung, Apple, Microsoft, Amazon, Alphabet, Meta), builds a **bitemporal Neo4j knowledge
graph with full provenance** (every edge backed by an `EvidenceSpan` of source text), and answers
multi-hop supply-chain / export-control questions with citations.

Feasibility research: see [docs/](docs/) (two studies; verdict: BUILD, 8.4/10).

## Approach

**Notebook-first DSML lifecycle.** Every stage is proven in a numbered Jupyter notebook; once notebooks
01–14 run clean end-to-end, the logic is refactored into the `semigraph` Python SDK (`src/semigraph/`).

## Stack

| Concern | Choice |
|---|---|
| Python | 3.12 via [uv](https://docs.astral.sh/uv/) |
| LLM | Provider-agnostic via LiteLLM; default **Claude Sonnet** |
| Graph DB | **Neo4j Desktop** (local), native vector indexes |
| Embeddings | Local `Qwen/Qwen3-Embedding-0.6B` (sentence-transformers, 1024-dim, 32k ctx) |
| SEC ingestion | `edgartools`, `sec-parser`, XBRL Company Facts API |
| Evaluation | Transparent LLM-judge (standard RAG metrics) + programmatic citation/numeric/temporal checks |

## Phases & Milestones

**Status: M0–M6 complete (benchmark: hybrid 100% correct / 0.865 faithful; temporal questions 100% vs 0% for vector-only). M7 (SDK) in progress — see [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).**

| Phase | Milestone | Notebooks | Deliverable |
|---|---|---|---|
| 0. Environment & scaffold | M0 | `00_smoke_test` | env + Neo4j + LLM + embeddings verified |
| 1. Data acquisition (Nvidia) | M1 | `01_edgar_acquisition`, `02_xbrl_facts` | raw filings + XBRL facts on disk |
| 2. Parsing & chunking | M2 | `03_semantic_parsing`, `04_chunking` | section-aware chunk store (parquet) |
| 3. Knowledge graph | M3 | `05_graph_schema` … `09_embeddings_and_evidence` | Nvidia graph w/ provenance |
| 4. Retrieval & answering | M4 | `10_retrieval_strategies`, `11_answer_generation` | cited Q&A over Nvidia |
| 5. Scale + temporal | M5 | `12_full_universe_ingestion`, `13_temporal_versioning` | 14-company bitemporal graph |
| 6. Evaluation | M6 | `14_evaluation` | ≥85% faithfulness benchmark |
| 7. SDK packaging | M7 | — | `semigraph` wheel + CLI + tests |

## Setup

```bash
uv sync                          # creates .venv with all dependencies
copy .env.example .env           # then fill in keys (Anthropic, Neo4j, SEC user-agent)
uv run jupyter lab               # open notebooks/
```

**Neo4j Desktop (one-time, manual):**
1. Download from https://neo4j.com/download/ and install.
2. Create a project → Add → Local DBMS (latest 5.x), set a password.
3. Start the DBMS; the bolt URI is `bolt://localhost:7687`.
4. Put the password into `.env` (`NEO4J_PASSWORD`).

## Layout

```
notebooks/        numbered notebooks, one DSML concern each
data/raw          immutable downloads (git-ignored)
data/interim      parsed section extracts (git-ignored)
data/processed    chunk stores, extraction outputs (git-ignored)
artifacts/        schema.cypher, canonical_entities.json, prompts, benchmark
src/semigraph/    the SDK (Phase 7)
docs/             feasibility studies
```
