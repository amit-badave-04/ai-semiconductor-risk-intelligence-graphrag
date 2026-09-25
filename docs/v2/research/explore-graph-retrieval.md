Facts only. Repo root: `C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag\src\semigraph\`. Paths below are relative to it.

## 1. Graph schema
Source of truth: `artifacts/schema.cypher`. It is applied by `graph/schema.py:22` `apply_schema`, which splits on `;` and runs each statement, all idempotent with `IF NOT EXISTS`. `serve/main.py:60` also calls it at startup.

**Node labels and unique keys** (`schema.cypher:1-17`):

| Label | Unique key |
|---|---|
| Company | `cik` and `name` |
| Filing | `accession_no` |
| FilingSection | `section_key` (= `accession:section_id`) |
| Metric | `metric_id` (= `cik:metric:period_end`) |
| RiskFactor | `risk_id` |
| EvidenceSpan | `chunk_id` |
| Product | `name` |
| ExportControl | `rule_id` |

**Properties, from the loaders** (`graph/loaders.py`):
- Company: `cik`, `ticker`, `name`, `tier`, `sec_filer`. Samsung gets a synthetic negative cik.
- Filing: `form`, `filing_date` (date), `url`.
- FilingSection: `section_id`, `title`, `n_chars`.
- Metric: `metric`, `concept`, `value`, `unit`, `period_start`, `period_end` (dates).
- EvidenceSpan: `text`, `kind`, `sub_heading`, `char_start`, `char_end`, `n_tokens`, `source_url`, `embedding`.
- RiskFactor: `summary`, `category`, `embedding`, plus `lineage_id`, `first_seen`, `last_seen` (from temporal).
- Product: `type`.
- ExportControl: `title`, `date`, `url`, `abstract` (`left(…,1000)`).

**Relationship types:**
- `FILED {date}`: Company to Filing.
- `HAS_SECTION`: Filing to FilingSection.
- `REPORTS_METRIC {accession_no}`: Company to Metric.
- `FROM_SECTION`: EvidenceSpan to FilingSection.
- `MENTIONS`: EvidenceSpan to Company.
- `SUPPLIES_TO`, `DEPENDS_ON`, `CUSTOMER_OF`, `COMPETES_WITH`: Company to Company, with `start_date`, `end_date`, `status`, `evidence_chunk_ids` (list), `evidence_quote`. `loaders.py:62` lists these four types, and `_RELATION_CYPHER` is at `:418`.
- `HAS_EVIDENCE {quote}`: RiskFactor to EvidenceSpan.
- `DISCLOSES_RISK {start_date, end_date, status}`: Company to RiskFactor.
- `MENTIONED_IN`: Product to EvidenceSpan.
- `AFFECTED_BY {start_date, status, evidence_chunk_ids}`: Company to ExportControl.

**Range indexes** (`schema.cypher:19-31`):
- Node indexes: `Company(ticker)`, `Filing(filing_date)`, `Metric(period_end)`.
- Relationship composite indexes on `(start_date, end_date, status)` for `SUPPLIES_TO`, `DEPENDS_ON`, `DISCLOSES_RISK`, `AFFECTED_BY`. There is none for `CUSTOMER_OF` or `COMPETES_WITH`.

**Vector indexes** (`schema.cypher:33-37`), both created with `vector.dimensions: 1024` and `vector.similarity_function: 'cosine'`:
- `evidence_embedding` on `EvidenceSpan.embedding`.
- `risk_embedding` on `RiskFactor.embedding`.

**Embedder** (`embeddings.py`): Qwen3-Embedding-0.6B, `EMBED_DIM = 1024` (`:30`). Vectors are L2-normalized. The query side uses an "Instruct: …" prompt (`:34`, `:60-65`). There are three backends (sentence-transformers, `embeddings_onnx.py`, `embeddings_remote.py`).

**Service labels** (`serve/store.py:119-128`):
- Constraints: `SvcPolicy.key` unique, `SvcAnswer.key` unique.
- Index: `SvcQuery(day)`.
- `DROP INDEX … IF EXISTS` is used for legacy index names.

## 2. Cypher inventory

**Neo4j- or Cypher-25-specific**
- `SEARCH n IN (VECTOR INDEX idx FOR $vec LIMIT k) SCORE AS score` is used 3 times and is the Neo4j 2026 grammar:
  - `retrieval/retriever.py:94-95` (risk_embedding, LIMIT 40).
  - `retriever.py:112-113` (evidence_embedding, LIMIT 60).
  - `retriever.py:128-129` (evidence_embedding, LIMIT $k).
  - The docstring (`:7-14`) says `db.index.vector.queryNodes` is deprecated and no longer used. No queryNodes calls remain.
- `CREATE VECTOR INDEX … OPTIONS {indexConfig: {…}}` at `schema.cypher:33,36`.
- `CREATE CONSTRAINT … REQUIRE … IS UNIQUE` at `schema.cypher:1-17` and `store.py:125-126`.
- Relationship-property indexes `FOR ()-[r:T]-() ON (…)` at `schema.cypher:25-31`.
- Composite range indexes at the same lines.
- `DROP INDEX … IF EXISTS` at `store.py:123-124`.
- No fulltext index or query, no `elementId`, no APOC, no `CALL {}` subqueries, and no `CALL IN TRANSACTIONS` anywhere.

**Temporal functions and types**
- `date()` is used in `loaders.py:189-190,246,571,617` and `temporal.py:162-165,196`.
- `datetime()` and `duration({hours:$ttl})` are used at `store.py:91` for the cache TTL.
- `toString(date)` is used at `retriever.py:89,106` and `temporal.py:151`.
- Timestamps are stored as ISO strings (`store.py:28`).

**Write patterns**
- `UNWIND $rows AS row … MERGE` is used at `loaders.py:161,186,204,242,272,321,418,428,437,569,614` and `temporal.py:135,160`.
- `MERGE … ON CREATE SET … ON MATCH SET` is used at `:418-426,435,617`.
- `MERGE` on a relationship with property `{quote: row.quote}` is at `:432`.
- `CASE WHEN` inside `SET` is at `:423-426` (list append and earliest-date logic) and `temporal.py:165`.
- List concatenation `r.evidence_chunk_ids + row.chunk_id` and the `IN` list test are at `:423-424`.
- `WITH e, row UNWIND row.mentions AS eid` (a nested unwind) is at `:328`.

**Read patterns**
- Variable-length paths `[r:A|B|C|D|E*1..{hops}]`, which are undirected and use a relationship-type union, are at `retriever.py:79`. `hops` is interpolated by f-string; the default is 2.
- `relationships(p)`, `startNode`, `endNode`, `type()`, and `coalesce(startNode(rel).name, startNode(rel).title)` are at `:82-83`.
- `WHERE (b:Company OR b:ExportControl)` is a label predicate in `WHERE` (`:80`).
- Aggregations:
  - `min`, `max`, `collect(...)[0]`, and `count(DISTINCT)` at `retriever.py:105-107` and `temporal.py:199`.
  - `collect(DISTINCT …)` and `reduce(t='', x IN collect(e.text)[..5] | t + ' ' + x)` at `loaders.py:599-600`. That is a list slice `[..5]` plus `reduce`.
  - `left(…, 8000)` at `:600`.
- `RETURN DISTINCT`, `ORDER BY … LIMIT`, and `LIMIT $k` are used throughout.
- Property-map filters on relationships (`{status:'Active'}`, `{status:'Deleted'}`) are at `retriever.py:96,103`.
- `count(*)` grouped by `status` is at `temporal.py:168`.
- `sum(coalesce(q.cost_usd, 0.0))` is at `store.py:76,79`.
- `IS NULL` and `IS NOT NULL` are used at `temporal.py:148,163`.
- There are no pattern comprehensions, no list comprehensions, and no map projections.
- `OPTIONAL MATCH` is not used anywhere.
- `run_cypher` is defined twice, at `graph/client.py:39` and `retrieval/retriever.py:41`. Both are the same `session.run` wrapper.
- The DBMS is not explicitly a database name; the default database is used.

## 3. Hybrid retriever (`retrieval/retriever.py:65-120`, `hybrid_retrieve`)

**Anchor detection**
- `detect_anchors` (`:59`) matches the question against word-boundary, case-insensitive alias regexes. The regexes are built from `load_canonical_entities()` over name plus aliases (`:47-56`, LRU-cached). There is no LLM involved.
- The result maps canonical name to `entity_id`, which is the cik.
- If nothing matches, the anchor falls back to `DEFAULT_ANCHOR_CIK = 1045810` (Nvidia) (`:38,74`).

**Query embedding**
- `embedder.encode_query(question)` runs once and is reused for both vector queries (`:75`).

**Five blocks**
1. **Relations** (`:76-85`): a 2-hop undirected path over the 4 relation types plus `AFFECTED_BY`. The endpoint `b` must be a Company or an ExportControl. It returns `DISTINCT` source, relation, target, `status`, `evidence_quote`, and `evidence_chunk_ids`. There is no LIMIT.
2. **XBRL metrics** (`:86-91`): `REPORTS_METRIC` Metrics for the anchors, `ORDER BY period_end DESC LIMIT 20`.
3. **Active risks** (`:92-100`): `SEARCH` over `risk_embedding` with LIMIT 40. It is then filtered to anchor companies with `DISCLOSES_RISK {status:'Active'}` joined to `HAS_EVIDENCE` to `EvidenceSpan`. It returns the top 6 by score. Filtering happens after the top-40 search, so the result can be empty or short.
4. **Dropped lineages** (`:101-109`): `DISCLOSES_RISK {status:'Deleted'}` where `lineage_id IS NOT NULL`, grouped by lineage. It returns `min(first_seen)`, `max(last_seen)`, and one example summary. It is ordered by `last_seen DESC` and limited to 10. There is no vector search here.
5. **Scoped excerpts** (`:110-116`): `SEARCH` over `evidence_embedding` with LIMIT 60, then `MENTIONS→Company` with `cik IN $ids`, then `LIMIT $k`. The default `k_chunks` is 8.

**Vector-only baseline** (`vector_retrieve`, `:123`): the same evidence search with LIMIT k and no graph blocks.

## 4. Answerer (`retrieval/answerer.py`, `llm.py`, `artifacts/prompts/answer.txt`)

**Prompt**
- It is a single user message.
- The persona is a supply-chain analyst who may use ONLY the context provided.
- The rules are: cite after every factual sentence with `[chunk_id]`, say plainly if the answer is absent, and be concise.
- The sections are, in order: QUESTION, KNOWN RELATIONSHIPS, REPORTED METRICS (XBRL), DISCLOSED RISKS, DROPPED RISK LINEAGES, SOURCE EXCERPTS.
- Placeholders are `{question}`, `{edges_block}`, `{metrics_block}`, `{risks_block}`, `{temporal_block}`, `{chunks_block}` (`answerer.py:230`).
- Empty blocks render as `(none)`.

**Block formatting** (`build_blocks`, `:44-71`)
- Edges: `- src REL tgt (status=…) [id]`, using up to the first 3 chunk_ids.
- Metrics: formatted as USD with `{value:,.0f}`.
- Risks: `- company (category): summary [chunk_id]`.
- Temporal: `disclosed first..last, then dropped — e.g. example[:120]`.
- Chunks: `[chunk_id]\ntext`.
- It also returns `full_context`, the exact string seen by the model, which the evaluation judge uses.
- It also returns `valid_ids`, built from edge chunk_ids, risk chunk_ids, and excerpt chunk_ids.

**Citation format and verification**
- Format: `accession_no:section_id:0000`, for example `0001045810-25-000023:I.1A:0007`.
- Regex `CITE_RE` (`:41`): `\[([0-9\-]+:[IVX]+\.[0-9A-Z]+:[0-9]{4})\]`.
- Post-verification is `hallucinated = cited - valid_ids` (`:236`). It is reported only; nothing is regenerated or stripped.

**Non-streaming path**
- `llm_text` (`:74`) calls `litellm.completion(model, messages, max_tokens=1200 default, thinking={"type":"disabled"}, num_retries=2)`.
- It retries 4 times. Transient errors (`TRANSIENT` tuple in `llm.py:35`) back off 15, 60, 180, 300 seconds.
- An empty response triggers a retry.
- `finish_reason=="length"` triggers a regenerate with a doubled budget up to `MAX_BUDGET=8000`; on the last attempt the truncated text is returned.

**Streaming path**
- `TextStream` (`:132`) uses `stream=True` and `stream_options={"include_usage":True}`, with 2 attempts and a 5/15 second backoff.
- Retries happen only before the first delta; a mid-stream error raises `RuntimeError`.
- Usage comes from the provider chunk. If it is missing, it falls back to `litellm.stream_chunk_builder`, with `estimated: True`.
- `answer_stream` (`:240`) yields events `retrieval`, `delta`, `error` (with partial text, usage, and cost), and `done`.

**Token accounting**
- `usage_cost` (`:122`) computes cost from `llm_input_price_per_mtok=2.0` and `llm_output_price_per_mtok=10.0` (`config.py:46-47`).

**Model and rules**
- `llm_model = "anthropic/claude-sonnet-5"` (`config.py:21`).
- Rules: no `temperature`, `top_p`, or `top_k` is ever passed (a 400 on non-default values), and `thinking={"type":"disabled"}` is passed on every call (`answerer.py:99,160`; `llm.py:80`).
- The critic model is `settings.critic_model` (`extractor.py:206`), called with `thinking_off=False` and `max_tokens=600`.

**JSON repair** (`llm.py:66-127`, `llm_json`)
1. Up to 4 attempts.
2. Transient errors get the same backoff.
3. Empty content triggers a fresh retry.
4. Truncation (`finish_reason=="length"`) triggers a fresh retry with a doubled budget (`max_tokens=4000` default, capped at 8000).
5. A regex `_FENCE_RE` strips markdown fences.
6. The output is validated with `model_validate_json`.
7. On failure, `_salvage_json_object` extracts the first balanced `{…}` from the text.
8. If that also fails, a correction turn is sent with the invalid output in context.

**Extractor**: `extraction/extractor.py:192` calls `llm_json(prompt, ChunkExtraction, model=settings.llm_model)`.

## 5. Neo4j-dependent state outside the knowledge graph (`serve/store.py`, used from `serve/routes.py`)

- **`SvcPolicy {key, value, updated_at}`**
  - It holds the `kill_switch` flag.
  - `kill_switch_on` (`store.py:48`) is `env_flag or policy=="on"`.
  - It is read on the stats route (`routes.py:107`) and on the ask path after the cache lookup (`routes.py:152`).
  - It is set by an admin route (`routes.py:222-223`) via `MERGE`.
- **`SvcQuery {id(uuid), day, ip_hash, strategy, cached, prompt_tokens, completion_tokens, cost_usd, created_at}`**
  - One node is `CREATE`d per answered question.
  - It is written on a cache hit (`routes.py:148`, `cached=True`), after a paid answer (`:180`, with usage and cost), and on an answer failure (`:196`).
  - `paid_queries_today` (`store.py:54`) counts today's `cached:false` rows against `s.max_queries_per_day` (`routes.py:154`).
  - `ledger_summary` (`:70`) aggregates by day and all-time. It is cached by the stats endpoint (`routes.py:90-99`) and also used by the admin route (`:216`).
  - `ip_hash` is non-reversible, from `guard.py:60`.
- **`SvcAnswer {key, question, strategy, answer, citations, hallucinated, usage_prompt, usage_completion, cost_usd, source, created_at}`**
  - It is an answer cache.
  - The key is `sha256(strategy|normalized question)[:32]` (`store.py:23`), with the question lowercased, whitespace-collapsed, and trailing `?.! ` stripped.
  - `get_answer` (`:89`) applies a TTL check with `datetime()` and `duration`, except when `source='benchmark'`.
  - `put_answer` (`:98`) uses `MERGE`. It is called after a paid answer (`routes.py:184`) and by `seed_examples` (`store.py:111`), which is called at startup from `serve/main.py:62` with `source='benchmark'`.
  - Lookup happens at `routes.py:145`.
- Startup order (`main.py:60-62`): `apply_schema`, `ensure_indexes`, `seed_examples`.

## 6. Bitemporal model (`graph/temporal.py`)

**Producing the states**
- Initial load (`loaders.py:428-435`): every `DISCLOSES_RISK` edge is created with `status='Active'` and `start_date` set to the filing date. Nothing is closed at load time. `apply_closure` (`temporal.py:173`) does that later.
- `apply_closure` calls `normalize_categories`, then `fetch_annual_risks`, then `compute_temporal_states`, then `write_temporal_states`.
- **Category fix**: `normalize_categories` (`:126`) maps case variants onto `CANONICAL_CATEGORIES` (10 values) and title-cases anything unknown.
- **Input**: `fetch_annual_risks` (`:142`) reads risks whose evidence sits in a 10-K or 20-F filing and that have embeddings, along with `filing_date` and `accession_no`.
- **Clustering**: `cluster_lineages` (`:54`) is greedy per company. Risks are date-ordered. Each one joins the most similar centroid at or above `SAME_RISK_SIM = 0.75`, or starts a new lineage. Centroids are renormalized means. This is pure numpy, done in Python, not in the database.
- **State rule** (`compute_temporal_states`, `:78`): `first_seen` is the minimum filing date in the lineage and `last_seen` the maximum. A lineage is closed when `last_seen < latest_annual` (the company's latest annual filing date) and the company has more than 1 annual. Closed means `status='Deleted'` with `end_date = latest_annual`; otherwise `'Active'` with `end_date` null. `lineage_id = "{cik}:{index}"`.
- **Write** (`write_temporal_states`, `:156`): one `UNWIND` sets `rf.lineage_id`, `rf.first_seen`, `rf.last_seen` and `d.start_date` (backdated to `first_seen`), `d.status`, `d.end_date`. It then returns a `count(*)` grouped by status. All rows go in one call with no batching. It is idempotent.

**Queries that read the temporal properties**
- `retrieval/retriever.py:96`, which filters `{status:'Active'}`.
- `retriever.py:103-109`, which filters `{status:'Deleted'}` and reads `lineage_id`, `first_seen`, `last_seen`.
- `temporal.py:196-199` (`risks_active_as_of`), which uses `start_date <= date($asof) AND (end_date IS NULL OR end_date > date($asof))` and `count(DISTINCT rf.lineage_id)`.
- The composite relationship indexes in `schema.cypher:27-31`.
- `AFFECTED_BY` and the other relation edges carry `status='Active'` but nothing here closes them. The docstring at `loaders.py:415-417` says the notebook manages closure.

## 7. Loading (`graph/loaders.py`)

**Batching and idempotency**
- Every write goes through `_run_batched` (`:79`), which uses `BATCH_SIZE = 100` rows per `session.run` (`:37`). That is auto-commit, one transaction per batch, with no explicit transaction and no retry.
- Everything is `MERGE`-based, so re-runs are idempotent.
- The read side is pandas and parquet under `settings.data_dir`.

**Load order and MERGE keys**

| Step | Function | MERGE key |
|---|---|---|
| 1 | `load_companies` (`:137`) | Company `cik`, 14 rows |
| 2 | `load_filings_and_sections` (`:170`) | Filing `accession_no`, `FILED` edge; FilingSection `section_key`, `HAS_SECTION` edge. `MATCH`es Company by `ticker`. |
| 3 | `load_metrics` (`:216`) | Metric `metric_id`, edge `REPORTS_METRIC` |
| 4 | `load_evidence_spans` (`:332`) | EvidenceSpan `chunk_id`, with embedding, `FROM_SECTION`, and `MENTIONS` |
| 5 | `load_knowledge` (`:443`) | relation edges, RiskFactor, Product |
| 6 | `load_export_controls` (`:547`) | ExportControl `rule_id` |
| 7 | `link_affected_by` (`:581`) | `AFFECTED_BY` |
| 8 | `apply_closure` | temporal properties |

- Step 4 runs `load_ecosystem_companies` first (`:352`) so `MENTIONS` do not silently drop. The `MATCH` in step 4 uses `MATCH (c:Company {cik: eid})`.
- Step 4 scope (`_span_scope`, `:282`): the latest annual (all sections), the latest quarterly, and 1 prior annual (risk sections only, `HIST_ANNUALS=1`). NVDA is an exception and loads all chunks.
- Step 5 relations are one `MERGE` per `(src)-[type]->(tgt)`, using `_RELATION_CYPHER.format(rel_type=…)` (a relationship type in the query text via f-string). They accumulate `evidence_chunk_ids` and keep the earliest `start_date`. A relation is merged for each of the 4 types, unresolved entities go to a parquet report, and `risk_id = sha1(chunk_id|summary)[:16]`. Existing `risk_id`s are read first, so only new risks are embedded.
- Step 5 also resolves entity names with a normalized alias lookup and a `SequenceMatcher` fuzzy match at threshold 0.90 (`:401`).
- Embeddings are `v.tolist()` float lists (1024) sent as `embedding` node properties, in 100-row batches.
- Embeddings are cached in per-ticker parquet files (`data/processed/embeddings/{TICKER}_chunk_embeddings.parquet`), so re-embedding is not needed.

**Data sources for a re-load**
- `data/raw/edgar/company_tickers.json` and `manifest_universe.json`.
- `data/interim/section_texts/*.parquet`.
- `data/processed/chunks/*.parquet`.
- `data/processed/extractions/*.jsonl`.
- `data/processed/xbrl/*_key_metrics.parquet`.
- `data/raw/federal_register_bis_rules.json`.

**Cost of a full reload**
- EvidenceSpan rows are the largest, at 100 rows per call.
- The number of spans, risks, and relations per ticker is not stated in code. Corpus counts live in the project memory notes (`graphrag-data-findings.md`), which I did not open.

**Coupling worth noting**
- The `neo4j.Driver` type is imported in `graph/loaders.py`, `graph/schema.py`, `graph/client.py`, `serve/store.py`, and `retrieval/*`. `graph/client.py:17` (`get_driver`) connects via `bolt://`.
- Every `driver.session().run` uses Cypher strings directly, with no abstraction layer.
