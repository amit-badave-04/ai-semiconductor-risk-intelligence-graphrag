# Project Status — productionized (web service live, 2026-09-09)

**Production**: `semigraph.serve` (FastAPI, SSE streaming, Neo4j-persisted cost controls) runs at
https://semigraph.fly.dev with Neo4j Community on a second Fly machine; torch-free ONNX query
embeddings; START/STOP via `scripts/ops.ps1`. See [RUNBOOK.md](RUNBOOK.md),
[PRODUCTIONIZATION_PLAN.md](PRODUCTIONIZATION_PLAN.md) and
[adr/0001-production-stack.md](../adr/0001-production-stack.md). 142 tests.

---

## SDK milestones — ALL COMPLETE (M7 shipped 2026-07-03)

**M7 is DONE**: notebook logic is refactored into the `semigraph` package under `src/semigraph/`
(config / llm / embeddings / ingestion / parsing / extraction / graph / retrieval / eval), with a
typer CLI (`semigraph ingest|build-graph|query|eval`), a 114-test pytest suite (LLM fully mocked —
zero API spend), packaged artifacts (schema.cypher, canonical_entities.json, benchmark.json,
prompts/), and `uv build` producing an installable wheel (verified in a fresh venv). All vector
queries use the Neo4j SEARCH clause. Every battle scar below is preserved in code and pinned by a
test where the logic is pure. `notebooks/15_sdk_inference_driver.ipynb` reproduces Query D via
`import semigraph` only (live-verified: 11 citations, 0 hallucinated). Notebooks 00–14 are
untouched as the historical record.

## V2 roadmap (approved as TODO — each retrieval change requires a paid ~$2-3 re-benchmark before merge)

1. **BM25/full-text keyword channel** — Neo4j full-text (Lucene) index on `EvidenceSpan.text`,
   merged with the vector channel via **RRF** (reciprocal rank fusion, not weighted score mixing —
   vector/BM25/graph scores are not on comparable scales).
2. **Reranker + adaptive k** — local cross-encoder (or Haiku-as-reranker) over merged candidates;
   fixes context precision (~0.3, k always 8 even when graph blocks answer).
3. **Eval judge diversity** — Ragas (needs version bump: pinned 0.4.3 fails to import,
   `langchain_community.ChatVertexAI` drift) + DeepEval for CI regression, pointed at a DIFFERENT
   judge model family (frameworks are LLM-judges too; same family = same self-preference).
   Ragas testset generation to grow the benchmark 20 → 50 questions. Keep the custom
   entity/edge/path expectations — no framework provides graph-path metrics.
4. **Framework interfaces, custom internals** — do NOT adopt neo4j-graphrag-python/LangChain
   wholesale (no framework supports the bitemporal layer = the 100%-vs-0% temporal edge, the
   quote-gate+critic, or XBRL-only numbers); optionally mirror official retriever interfaces.

---

*The original M7 handoff below is retained for context.*

