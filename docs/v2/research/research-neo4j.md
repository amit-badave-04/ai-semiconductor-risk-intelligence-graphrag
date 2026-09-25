**Neo4j Community 2026.x: verified capabilities, limits and recommended setup for semigraph (report dated 2026-09-25)**

Tags: **[V]** seen live in docs, source, PyPI or GitHub. **[X]** executed by me. **[U]** unverified or inference. For [X] I used Neo4j's own analyzer classes and Lucene 10.4 from the local Neo4j Desktop 2026.05 jars, the langchain-neo4j 0.10.0 and neo4j 6.3.1 source, and the repo's own files (README, `retriever.py`, `serve/store.py`, the deploy scripts). I did not start any Neo4j server (no Docker or JDK here, and no permission to download one), so Community runtime behaviour is docs-level unless marked [X].

**0. Version**
- **[V]** 2026.09.0 was released 21 Sep 2026. It adds `SEARCH … FULLTEXT INDEX` and ~5x faster rescored-binary vector search. It also makes the page cache reserved at startup and hard-capped.
- **[V]** 2026.07.0 had a block-format UTF-8/`trim()` bug, fixed in 2026.07.1 (5 Aug). 2026.08.0 is flagged "do not use" (fixed in 08.1).
- v1 already pins `neo4j:2026.07.1-community`. `SEARCH…WHERE…IN` needs 2026.06 or later, so that is fine.
- Recommendation: build M1 on 2026.07.1. Run the 162 tests and the eval on 2026.09.0 in CI. Promote to 2026.09 at its first patch. Whether 2026.09 becomes an LTS release is [U].
- Version skew: the local Desktop DBMS is 2026.05.0 Enterprise, which has no `IN` in `SEARCH WHERE` and no rescored-binary quantization. Do not validate queries there.

**1. Community vs Enterprise [V, raw table HTML plus constraints pages]**
Community has:
- GPLv3, one user database plus `system`, aligned store format only, and slotted runtime only.
- Uniqueness constraints only.
- Vector and fulltext indexes, composite and range indexes on nodes and relationships, APOC Core, and GDS Community.
- `db.transaction.timeout`, `db.memory.transaction.max`, and `server.databases.read_only`, none of which is flagged Enterprise. I found no documented CPU or memory cap.

Enterprise-only things you might wrongly assume exist:
- existence, type and key constraints (`IS NOT NULL` must be validated in the app);
- parallel and pipelined runtimes;
- block format, so native `VECTOR` properties are out and embeddings stay `LIST<FLOAT>`;
- `neo4j-admin database copy`;
- CDC (no change feed);
- the Prometheus metrics endpoint;
- the query-log setting (docs say Enterprise; runtime [U]);
- `CREATE DATABASE`, clustering and read replicas, RBAC (no read-only role), and online backup.

**2. Vector search**
```
CREATE VECTOR INDEX ev IF NOT EXISTS FOR (e:EvidenceSpan) ON e.embedding
 WITH [e.filer_cik, e.source_kind, e.is_current, e.doc_date, e.valid_to]
 OPTIONS {indexConfig:{`vector.dimensions`:1024,`vector.similarity_function`:'cosine',
                       `vector.quantization.type`:'none'}}
MATCH (e:EvidenceSpan) SEARCH e IN (VECTOR INDEX ev FOR $vec
  WHERE e.filer_cik IN $ciks AND e.is_current = true LIMIT 60) SCORE AS score
```
- **[V] Filter grammar.**
  - Allowed: `= < <= > >=`, `IN` (from 2026.06), and `AND`.
  - Not allowed: `OR`, `<>`, `NOT` (booleans only), string operators, `IS NULL`, and LIST-typed properties.
  - The same property cannot be filtered with both `=` and a range, or twice in the same direction.
  - Filter properties must be declared in `WITH` at creation. Allowed types are INTEGER, FLOAT, STRING, BOOLEAN, DATE, datetimes and DURATION. Anything else counts as null and is excluded.
