# semigraph — SDK reference

`semigraph` is the packaged form of the AI Semiconductor Risk Intelligence GraphRAG
pipeline: SEC EDGAR filings (10-K/10-Q/8-K + XBRL) and BIS/Federal Register
export-control rules → a **bitemporal Neo4j knowledge graph with full provenance** →
multi-hop, citation-grounded question answering.

Use it for: multi-hop supply-chain dependency tracing, export-control exposure screening,
bitemporal risk-evolution analysis (active vs dropped disclosures, as-of queries),
deterministic XBRL financial lookups, and audit-grade cited Q&A that declines when the
corpus lacks the facts.

This README documents the **package** (install, configuration, API, CLI, design rules).
For the project story — the problem it solves, DSML lifecycle, notebooks, benchmark
results, V2 roadmap — see the repository root `README.md`.

## Install

```bash
uv pip install .            # from the repo root, or install the wheel from dist/
uv pip install ".[eval]"    # + ragas (second-scorer stack; see note in Design rules)
uv pip install ".[serve]"   # + FastAPI/uvicorn/onnxruntime for the web service (semigraph.serve)
```

Python ≥ 3.12. A running Neo4j (Desktop 2026.x, local bolt) is required for
graph/retrieval/eval; ingestion and the pure logic work without it.

## Configuration

Settings load from `.env` in the working directory (pydantic-settings), all overridable
via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | LLM calls (LiteLLM routes by model prefix) |
| `LLM_MODEL` | `anthropic/claude-sonnet-5` | extraction, answering, faithfulness/correctness judging |
| `CRITIC_MODEL` | `anthropic/claude-haiku-4-5` | extraction critic, per-chunk relevance judge |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / — | graph connection |
| `SEC_USER_AGENT` | — | required declared identity for EDGAR requests |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | local embeddings, 1024-dim (schema-locked) |
| `EMBEDDING_BACKEND` | `local` | `local` (sentence-transformers), `onnx` (torch-free, `ONNX_MODEL_PATH`), `remote` (OpenAI-compatible endpoint, `EMBEDDING_API_*`) |
| `DATA_DIR` | `./data` | data-lake root (raw / interim / processed) |

## Quickstart (inference)

```python
from semigraph.config import get_settings
from semigraph.embeddings import Embedder
from semigraph.graph.client import get_driver
from semigraph.retrieval.answerer import answer

driver = get_driver(get_settings())
result = answer(
    "What dependencies connect Meta's AI infrastructure plans to Nvidia, TSMC, "
    "HBM suppliers, and export controls?",
    driver, Embedder(), strategy="hybrid",   # or "vector" for the baseline
)
print(result["answer"])          # cited answer ([chunk_id] after factual sentences)
print(result["citations"])       # cited chunk ids (post-verified against retrieval)
print(result["hallucinated"])    # cited ids NOT in the retrieved context (should be empty)
print(result["context"])         # the FULL context string the model saw (judge-ready)
```

## CLI

```
semigraph ingest       [-t TICKER]...   download EDGAR/XBRL/Federal-Register, parse, chunk (no LLM cost)
semigraph build-graph  [-t TICKER]... [--extract]
                                        apply schema + load the graph from the data lake;
                                        --extract runs PAID LLM extraction (prints a cost
                                        estimate and asks for confirmation first)
semigraph query "QUESTION" [--strategy hybrid|vector] [--show-context]
                                        one answer (one Sonnet call — cents)
semigraph eval         [--limit N] [--systems hybrid,vector]
                                        gold benchmark (PAID answer+judge calls; confirms first;
                                        checkpointed per question/system — interruptions never re-bill)
```

## Module map

