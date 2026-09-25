# Chunk freshness, staleness and versioning for semigraph (research, 2026-09-25)

The corpus is already stale, and the AMD 10-K/A is a real case where the graph serves a corrected-away figure. Nothing in the repo was changed. One point of disclosure: my first LangChain probe resolved against the repo `.venv` (langchain-core 1.4.8), read-only; every later probe ran in an isolated uv environment. The SEC probes were about 45 requests at 2.5 requests/second or less. Labels are VERIFIED (seen live) or UNVERIFIED.

## Findings that shape the design

- **The corpus is behind EDGAR (VERIFIED, submissions API vs `manifest_universe.json`).**
  - 10 of 13 filers have a newer periodic filing on EDGAR than our corpus. Examples are the NVDA 10-Q filed 2026-08-26, the AVGO 10-Q filed 2026-09-10, and a new MSFT 10-K filed 2026-07-29.
  - These become a real "snapshot B" for tests.
- **The AMD 10-K/A case (VERIFIED, local chunk parquet plus EDGAR).**
  - Original 10-K `0000002488-26-000018` was accepted 2026-02-03T23:14:52Z. The 10-K/A `0000002488-26-000021` was accepted 2026-02-04T20:06:57Z. Both carry `filingDate` 2026-02-04, so ordering must use `acceptanceDateTime`.
  - The 10-K/A corrects transposed Client-revenue MD&A figures.
  - Our chunk says "31% increase in unit shipments … 15% increase in average selling price". The amendment says 15% units and 31% ASP.
  - The manifest lists the 10-K/A, but it was never chunked, and `_span_scope` filters on `form == '10-K'` exactly. So the graph serves the wrong figures with no staleness signal.
- **XBRL restatements are dropped.** `curate_metrics` keeps the earliest `filed` per period end, so a restated value never lands.
- **Benchmark answers never expire.** `store.get_answer` exempts `source='benchmark'` from the TTL.
- **Real amendments are rare in our universe (VERIFIED).**
  - Across the last 1,000 filings per filer there is one 10-K/A (AMD), two old ASML 20-F/As, and zero 8-K Item 4.02 or NT filings.
  - Restatement, withdrawal and late-filing tests therefore need synthetic fixtures.

## 1. Existing approaches