**Milestones M0–M6 are COMPLETE and verified. M7 (the `semigraph` SDK) is the only one left.**
This file is the handoff for the SDK-building session. Companion references: notebook text exports in
`C:\temp\graphrag-notebooks-txt\` (regenerate with the `notebook-to-text` skill), the approved plan at
`~/.claude/plans/please-read-both-the-clever-mango.md`, and this repo's git log (one commit per fix, with
reasoning in the messages).

## Headline result (M6 benchmark, artifacts/eval_report.json)

| metric | hybrid GraphRAG | vector-only baseline |
|---|---|---|
| correctness (20 questions) | **100%** | 80% |
| faithfulness | **0.865** (meets 0.85 target) | 0.943 (inflated by hedging/refusing hard questions) |
| citation validity | **100%** | 100% |
| **temporal questions** | **100%** | **0%** ← the bitemporal layer is the entire difference |
| numeric questions | **100%** | 80% (XBRL metrics vs prose retrieval) |

## What exists

- **Graph (Neo4j Desktop 2026.05, bolt://localhost:7687)**: 13 filers + ecosystem companies, 59 filings,
  ~2,500 EvidenceSpans (1024-dim Qwen3 embeddings), ~8,100 RiskFactors in temporal lineages
  (495 Deleted with end_dates), 731 XBRL metrics, 13 ExportControl rules, 21 AFFECTED_BY edges.
- **Notebooks 00–14**: every stage proven end-to-end; all run clean. Each ends with an assertion cell.
- **Artifacts**: `schema.cypher`, `canonical_entities.json` (26 entities), `prompts/` (extractor, critic),
  `benchmark.json` (20 questions), `eval_report.json`, `eval_scores.json`.
- **Data lake** (`data/`, git-ignored, rebuildable): raw EDGAR HTML + XBRL JSON, section parquets,
  chunk parquets per filer, extraction jsonl per filer (~2,300 chunks extracted), embedding caches.

## M7 scope (from the approved plan)

Refactor notebook logic into `src/semigraph/` — notebooks become thin demo drivers:
- `config.py` (pydantic-settings from `.env`), `llm.py` (the hardened `llm_json` — see battle scars below),
  `embeddings.py` (cached loader + query-prompt handling)
- `ingestion/` (edgar, xbrl, federal_register) · `parsing/` (segmentation incl. page-header fallback, chunker)
- `extraction/` (Pydantic schemas, extractor+critic, entity resolution) · `graph/` (schema DDL, loaders,
  temporal closure) · `retrieval/` (hybrid retriever with temporal block, answerer) · `eval/` (benchmark runner)
- `typer` CLI: `semigraph ingest|build-graph|query|eval`; `pytest` for pure logic; `uv build` wheel.
- Exit: fresh env → `uv pip install .` → demo notebook reproduces Query D with only `import semigraph`.

## Battle scars the SDK must preserve (each cost a failed run; do not regress)

1. **LLM calls (Sonnet 5 via LiteLLM)**: never pass `temperature/top_p/top_k` (400); pass
   `thinking={"type":"disabled"}` on structured calls (default-on thinking eats capped budgets →
   `content=None`); on `finish_reason=="length"` REGENERATE with doubled budget (never "fix" truncated
   JSON); retry ONLY transient errors (`litellm.APIConnectionError/ServiceUnavailableError/
   InternalServerError/RateLimitError/Timeout`) with backoff 15/60/180/300s + `num_retries=2`;
   checkpoint every paid loop per item. Critic/validators run on Haiku 4.5 (`use_thinking_off=False`).
2. **Neo4j 2026.x vector search**: use the `SEARCH` clause (`db.index.vector.queryNodes` is deprecated):
   `MATCH (n:Label) SEARCH n IN (VECTOR INDEX name FOR $vec LIMIT k) SCORE AS score` — MATCH must bind
   ONE variable; filters compose AFTER the search. Notebooks 09–11 still use the deprecated call (works,
   warns) — SDK should use SEARCH uniformly.
3. **SEC parsing**: Intel 10-K has NO item headings (page-header fallback: "Risk Factors44");
   ASML 20-F cover has junk "Item 17 ☐ 18 ☐" (fallback triggers on "no KEEP sections", not "no rows");
   ASML 2023/24 unmarkable (warned+skipped by design); 20-F items 3/4/5; TSMC/ASML file 20-F,
   Samsung doesn't file (entity-only node).
4. **Eval**: faithfulness judge must see the FULL context the answerer saw (graph blocks + excerpts);
   retrieval must expose the bitemporal layer or temporal questions fail.
5. **Embeddings**: Qwen3-Embedding-0.6B, 1024-dim (schema-locked), `local_files_only=True` first,
   passages plain / queries via `prompt_name="query"`, embedding caches keyed by chunk_id set.

## Known issues / backlog (found by M6, for the SDK or later)

- **Risk category enum not enforced** — extractor produced ~2,000 free-text categories (nb13 normalizes
  case, but consolidation to the 10-value enum needs strict schema or post-mapping in the SDK).
- **372 unresolved entities** in `data/processed/extractions/resolution_report_universe.parquet` —
  canonical-dictionary growth candidates; AAPL/AMZN/GOOGL/TSM/ASML currently have 0 canonical relations.
- **Context precision ~0.3** — always retrieves k=8 chunks even when graph blocks answer; reranker or
  adaptive-k would lift it.
- **AFFECTED_BY linking is keyword-heuristic** (documented in nb12 stage 7) — refine when needed.
- **Judge self-preference** — Sonnet judges Sonnet; M7 idea: RAGAS/DeepEval as second scorer +
  different judge family; RAGAS testset generator to grow 20 → 50 questions.
- `ragas` is in pyproject but unused — decide in M7: integrate as second scorer or drop the dep.

## Environment facts

- Windows 11, uv-managed Python 3.13, kernel `ai-semiconductor-risk-intelligence-graphrag` registered
  for VS Code. `.env` holds ANTHROPIC_API_KEY / NEO4J_* / SEC_USER_AGENT / EMBEDDING_MODEL.
- User runs notebooks themselves in VS Code. **Editor-buffer collision hazard**: never edit a notebook
  the user has open (a stale buffer once clobbered a fix mid-run) — have them close it first.
- API budget: ~$34 loaded lifetime; extraction+eval spent most of it — check console.anthropic.com
  before planning paid runs. SDK work itself is ~free (tests can mock LLM calls; probes are cents).