| Module | Contents (ported from notebook) |
|---|---|
| `config` | `Settings` / `get_settings()` — env + data-lake paths |
| `llm` | `llm_json` — the hardened structured-call helper (see Design rules) |
| `embeddings` | `Embedder` — local-first load, passage vs query conventions, chunk-id-keyed cache (09/14) |
| `ingestion.edgar` | ticker→CIK, filing download + manifest (01, 12) |
| `ingestion.xbrl` | Company Facts → curated key-metric parquets; **no LLM ever parses numbers** (02, 12) |
| `ingestion.federal_register` | BIS export-control rule fetch + cache (12) |
| `parsing.segmentation` | sec-parser segmentation + Intel page-header / ASML no-KEEP fallbacks (03, 12) |
| `parsing.chunker` | table-aware, section-hierarchy chunking with offsets (04, 12) |
| `extraction.schemas` | Pydantic extraction contracts + category normalization (07) |
| `extraction.extractor` | extractor → verbatim-quote gate → Haiku critic; per-chunk checkpointing; cost estimator (07, 12) |
| `extraction.resolution` | canonical-dictionary + fuzzy entity resolution (08, 12) |
| `graph.client` / `graph.schema` | driver, `run_cypher`, schema DDL application (05) |
| `graph.loaders` | idempotent MERGE loaders: deterministic layer, evidence spans, knowledge, export controls (06, 09, 12) |
| `graph.temporal` | bitemporal lineage clustering + closure (pure core + thin appliers), as-of queries (13) |
| `retrieval.retriever` | entity-first `hybrid_retrieve` (graph + XBRL + bitemporal + scoped vector) and `vector_retrieve` baseline — **SEARCH clause everywhere** (10, 14) |
| `retrieval.answerer` | `build_blocks` + `answer` with citation post-verification and full-context return (11, 14); `answer_stream` / `TextStream` for streamed, usage-accounted answers |
| `serve` | FastAPI web service (`uvicorn semigraph.serve.main:app`): SSE answers, evidence lookup, Neo4j-persisted rate/daily/kill-switch controls — needs the `serve` extra |
| `eval.runner` | benchmark runner: programmatic checks + LLM judges, checkpointed, artifact writers (14) |
| `artifacts` | packaged `schema.cypher`, `canonical_entities.json`, `benchmark.json`, `prompts/` + loaders (`importlib.resources`) |

## Design rules (battle scars — each cost a failed run; do not relax)

1. **LLM calls** (`llm.llm_json`): never pass `temperature/top_p/top_k` (400 on Sonnet 5);
   `thinking={"type":"disabled"}` on structured Sonnet calls (default-on thinking eats capped
   budgets → `content=None`); Haiku calls omit it (`thinking_off=False`). On
   `finish_reason=="length"` REGENERATE fresh with a doubled budget — never ask the model to
   "fix" truncated JSON. Retry ONLY transient errors (`APIConnectionError / ServiceUnavailable /
   InternalServerError / RateLimit / Timeout`) with 15/60/180/300s backoff + `num_retries=2`.
   Checkpoint every paid loop per item.
2. **Neo4j 2026.x vector search**: the `SEARCH` clause only —
   `MATCH (n:Label) SEARCH n IN (VECTOR INDEX name FOR $vec LIMIT k) SCORE AS score` —
   MATCH binds ONE variable; filters compose AFTER. (`db.index.vector.queryNodes` is deprecated.)
3. **SEC parsing**: Intel 10-K has no item headings (page-header fallback); ASML 20-F fallback
   triggers on "no KEEP sections", not "no rows"; ASML 2023/24 warned+skipped by design;
   20-F items 3/4/5; Samsung doesn't file (entity-only node).
4. **Eval**: the faithfulness judge must see the FULL context the answerer saw (graph blocks +
   excerpts); retrieval must expose the bitemporal layer or temporal questions fail (100% → 0%).
5. **Embeddings**: Qwen3-Embedding-0.6B, 1024-dim (schema-locked); `local_files_only=True`
   first; passages plain / queries via `prompt_name="query"`; caches keyed by the chunk-id set.
6. **Numbers**: financial figures come from XBRL only — never from LLM parsing of prose.

Note on `[eval]`: the pinned `ragas==0.4.3` currently fails to import against resolved
`langchain-community` versions (`ChatVertexAI` moved); integrating it is a V2 item.

## Testing

```bash
uv run pytest -q     # 114 tests; litellm fully mocked — zero API spend, no Neo4j needed
```

Pure logic is tested directly (chunker, segmentation fallbacks, entity resolution,
extraction gates + checkpoint resume, bitemporal closure, `llm_json` failure handling,
retrieval context assembly, eval scoring math).