- **[V] In-index filtering.** Search continues until k matching results are found. A plain `WHERE` in the `MATCH` is only post-filtering. v1 currently post-filters after `LIMIT 40/60`, which loses recall for rare companies.
- **Community availability.** [V] Neo4j's blog says filtered search is available across EE, CE and Aura and has been GA since 2026.02. [V] A Neo4j staff reply on the forum covers 2026.04.0-community. v1 already runs `SEARCH` (without `WHERE`) on CE. The `WHERE` form itself is not executed by me [U], so make it the first M1 probe.
- **Score and limits.** The score is bounded 0 to 1 and should be used for ranking only. `LIMIT` accepts up to 2^31−1.
- **Tuning.**
  - There is no per-query `ef_search`.
  - The knobs are `LIMIT` (over-fetch), the index-level `vector.default_search_expansion_factor` (2026.07+, candidates ≈ LIMIT × factor), `vector.hnsw.m` (default 16, max 512), and `ef_construction` (default 100, max 3200).
  - The default quantization changed from scalar to rescored binary in 2026.08, so pin it explicitly.
- **Recommended settings.** The corpus is ~43 MB at full precision, so use quantization `none`, `m` 32, `ef_construction` 200 and expansion factor 1–2. Verify recall against exact cosine on the eval set.
- **[X] Engine floor.** Lucene 10.4 HNSW on 10.5k × 1024 synthetic clustered vectors, on a 24-core desktop with no Bolt or Cypher overhead:
  - p95 0.8 ms unfiltered and 1.65 ms with a 7% selective filter (k=40);
  - recall@40 of 0.99–1.0.

  The vector engine is not the bottleneck.
- **Denormalize onto `EvidenceSpan`:** `filer_cik` (INT), `source_kind` (STR), `is_current` (BOOL), `valid_to` (DATE, sentinel 9999-12-31, never null), `doc_date` (DATE), `doc_version` (INT), and `corpus` (`'seed'` or an upload-set id). On `RiskFactor`, add `filer_cik`, `is_current` (status currently lives on the `DISCLOSES_RISK` relationship), and `first_seen`/`last_seen`.
- **`MENTIONS` limit.** `MENTIONS` is multi-valued and cannot be a vector filter. Post-filter it with over-fetch, or filter it in fulltext (see section 3).
- **Recreate both indexes.** They have no `WITH` properties today.

