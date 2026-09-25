# FalkorDB vs Neo4j for semigraph v2: verified findings and recommendation

**Recommendation.** Primary is FalkorDB pinned to ≥4.20.7, self-hosted as `falkordb-server`. It is conditional on two things: SSPLv1 is acceptable, and the 4.20.x spike below passes. Fallback is Neo4j Community, which needs no rewrite and costs $0.

**Read this first.**
- **Neo4j is not the paid line item.** Community is GPL-3.0 and free (VERIFIED: https://github.com/neo4j/neo4j). Your Neo4j Fly VM is 1 GB shared-cpu-1x (`deploy/neo4j/fly.toml`), about $5.9/month by my arithmetic from https://docs.fly.io/about/pricing/. The WebFetch summary said $59, which is wrong by 10x. Sonnet is the real cost.
- **What FalkorDB buys.** Free async read replicas, since Community has no clustering (search-result summary, UNVERIFIED against Neo4j docs), and a smaller footprint. It does not buy licence savings.
- **Everything I measured ran on FalkorDB v4.18.3, not 4.20.7.** I used falkordblite 0.10.0 in WSL2 Ubuntu 24.04. Its commit "update FalkorDB server to v4.18.3" confirms the version, and `MODULE LIST` reported ver 41803. I did not download the 4.20.7 binary because that needs your consent. Scripts are in `C:\Users\amit1\AppData\Local\Temp\claude\C--Users-amit1-OneDrive-Documents-Projects-ai-semiconductor-risk-intelligence-graphrag\7e3d5304-3ec1-40b7-9bfd-29c48c46aa97\scratchpad\spike*.py`.
- **The vector count is wrong in the brief.** The graph has 10,574 vectors (2,492 spans plus 8,082 risks), not about 3k.

## Verified facts
- **Releases.** v4.20.7 shipped 2026-09-24, and there are about 40 tags since 2025-10-19 (`gh api repos/FalkorDB/FalkorDB/releases`).
- **Python client.** falkordb-py 1.7.1 (2026-08-13, MIT, Python ≥3.10) ships asyncio, cluster and sentinel modules (inspected locally).
- **Licence.** SSPLv1, with §13 at LICENSE lines 478-490 (https://github.com/FalkorDB/FalkorDB/blob/main/LICENSE). FalkorDB's FAQ says a service exposing it "as an API" must publish its whole service source (https://docs.falkordb.com/references/license.html). A public GraphRAG API arguably triggers that, and it would apply to your own repo, which currently has no LICENSE file. This is not legal advice. SSPL is fine for a fully public repo and a problem for enterprise or closed deployment.
- **Redis dependency.** It needs Redis 8 (RSALv2 / SSPLv1 / AGPLv3) plus RediSearch. FalkorDB's docs never mention Valkey.
- **Cloud Free is unusable for us.** It has a 100 MB RAM cap, no persistence, is stopped after 1 idle day and deleted after 7 (https://docs.falkordb.com/cloud/tiers/free). Our data used about 152 MB. Startup costs from $73 per GB per month (https://www.falkordb.com/pricing/), so self-hosting is the free path.

## Parity table (tested on 4.18.3)

| Construct (file:line) | Result | Action |
|---|---|---|
| `SEARCH n IN (VECTOR INDEX …) SCORE AS score` (retriever.py:95,113,129) | FAIL | Use `CALL db.idx.vector.queryNodes(label, attr, k, vecf32($v)) YIELD node, score`. |
| Score direction (retriever.py:99,116) | Different | FalkorDB returns cosine distance ascending: 0.2517 vs exact cosine 0.7483. Change to `ORDER BY score ASC`. Neo4j's score is (1+cos)/2 in [0,1] (VERIFIED: Neo4j Cypher manual). Nothing downstream reads `score`. |
| Vector index DDL (schema.cypher:33-37) | Rewrite | `CREATE VECTOR INDEX FOR (e:EvidenceSpan) ON (e.embedding) OPTIONS {dimension:1024, similarityFunction:'cosine', efRuntime:64}`. No `IF NOT EXISTS`; re-creating errors "already indexed". |
| Embedding writes (loaders.py:325,430) | Rewrite | Wrap as `vecf32(row.embedding)`. |
| Unique constraints (schema.cypher:1-17) | Rewrite | Cypher `CREATE CONSTRAINT` fails. Use `GRAPH.CONSTRAINT CREATE` (`create_node_unique_constraint`) after the range index; it goes PENDING then OPERATIONAL. |
| Composite relationship index (schema.cypher:25-31) | Differs | It becomes per-property RANGE indexes. A second index on the same attribute errors. |
| `UNWIND`+`MERGE` batches, `ON CREATE/ON MATCH SET`, `CASE`, list `+`, `IN`, chained `WITH…UNWIND…MATCH…MERGE` (loaders.py:161-447) | PASS unchanged | Re-running a batch of MERGEs on existing nodes did not change node or relationship counts. |
| `date()`, `toString(date)`, `min/max(date)`, date comparisons | PASS | None. `datetime()` fails (store.py:91), but that code is being replaced. `elementId` fails, but the repo never uses it. |
| `*1..2` typed alternation, label-OR `WHERE`, `UNWIND relationships(p)`, `startNode/endNode/type` (retriever.py:79-85) | PASS | Same relationship sets as a Python reference for 4 anchors. Recheck on 4.20.x, because v4.20.0 changed variable-length traversal (#2156). |
| `collect(x)[0]`, `toString(min())`, OPTIONAL MATCH chain, `collect(DISTINCT)`, `CALL {}`, list and pattern comprehensions | PASS | None. |
| `left(reduce(… collect(e.text)[..5] …))` (loaders.py:599-600) | FAIL "Invalid use of aggregating function" | Aggregate in a `WITH` first; my rewrite passed. |
| `EXISTS { }`, regex `=~` | FAIL | Unused in the repo. |
| Neo4j driver over Bolt | Removed | v4.20.0 notes say "remove bolt support". Use falkordb-py. |
| Boot-time schema apply (main.py:60) | Not idempotent | Catch "already exists". |

Also, `temporal.py:95` calls `sort_values("filing_date")` with no tie-break, and `fetch_annual_risks` has no `ORDER BY`. The greedy lineage clustering is order-sensitive, so lineage_ids and Deleted counts can differ after a rebuild. Sort by `["filing_date", "risk_id"]` with a stable sort. This is inferred, not measured.

## Vector behaviour (measured on 4.18.3 unless noted)
- **Update bug.** Setting any property on a vector-indexed node halves effective top-k: 40 distinct results became 20 after one `SET` and 13 after two.
  - PR #2197, merged 2026-07-13, fixes this. Its test is present at v4.20.0 and v4.20.7 and absent at v4.18.3.
  - The test only asserts k=1, so whether dilution is fully fixed is UNVERIFIED.
  - v1's temporal writes SET every RiskFactor after embedding, so it would trigger this. Create vector indexes last, and add a regression test.
  - FalkorDBLite lags the server by about 5 months and ships the bug. It has no Windows wheels and needs glibc ≥2.39, so it works only for Linux unit tests.
- **Async index build.** A 3,000×1024 index took 0.6 s to reach OPERATIONAL. Queries during build fail with "Invalid arguments for procedure".
- **Recall.** On 8,082 synthetic 1024-d vectors, default efRuntime=10 gave recall@8 of 0.875. efRuntime≥64 gave 1.0 at both @8 and @40, so set 64 or higher. Per-query override is not documented.
- **No in-index filtering.** The docs say vector queries don't combine with property filters. Neo4j 2026.02+ has `SEARCH … WHERE` (VERIFIED: Neo4j Cypher manual).
- **Scoped exact search works better than ANN plus post-filter.** Anchor-scoped `MATCH … vec.cosineDistance(rf.embedding, vecf32($v))` gave the exact top-6 in 40 of 40 queries at p50 1.9 ms with about 497 risks per company. A scan of all 8,082 risks took 6.5 ms. ANN(40) plus post-filter returned 2.27 of 6 rows, but that run was confounded by the update bug. The structural weakness (about 3 survivors from 40 neighbours across 13 companies) also affects v1's Neo4j query.

## Scale, memory and persistence
- **Concurrency model.** Reads run in parallel, writes serialize per graph, and `THREAD_COUNT` defaults to hardware threads. Clusters shard by graph key, so they don't scale a single graph. Replicas are async and served through `GRAPH.RO_QUERY`.
- **Immutable-snapshot scaling.** The serving graph is read-only. So N identical instances loaded from one RDB, with versioned graph names such as `semigraph_20260925` for blue/green swaps, avoid replication entirely.
- **Throughput.** A synthetic saturation run reached 1,339 full retrievals per second (about 4,000 queries per second) at 96 clients, p95 92 ms. It ran over a unix socket on the same 24-thread box as the load generator, with no Neo4j number. The vendor's "496x" benchmark is vendor-run (https://www.falkordb.com/blog/graph-database-performance-benchmarks-falkordb-vs-neo4j/). The database will not be the 10k-user bottleneck, but "at par with Neo4j" is not proven.
- **Footprint.** 10,905 nodes and 26,722 relationships took 152 MB used and about 290 MB RSS. The RDB was 46.7 MB and reloaded in under a second. Loads ran at 1.3-1.7k rows per second, roughly 8 s for all vectors.
- **Timeout default.** Recent `falkordb/falkordb` Docker images default to `TIMEOUT` 1000 ms (open issue #1826). Set the timeout config explicitly.
- **Hosting.** Fly costs about $5.9/month for 1 GB plus $0.15 per GB-month for the volume. Oracle Always Free was cut to 2 OCPU / 12 GB on 2026-06-15 and idle instances are reclaimed (p95 CPU, network and memory under 20% for 7 days), so a quiet demo box is at risk (https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).
- **Migration.** Rebuild from the parquet, jsonl and embedding caches with the existing loaders. Don't go dump to CSV; the official tool doesn't mention vectors (https://docs.falkordb.com/operations/migration/neo4j-to-falkordb.html).

## Alternatives (status checked live)
- **Neo4j Community.** GPL-3.0; the 2026.09.0 tag exists. AuraDB Free allows 200k nodes and 400k relationships, pauses after 72 h idle, and deletes after 30 paused days. Aura Pro is $65/GB/month. Vector support on Aura Free is UNVERIFIED.
- **ArcadeDB.** Apache-2.0, release 26.9.1 (2026-09-03). It claims native OpenCypher, a Neo4j-style `db.index.vector.queryNodes` and Raft HA. Its TCK figure and compatibility are vendor claims I did not test. It is the permissive option if SSPL is rejected.
- **Memgraph.** BSL 1.1 restricted to internal business purposes, so not open source, and Community HA is manual.
- **Kuzu.** Archived 2025-10-10 (v0.11.3). The MIT forks LadybugDB and Bighorn are active, but all are embedded and single-process, which doesn't suit 10k users.
- **Apache AGE.** Apache-2.0, supports PG 11-18, but is not offered on Neon and not officially on Supabase.
- **Postgres + pgvector.** Neon Free is 0.5 GB and 100 CU-hours (https://neon.com/docs/introduction/plans). Our vectors are about 43 MB raw, so it fits (estimate, not benchmarked). It would give pre-filtered vector search and the official `langgraph-checkpoint-postgres`. It drops Cypher and the graph-DB identity.
- **SurrealDB.** Not Cypher; skipped.

## Integrations
- **langchain-falkordb 0.2.0** (MIT, 2026-07-06, maintained by FalkorDB, 10 stars) provides `FalkorDBGraph`, `FalkorDBVector`, `FalkorDBQAChain`, `FalkorDBSaver` and `FalkorDBChatMessageHistory`. It installed and imported with langchain 1.4.2, langgraph 1.2.12 and falkordb 1.7.1 on Python 3.13.
- **langchain-community is archived** ("No longer maintained"), so its FalkorDB classes are out.
- **GraphRAG-SDK 1.4.0** (Apache-2.0) failed to build hnswlib on Windows and is an opinionated pipeline. Skip it and write a custom `BaseRetriever` around our Cypher.

## Service state (ledger, cache, kill switch)
- **Where it goes.** Move it to a separate Valkey (BSD-3) or Upstash. Upstash Free is 256 MB and 500K commands per month; pay-as-you-go is $0.20 per 100K (https://upstash.com/docs/redis/overall/pricing).
- **Mapping.**
  - The kill switch becomes a plain key.
  - The daily paid count becomes `INCR paid:{day}` with `EXPIRE`.
  - The answer cache becomes `SET ans:{sha} EX ttl`; benchmark seeds get no TTL.
  - The ledger becomes a capped stream, with Langfuse for traces and cost.
- **Do not set an eviction policy on the graph instance.** The whole graph is one key.
- **Checkpointer.** `langgraph-checkpoint-redis` needs RedisJSON and RediSearch, but FalkorDB's Redis exposes only `graph` and `vectorset`. Use the Postgres checkpointer.

## De-risking spike (LLM-free, run first)
1. Run `falkordb-server` ≥4.20.7 pinned by digest, with timeout config set and `noeviction`.
2. Re-run my `spike.py` and `spike3.py` through `spike5.py` on it, and make the update-dilution and efRuntime checks permanent tests.
3. Put the loaders behind a `GraphStore` interface and port the DDL, `vecf32` and constraint changes. Rebuild from the data lake and create vector indexes last.
4. Run `hybrid_retrieve` on both backends for the 20 gold questions. Diff the edges, metrics, risks, temporal and chunks layers by id (Jaccard per layer, plus Deleted-lineage counts). Only then run answers and judges.
5. Load-test with real embeddings, multi-process, against Neo4j on the same box.