| Tool | Live status | Fit |
|---|---|---|
| LangChain `index()` | `index`, `aindex`, `RecordManager` and `DocumentIndex` are in langchain-core 1.6.5 (PyPI, 2026-09-24). Cleanup modes are `incremental`, `full` and `scoped_full`. `SQLRecordManager` moved to `langchain_classic.indexes` and needs `sqlalchemy[asyncio]`. langchain-community is sunset. | Not usable. The destination must be a `VectorStore` or `DocumentIndex`. `Neo4jVector.delete is VectorStore.delete` is True, so the guard in `index()` rejects it. It hard-deletes and assigns its own content-plus-metadata hash ids. It has no supersession and no custom schema. Borrow the record-manager idea (content_hash, source_id, updated_at). |
| LlamaIndex 0.14.25 | `IngestionPipeline` `DocstoreStrategy` has `UPSERTS`, `DUPLICATES_ONLY` and `UPSERTS_AND_DELETE` (source read, [pipeline.py](https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/ingestion/pipeline.py)). | Hash upsert keyed on `ref_doc_id` needs a docstore plus a vector store and hard-deletes. Borrow the pattern only. |
| Haystack 3.2.0 | `DuplicatePolicy` is NONE, SKIP, OVERWRITE or FAIL. Document id is a content hash (tested: it changes with content or meta). | An edited document gets a new id and orphans the old one. No versioning. Not a fit. |
| Graphiti 0.30.2 (2026-09-08) | Apache-2.0, 31k stars, pushed 2026-09-24, [repo](https://github.com/getzep/graphiti). Documents Neo4j 5.26; compatibility with 2026.07 is UNVERIFIED. Edges carry `valid_at`, `invalid_at`, `expired_at` and `created_at`. Search uses `vector.similarity.cosine` in Cypher, not a vector index. | Do not adopt. Source call sites show about 4 fixed LLM calls per episode plus 2 to 3 per edge (`resolve_edge`, `extract_timestamps`, attributes). That is an estimate from code, not measured $/episode. We pay 1 call per chunk today. [Docs](https://help.getzep.com/graphiti/core-concepts/adding-episodes) say `add_episode_bulk` skips invalidation. `add_triplet` still calls the LLM. Its schema is generic Entity/RELATES_TO, not our typed evidence-id edges. SEC supersession is deterministic at accession level and needs no LLM contradiction check. Borrow the four timestamps. |
| LightRAG 1.5.7 (MIT, 2026-09-02) | [README](https://raw.githubusercontent.com/HKUDS/LightRAG/main/README.md): updating a document is delete then insert, with KG rebuild from the LLM cache. Neo4j is supported as graph storage; Postgres is the recommended production backend. `adelete_by_doc_id` did not appear in the README, so that name is UNVERIFIED. | No validity intervals and its own schema. Borrow the LLM-cache rebuild idea. |
| Microsoft GraphRAG 3.2.0 (2026-09-24) | `graphrag update` appends to `update_output` parquet ([CLI](https://microsoft.github.io/graphrag/cli/)). Docs are silent on modified and deleted documents. Open bug [#2540](https://github.com/microsoft/graphrag/issues/2540), dangling ids after an update. | Not a fit. Parquet-based, not Neo4j-native. |

## 2. Literature, 2024 to 2026 (abstracts VERIFIED)

- [HoH (arXiv 2503.04800)](https://arxiv.org/abs/2503.04800): outdated documents both distract retrieval and mislead generation. A search snippet reported about 50% outdated results in the top 5; I did not check that number in the paper.
- [Grofsky (arXiv 2509.19376)](https://arxiv.org/abs/2509.19376): a half-life recency prior lifts Latest@10 from 0.20 to 0.60 on a hard CVE set. The author calls it partial and parameter-sensitive.
- [Fang et al. (arXiv 2509.11353)](https://arxiv.org/abs/2509.11353): LLM rerankers favour recent content, shifting the top-10 mean year by up to 4.78 years. Keep recency out of the LLM.
- [VersionRAG (arXiv 2510.08109)](https://arxiv.org/abs/2510.08109): a version graph plus intent routing scores 90% vs 58% naive on VersionQA, with 97% fewer indexing tokens than GraphRAG.
- [TimelyRAG (arXiv 2609.11572)](https://arxiv.org/abs/2609.11572): temporal-distance ranking for overlapping amendments, up to +28.6% nDCG@10.
- [TEMPO (arXiv 2601.09523)](https://arxiv.org/abs/2601.09523): the best system reaches 32.0 NDCG@10.
- [Zep (arXiv 2501.13956)](https://arxiv.org/abs/2501.13956): the bitemporal edge model behind Graphiti.
- [dbt freshness](https://docs.getdbt.com/reference/resource-properties/freshness): the `warn_after` / `error_after` pattern for freshness SLOs.

The takeaways are:
- filter to status current by default;
- use a recency prior only for "latest" intents and only as a small ranking feature;
- handle as-of and history queries by validity interval;
- route by an intent classifier.

## 3. SEC and Federal Register semantics and detection

**Detection (VERIFIED by probes today):**
- The submissions API returns no ETag or Last-Modified. A conditional GET returned a full 200 (159,785 bytes for NVDA), so ETag/If-Modified-Since is not usable there.
- Polling the newest accession for 13 filers is about 13 requests and 2 MB per poll. That is well under the 10 requests/second [fair-access limit](https://www.sec.gov/about/developer-resources).
- A cheaper path is `usgaap.rss.xml` (ETag and Last-Modified, updated within 10 minutes). It covers US-GAAP and IFRS filings.
- The daily-index `form.YYYYMMDD.idx` returns 304 on a conditional GET. It is built about 02:00 UTC the next day, so it works as nightly catch-up only.
- The getcurrent Atom feed returned 503 once, then 200. Use retry with backoff.
- The Federal Register `documents.json` returns `no-store`. Poll on a `publication_date` watermark minus 3 days per agency.
- Federal Register corrections are separate documents (`C1-2026-16628` has `correction_of` pointing to `2026-16628`). `effective_on` can precede `publication_date` (2026-19537 was published 09-24, effective 09-22).
- I did not read the Federal Register API rate-limit policy.

**Modelling rules:**
- A 10-K/A supersedes an older version only for the sections it contains. A Part III-only amendment (due within 120 days of year end) leaves Item 1A and MD&A alone. [Source](https://www.sec.gov/Archives/edgar/data/1812727/000164117225003280/form10-ka.htm).
- Order versions by `acceptanceDateTime`.
- An 8-K Item 4.02 is a non-reliance flag. "Little r" restatements arrive silently in the next periodic filing's XBRL. Model one metric observation per `(accn, filed)`.
- Filing deadlines per [SEC](https://www.sec.gov/rules-regulations/2005/12/revisions-accelerated-filer-definition-accelerated-deadlines-filing-periodic-reports): 10-K in 60, 75 or 90 days depending on filer size; 10-Q in 40 or 45 days. Form 12b-25 adds 15 days for a 10-K and 5 for a 10-Q. Use these for "expected by" staleness alarms.
- A newer 10-Q supersedes the prior 10-Q as the latest quarter, but the prior one stays valid for as-of queries.

**Scheduler:**
- GitHub Actions cron has a 5-minute minimum, drops jobs at the top of the hour, and disables schedules after 60 days of inactivity in public repos ([docs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).
- A Fly scheduled machine offers only hourly, daily, weekly or monthly, on a fuzzy cycle, and cannot be triggered by hand ([docs](https://docs.fly.io/machines/flyctl/fly-machine-run/)).
- **Recommendation:**
  - Run an in-app scheduler on the always-warm API machine, guarded by a Neo4j lease.
  - Add an admin "Check now" endpoint, which is also useful in the demo.
  - Use a GitHub Actions cron as an external heartbeat that calls the same endpoint.
- **What it does on a hit:**
  1. Record a `pending` source event.
  2. Fetch and parse the document.
  3. Diff by content hash.
  4. Embed and extract only the changed chunks.
  5. Run gates.
  6. Promote in one transaction.
  7. Bump `snapshot_id`.
  8. Invalidate cached answers.

## 4. Design for our Neo4j model

**Vector-index filtering changes retrieval (VERIFIED).**
- Filtered vector indexes are GA in Neo4j 2026.02, Community included ([blog](https://neo4j.com/blog/genai/vector-search-with-filters-in-neo4j-v2026-01-preview/)). Our production image is `neo4j:2026.07.1-community`.
- Filter properties must be declared with `WITH [...]` at index creation. Changing them means a rebuild ([syntax](https://neo4j.com/docs/cypher-manual/current/indexes/syntax/)).
- `WHERE` inside `SEARCH` allows only `=`, `<`, `>`, `<=`, `>=`, `AND`, and `IN` from 2026.06. There is no OR, `<>` or IS NULL ([SEARCH](https://neo4j.com/docs/cypher-manual/current/clauses/search/)).
- So open-ended intervals need a sentinel `valid_to = date('9999-12-31')`. The retriever comment that filters compose after SEARCH is now outdated.
- `SEARCH … FULLTEXT` needs 2026.09, so BM25 must keep using `db.index.fulltext.queryNodes`, over-fetched then post-filtered. A filter inside fulltext search is undocumented (UNVERIFIED).
- Whether a later `SET e.is_current = false` is reflected inside an existing filtered index is UNVERIFIED. It must be an M1 integration test.
- The Fly Neo4j heap is 400 MB and page cache 200 MB. The memory cost of the filter properties is UNVERIFIED, so measure it.

**Two separate axes.** Keep document-version status apart from fact validity. `temporal.py` already handles fact validity (Active/Deleted lineage). Do not merge them.

| Where | New properties and relationships |
|---|---|
| `Document` (new) | `doc_id`, `source_type` (sec, federal_register, upload), `natural_key` (cik, family, fiscal_period_end, or an upload slug) |
| `Filing` (version node; add label `DocumentVersion` for uploads and Federal Register) | `version_seq`, `accepted_at` (ordering key), `effective_from`, `status` (current, superseded, amended, withdrawn, pending), `supersede_kind` (corrected or rolled), `scope` (full, sections, part3), `content_hash`, `snapshot_id`, `ingested_at`, `retired_at`, `last_verified_at`, `source_etag` |
| Relationships | `(:Filing)-[:VERSION_OF]->(:Document)`; `(new)-[:SUPERSEDES {scope, reason, at}]->(old)`; `CORRECTS` for Federal Register corrections; `(:Snapshot {id, at, counts})` |
| `EvidenceSpan` and `RiskFactor` | `content_hash`, `is_current` (boolean), `valid_from` (date), `valid_to` (date, sentinel), `filer_cik` (int), `form`, `accepted_at`, `change_type` (new, unchanged, modified), `prev_chunk_id`, `superseded_by`, `first_seen_at` |
| Fact edges | Keep `start_date`, `end_date`, `status`. Add `closure_basis` and `last_evidenced_at`. |
| `Metric` | One observation per `(cik, metric, period_end, accn)` with `filed` and `is_current` = max filed. |

Keep `chunk_id` positional so existing citations and benchmark answers stay valid.

**Supersession rules (deterministic, no LLM).**
- Corrected: an amendment or restatement replaces the old section. Set the old `valid_to` to the amendment's acceptance date and flag the old span stale-corrected.
- Rolled: a new period replaces the old one. The old content stays valid for its own period and is labelled "prior period", with no warning.
- Withdrawn: an upload or Federal Register withdrawal is retracted and hidden.
- Close a fact edge only when the latest version of the same section lacks its evidence.

**Retrieval and as-of.**
- Default: `WHERE e.is_current = true AND e.filer_cik IN $ciks`.
- As-of: `WHERE e.valid_from <= $asof AND e.valid_to > $asof`.
- History or trend questions drop the currency filter.
- An intent classifier chooses among latest, as_of and history.
- Apply a half-life recency prior to "latest" questions only, over co-current candidates.

**Content-hash diffing.**
- Normalise the text once, hash it, and fix the normalisation spec now. Changing it later invalidates every hash.
- Diff per section by hash set. Classify chunks as unchanged, modified (fuzzy match to a removed chunk) or new.
- Reuse embeddings and extractions from hash-keyed caches (`sha256(content_hash|model|prompt_version)`). The current embedding cache is parquet-row-order keyed and the extraction checkpoint is keyed by `chunk_id`. Both must become hash-keyed, or every update re-bills.
- Positional token-window chunking makes an inserted paragraph shift later chunk boundaries. Measure the reuse rate on the AMD original vs 10-K/A pair.

**UI.**
- Per-citation badge:
  - "Current (verified date)" for a current span.
  - Red "Corrected by 10-K/A …" for a corrected one.
  - Grey "Prior period (FY2025)" for a rolled one.
- Header: "Data as of <max accepted_at> · checked <last poll> · N filings pending".
- Staleness warnings:
  - A possible late filing when `now > expected_by + grace` with no NT.
  - A red feed-stale banner when the poller heartbeat exceeds 2× the interval.

**Answer prompt.**
- Inject `data_as_of` and, per context item, its form, accepted date and status.
- Require the model to state the status of any non-current evidence it cites.
- Add a post-check that flags stale citations, mirroring the existing hallucinated-citation check.

**Cache invalidation.**
- Include `snapshot_id` and intent in the cache key.
- Remove the benchmark TTL exemption.
- Bump the snapshot inside the promotion transaction.
- Later, add targeted invalidation from stored citations for answers that cite chunks that became non-current.

**Test plan.** Use two real snapshots. A is the current dump. B adds the AMD 10-K/A, the MSFT FY26 10-K, the Q2 10-Qs and the Federal Register correction.
- AMD Client revenue (units vs ASP) flips from A to B.
- NVDA "latest quarter" flips, and an as-of date of 2026-06-30 still returns Q1.
- MSFT latest annual risk factors flip, and an as-of date of 2026-06-01 returns FY25.
- Synthetic upload v1 to v2 with one changed paragraph triggers exactly 1 embed and 1 extraction call. A withdrawal hides the document.
- Synthetic XBRL restatement returns the new value as current and the old value as-of an earlier date.
- Invariants, checked by Cypher:
  - At most one current version per `(Document, section)`.
  - No current span under a non-current version.
  - Every Active edge has at least one current evidence chunk.
- Filtered SEARCH excludes a span after `SET is_current = false`.
- Five superseded near-duplicates do not shrink the top-k.
- A pre-promotion cached answer is not served afterwards.
- Re-polling changes nothing and does not bump the snapshot.
- Metrics:
  - Stale-citation rate is 0 for current-intent questions.
  - Outdated-in-top-5 rate, measured before and after the filter.

## Prioritised implementation list

**(a) Into the M1 versioned rebuild, to avoid rebuilding twice:**
1. Recreate the vector indexes on `EvidenceSpan` and `RiskFactor` with `WITH [is_current, filer_cik, form, valid_from, valid_to]`, using sentinel dates, plus a fulltext index.
2. Add the `Document` node, the version properties on `Filing`, the span, risk and edge properties, `SUPERSEDES`, `VERSION_OF`, `CORRECTS` and `Snapshot`, ordered by `accepted_at`.
3. Model `Metric` per accession (drop earliest-filed dedupe).
4. Compute `content_hash` on every chunk and make the embedding and extraction caches hash-keyed.
5. Widen ingest scope to include 10-K/A and all periodic filings, and retain superseded versions instead of deleting them.
6. Add `snapshot_id` to the answer-cache key, remove the benchmark TTL exemption, and add `Source` state nodes (watermark, latest_accession, last_success).
7. Add the filter-after-SET integration test and pin the Neo4j image at 2026.07.1 or later (`IN` needs 2026.06).

**(b) Later:** the poller and admin "Check now", the upload and diff UI, badges and warnings, the intent classifier and recency prior, the prompt and post-check changes, targeted cache invalidation, SLO dashboards, transaction-time replay, Federal Register withdrawal detection, and 8-K Item 4.02 handling.

**Scratch scripts:** `C:\Users\amit1\AppData\Local\Temp\claude\C--Users-amit1-OneDrive-Documents-Projects-ai-semiconductor-risk-intelligence-graphrag\7e3d5304-3ec1-40b7-9bfd-29c48c46aa97\scratchpad`