**3. Fulltext and hybrid**
- **[V] Engine.** The engine is Lucene 10.5.1 on the 2026.09 branch. Queries go through `MultiFieldQueryParser` with OR as the default operator.
- **[X] Scoring.** Scoring is BM25. I executed Lucene 10.4 and its default similarity is `BM25Similarity`, and Neo4j's source sets no override. The docs only say "approximation score".
- **[X] Analyzers (Neo4j's own).** The default `standard-no-stop-words` keeps `3A090`, `H200`, `HBM3E` and `CoWoS` as single (lowercased) tokens. It splits `0001045810-26-000075` into `[0001045810, 26, 000075]`, `2026-00789` into `[2026, 00789]`, and `10-K/A` into `[10, k, a]`.
- **Quoting identifiers.** An unquoted ID becomes an OR of its pieces, which is noisy. A quoted ID becomes an exact phrase query, so no custom analyzer jar is needed.
- **Analyzers to avoid.** `simple` deletes digits. `english` stems (CoWoS becomes `cowo`).
- **Other analyzers.** `whitespace` and `keyword` keep identifiers whole but are case-sensitive. For an `ids` LIST<STRING> property, add a second fulltext index with the `keyword` analyzer, or use `Identifier` nodes with a range index.
- **[V] Filtering.** Fulltext has no `WHERE`, in either `SEARCH FULLTEXT` or `queryNodes`.
  - Fulltext does index LIST<STRING>. Put `filer_cik`, `mentions_ciks` and `fresh` in the index as strings, then add Lucene clauses such as `+mentions_ciks:1045810 +fresh:current`. I parsed this form successfully [X].
  - Escape user text. In Lucene syntax `/` starts a regex, `-` prohibits, and `:` selects a field.
- **[X] langchain-neo4j hybrid** (via neo4j-graphrag 1.21.0):
  - It uses max-normalization per list, then `max()`, or an alpha-weighted sum. It is not RRF.
  - It raises "Filters are not supported with hybrid search".
  - It uses the deprecated `db.index.vector.queryNodes` procedure.
  - `remove_lucene_chars` strips `- / " :`, which destroys accession and FR document numbers.
- **[V]** Neo4j's own developer guide has a weighted-RRF Cypher recipe. I recommend RRF in Python over two Cypher calls, because it is unit-testable and leaves a slot for a reranker.
- **Replacing bm25s.** In-Neo4j BM25 can replace it. It is transactional with uploads, needs no rebuild, and has one source of truth. Caveats: IDF still counts superseded chunks (so filter them out), and k1/b are not tunable.

**4. Concurrency**
- **[V] Driver.** neo4j 6.3.1 (2026-09-15, Apache-2.0) has these defaults: pool 100, acquisition timeout 60 s (set ~5 s), connect timeout 30 s, connection lifetime 3600 s.
  - `execute_query` defaults to `routing_=WRITE`, so pass `READ`.
  - `Query(timeout=)` is capped by the server's `db.transaction.timeout`, which defaults to 0 (off). Set it to 10–15 s and also set `db.memory.transaction.max`.
  - Use one session per coroutine under `asyncio.gather` [U].
  - Server defaults: Bolt threads max 400, concurrent transactions 1000.
- **[V] v1 issue.** The API uses the sync driver via `run_in_threadpool`. It also queries Neo4j on every request for the kill switch, the answer cache and the ledger write, cache hits included.
  - Move that state out of the knowledge-graph database, or memoize the policy read for ~5 s.
  - Key cached answers by graph version, using the dump SHA you already have.
- **Sizing.**
  - **[V]** The store is ~50 MB. The vector index lives in the OS file cache, not the page cache. 2026.09 reserves the full page cache at startup.
  - The current 1 GB VM with 400m heap and 200m page cache is tight. Move to 2 GB with heap 512m and page cache 256m.
- **p95 [U].** 15 live questions/s × 5 queries is ~75 small reads/s plus cached traffic, which a single instance handles easily. My estimate is 30–150 ms for the five queries run sequentially and 15–60 ms with `gather`, on shared-cpu-2x. Measure it in M1. Query embedding and the LLM slots dominate, not Neo4j.
- **Single-instance limit.** One instance stops being enough for availability (host failure), not throughput.
- **"N copies from one dump."**
  - **Feasibility [V].** A Fly volume attaches to exactly one machine. v1 already restores from a baked dump. Balance with Flycast or with N async drivers, and set `server.databases.read_only=neo4j` (runtime on CE [U]).
  - **Cost [V].** From Fly's price list effective Oct 1: shared-cpu-2x at $4.39/mo plus $6/GB of RAM is about $14/replica/month always-on including the volume. A stopped machine costs only its rootfs.
  - **Limits.** Uploads and ledger writes cannot go to replicas, and Community has no replication. That means one writer or app-level idempotent dual-write. `fly deploy --strategy bluegreen` does not work with volumes [V], so blue/green must be one Fly app per data generation. I would not do this in v1.

**5. Freshness and rebuild**
- **Model.** Keep the bitemporal edges. Add node-level flags for filtering: `is_current`, `valid_from`, `valid_to` (sentinel), `doc_id`, `doc_version`, `content_hash`, `superseded_by`, `ingested_at`.
- **Documents.** Model versions as `(:SourceDocument {doc_id, version, sha256})-[:SUPERSEDES]->(:SourceDocument)`. Include the version in chunk ids.
- **Upload of v2.** Do it in one transaction: hash-diff the chunks and reuse embeddings for unchanged ones, add new chunks, then set the old ones to `is_current=false` and `valid_to=today`.
- **Read-time staleness.** Compute "a newer filing exists" from `Filing.filing_date`. Add a range index on `(doc_id, is_current)`.
- **Constraints.** Existence constraints are unavailable in Community, so validate in the app.
- **Full rebuild.** Community has no second database, so use a generation dump-swap. Build offline, run `neo4j-admin database dump`, bake it into the image, start a new app, run the eval, flip the API target, and keep the previous generation for rollback.
  - Label-versioned graphs collide with the unique `chunk_id` constraint and leak into every query.
  - In-place staging needs one huge transaction and duplicate keys.
- **Warning.** `restore-and-start.sh` runs `load --overwrite-destination` whenever the seed SHA changes. That wipes uploaded documents and the ledger. Keep raw uploads and a manifest in object storage and replay them after a rebuild.

**6. langchain-neo4j 0.10.0 [V]**
- MIT, released 2026-06-10, roughly one release every 2.5 months, recent commits are Dependabot only, and it depends on neo4j-graphrag ≥1.12 and langchain-classic.
- **[X]** `Neo4jVector`, `Neo4jGraph` and `Neo4jChatMessageHistory` contain zero `async def`. Only `AsyncNeo4jSaver` (a LangGraph checkpointer) is async. It writes Thread and Checkpoint nodes into the single Community database, so use SQLite or Redis instead.
- **Verdict.** Write your own thin async Cypher layer exposed as LangGraph tools.
- neo4j-graphrag 1.21.0 (Apache-2.0, 2026-09-23) uses `SEARCH` with in-index filters but falls back to brute force if a property is not declared. Treat it as reference only.
- Do not expose Text2Cypher publicly. Community has no read-only role, so use parameterized READ transactions only.

**7. GDS**
- **[V]** GDS Community is free, includes all algorithms, is limited to 4 CPU cores and 3 stored models, and its Neo4j-owned code is GPLv3. The jar is bundled in the server's `products/` directory. `NEO4J_PLUGINS='["graph-data-science"]'` copies it and sets `dbms.security.procedures.unrestricted=gds.*`. Whether the CE Docker image actually contains `/products` is [U].
- **Chokepoint centrality.** It is a cheap highlight. Compute betweenness and PageRank in the offline build and store them as `Company` properties, so the demo instance carries no GDS memory risk. Label the result "as-disclosed" (26 companies, disclosure-derived edges), with click-through to the evidence.

**8. Backup and DR**
- **[V]** In Community, dump and load run only with the DBMS offline. 2026.09 adds `--split-archive-part-size`. Load can read from S3, GCS or Azure paths.
- **[V]** Aligned is the Community default format, and `database copy` is Enterprise-only. v1's block→aligned step therefore works only on Desktop.
- **Fix.** Build in a Community container at the prod version so the dump is natively aligned. No Docker is installed here, so use the Community tarball with Desktop's Zulu 21 JRE, or install Docker.
- **[V] Fly snapshots.** Volumes get daily block snapshots, kept 5 days by default (1–60), at $0.08/GB-month. They are crash-consistent, not application-consistent (behaviour with Neo4j [U]).
- **DR plan.** Keep source documents, the manifest and the pipeline in object storage, plus one dump per generation. For a hot export use `apoc.export.json.all(null,{stream:true})`, which needs no file config.

**Recommended configuration**
- Neo4j 2026.07.1-community (CI on 2026.09.0). `db.query.default_language=CYPHER_25`, plus a `CYPHER 25` prefix in every query.
- 2 GB VM: heap 512m, page cache 256m, `db.transaction.timeout=15s`, `db.memory.transaction.max=64m`, `db.memory.transaction.total.max=256m`.
- Vector indexes with `WITH` filter properties, quantization `none`, `m` 32, `ef_construction` 200.
- Fulltext: `standard-no-stop-words` on text, plus a `keyword` index on `ids`.
- Async driver: pool ~50, acquisition timeout 5 s, `routing_=READ`. Move the answer cache and ledger out of the database.
- RRF in Python. Answer cache keyed by graph version.
- Generation dump-swap. Raw uploads stored outside Neo4j.

**Top 5 pitfalls**
1. Dev/prod version skew, and `.0` releases (07.0 and 08.0 both regressed).
2. `SEARCH WHERE` has no `IS NULL`, `OR` or LIST filters, so use booleans and date sentinels and recreate the indexes.
3. Lucene escaping versus identifiers: `remove_lucene_chars` destroys IDs, so quote them and escape the rest.
4. State stored in Neo4j (ledger, cache, checkpoints, uploads) breaks replicas and dump-swap rebuilds.
5. Community and Enterprise format traps: block vs aligned, Enterprise-only `copy`, no existence constraints, and no metrics or read-only role.

**Sources (all fetched 2026-09-25)**
- neo4j.com/docs/operations-manual/current/introduction/
- neo4j.com/docs/cypher-manual/current/clauses/search/
- neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/
- neo4j.com/blog/genai/vector-search-with-filters-in-neo4j-v2026-01-preview/
- neo4j.com/release-notes/database/neo4j-2026-09-0/
- neo4j.com/developer/genai-ecosystem/hybrid-search/
- github.com/neo4j/neo4j (2026.09 branch)
- pypi.org/pypi/neo4j/json
- fly.io/pricing-update/

Probe scripts are in the session scratchpad: `n4j\lucene_probe.py` and `n4j\hnsw_probe.py`.
