# semigraph v2 — plan (revision 3)

Date: 2026-09-25. Supersedes revisions 1–2. Evidence: 14 discovery reports in [`research/`](research/) (code inventories + live-web research, all dated 2026-09-25), my own probes (Federal Register recall bug, Neo4j filtered search and full-text on a live server, saved-run statistics), an independent Fable architecture review and an Opus advisor pass. Labels: **VERIFIED** = seen live/measured; **UNVERIFIED** = reported but not confirmed.

## 0. Decisions (all by the owner)

| Topic | Decision |
|---|---|
| Graph store | **Stay on Neo4j Community.** FalkorDB evaluated and rejected (saves no money, SSPL, no in-index filtering; Neo4j has native filtered vector search). |
| Observability / stack | LangChain 1.x + LangGraph 1.x + **Langfuse** ("langfloe" = Langfuse) + DeepEval; Ragas optional and pinned. |
| Scale target | **1,000 concurrent users**, provable by a load test (10,000 dropped). |
| Frontend | Next.js static + Vercel AI SDK on Cloudflare Pages. |
| LLMs | Any vendor; open-weight models only via **US hosts**. GPT-6 Luna (released 2026-09-22) and the newest Haiku are eligible bake-off candidates. Cheap default + escalate. |
| Audience | **A demo to a potential buyer**: best-of-best capabilities, including document upload (with updated versions) and explicit chunk freshness/staleness handling. |
| Budget | API spend ≤ ~$30; stop and ask before any step > $1. Then: "finalize, then go for M1" (this document is the finalization). |

Working rules: `v1-final` tagged, work on branch `v2`, v1 stays deployable until a milestone clears its gates; TDD, ≥ 80% coverage, reviewers per repo rules, Opus `verifier` on every phase diff, live probe before shipping any API call shape.

## 1. Verified findings that shape the plan

**Data (first ask).** 10 new filings since v1's 2026-07-03 pull: 9 × 10-Q (NVDA 08-26, AMD 08-05, INTC 07-24, AVGO 09-10, QCOM 07-29, AAPL 07-31, AMZN 07-31, GOOGL 07-23, META 07-30) + **MSFT FY26 10-K (07-29)**; none for MU (10-K due ~Oct), TSM, ASML. ≈ 630 chunks (+13%), extraction ≈ $2–4 on Sonnet 5 at $2/$10. v1 ingestion could not pull them (per-ticker manifest skip, per-ticker chunk skip, caches that never refresh, string-sorted "latest 10-Q"). **v1's Federal Register query has a recall bug** (OR acts as AND: 14 hits vs 43/24/79; v1 stores 13 of ~166 BIS rules, latest 2025-09-16) — reproduced; regulatory answers will change. **TSM FY2025** XBRL facts are absent from the Company Facts API. **Non-USD metrics (TSM TWD, ASML EUR) print as "USD"** (`answerer.py:55`, retriever omits `m.unit`) — confirmed in code.

**Freshness reality (VERIFIED).** The corpus is already behind EDGAR (10 of 13 filers). **AMD's 10-K/A (0000002488-26-000021)** corrects transposed MD&A Client-revenue figures (units +15% / ASP +31%, not the reverse); the manifest lists it but it was never chunked and `_span_scope` filters `form == '10-K'`, so the graph serves the wrong figures with no staleness signal — a real demo case. XBRL restatements are dropped (`curate_metrics` keeps earliest `filed`). Benchmark answers never expire (`store.get_answer`). Submissions API has no ETag (full 200s); `usgaap.rss.xml` and daily-index support conditional GETs; 10 req/s fair-access limit.

**Retrieval (measured on v1's saved runs).** Excerpts = exactly 8 chunks, ACTIVE RISKS = 6/6 in all 18 runs; the relationship block averages **≈ 78 edge lines (19–88), uncapped**, and dominates the ≈ 40k-char context → capping/ranking edges is the cheap cost+precision lever. `DEFAULT_ANCHOR_CIK` (Nvidia) silently anchors unanchored questions (only benchmark R2 relies on it).

**Neo4j (probed live on 2026.05 Desktop Enterprise + docs).** Filtered vector search works in-index: index declared `WITH [props]`; predicates `= < > <= >=` joined by AND on boolean/int/string/date; **`IN` needs 2026.06 (rejected on 2026.05)**; no OR/NOT/`<>`/IS NULL ⇒ use booleans and date sentinels; a later `SET is_current=false` is honoured; filtered search is GA in Community since 2026.02 (docs) — the `WHERE` form on Community 2026.07.1 still needs a first-day probe. Full-text is Lucene **BM25** (verified by execution of Neo4j's classes): `3A090`, `H200`, `CoWoS`, `HBM3E` survive as tokens; hyphenated IDs (accession, FR number, `10-K/A`) split into pieces ⇒ **quote them as phrases**; `remove_lucene_chars` in langchain-neo4j destroys IDs; its hybrid is max-normalised, not RRF, and rejects filters ⇒ **RRF in Python over two Cypher calls**. Community: one user database (no `CREATE DATABASE`), uniqueness constraints only, aligned format only, no read-only role, dump/load offline. **Dev/prod skew:** local Desktop is 2026.05 Enterprise; production is `neo4j:2026.07.1-community` (2026.09.0 exists; 07.0 and 08.0 regressed). Neo4j is not the 1,000-user bottleneck (engine p95 ≈ 1–2 ms in a synthetic test; 5 small reads per live question). Full rebuild = generation **dump-swap**, not in-place; the seed-restore script overwrites uploads and the ledger, so state must live elsewhere.

**LLM / cost (as reported 2026-09-25; re-verify at bake-off).** Per live answer (16k in / 1.5k out): Sonnet 5 ≈ $0.047; candidates ≈ $0.002–0.018. Prompt caching ≈ $0 on our answer path. Free tiers are dev-only. Published quality evidence is weak for our use ⇒ our gold set is the evidence. Judge choice outweighs model choice (Sonnet→Haiku judge moved hybrid correctness 1.00 → 0.90 on identical answers; 13 of 20 questions are mechanical, 7 judge-dependent). **Haiku 4.5 (extraction critic + judges): "retirement not sooner than 2026-10-15".**

**Stack (import-tested together on Python 3.13).** langchain 1.4.2, langgraph 1.2.12, langfuse 4.15.6, litellm 1.102.1 (1.82.7/8 were compromised — pin ≥ 1.83, hash-locked), langchain-litellm 0.9.0, fastapi 0.141.1, deepeval 4.2.6; **Ragas 0.4.3 fails to import** (workaround `langchain-community==0.4.1`); Phoenix Elastic-2.0; LangSmith closed; TruLens ≈ 3–4× judge cost ⇒ DeepEval + Langfuse + our domain metrics. LangGraph Agent Server needs a license key ⇒ host the MIT graph in our FastAPI. `langchain-neo4j` has no async and a weak hybrid ⇒ own thin async Cypher layer as LangGraph tools; no public text-to-Cypher.

**Scale (1,000 users).** Little's law with think time 120–300 s and streams 15–20 s ⇒ ≈ 50–140 concurrent streams and ≈ 3–7 new questions/s (burst ×1.5); most traffic is static/cached. One fast core held ≈ 450–500 mock streams (optimistic; budget 150–200 per vCPU) ⇒ 2–3 API machines plus embedder/rerank cost (measure). LLM capacity at this scale fits standard paid tiers. v1 blockers stay: thread-per-stream sync generator, `max_concurrent_answers=2`, process-local limiter, Neo4j read/write per request for ledger/cache/kill switch; behind Cloudflare `fly-client-ip` becomes the edge IP.

**Uploads (research, VERIFIED unless noted).** Parser: **Docling 2.130.0 (MIT)** primary (1.18 s/page warm, 30/30 tables recovered, ≈ 1 GB steady / 2.1 GB peak, ≈ 500 MB models, does not fit the 2 GB API machine → separate parse app), MarkItDown fallback; **exclude PyMuPDF (AGPL), marker (weights license), pypdf for untrusted input (10 advisories)**. Community has no multi-tenancy ⇒ app-level isolation via separate labels (`User*`) + mandatory `workspace_id` through one repository, own vector index, CI leak-test harness; RQ + Valkey queue; R2 transient storage with lifecycle; prompt-injection defences (untrusted-content block, no tool-calls from upload text, strip image markdown, "user-provided" labelling).

## 2. Target shape

```
Cloudflare Pages (static Next.js UI) ─► Cloudflare edge (Turnstile/Access, WAF, origin-auth header)
      │ SSE (AI SDK UI-message-stream encoded by our FastAPI)
      ▼
Fly.io: N stateless async FastAPI machines
  ├─ LangGraph agent: router → retrieve | tool-loop → structured draft → verify → release
  ├─ LiteLLM Router (cheap default → strong escalation; ONE fallback layer)
  ├─ Valkey: limits, exact + query-embedding cache, atomic admission/spend, kill switch, RQ broker
  ├─ Neo4j Community (public corpus, generation dump-swap) + User* labels for uploads (app-level isolation)
  ├─ Langfuse (sampled traces/cost)
  └─ semigraph-parse app (Docling, on-demand) → ingest stage → Neo4j
Offline pipeline (semigraph CLI): freshness check → incremental ingest(--as-of) → segment → chunk(append-only) → extract → resolve → embed → versioned full rebuild → closure → dump
```

## 3. Search architecture (six types, Neo4j-native)

| Type | Implementation |
|---|---|
| Vector | Qwen3-Embedding-0.6B (ONNX q8 in service); Neo4j vector indexes with filter properties; quantization pinned `none`, `m` 32, `ef_construction` 200. |
| Keyword / BM25 | Neo4j full-text (Lucene BM25, `standard-no-stop-words`); **identifiers quoted as phrases**; user text escaped; second `keyword`-analyzer index for id lists; freshness/company as indexed strings in the Lucene query. |
| Hybrid | **RRF (k=60) in Python** over the vector and full-text Cypher calls (unit-testable; leaves a slot for rerank). |
| Filtered | In-index `SEARCH … WHERE` on `is_current / retrievable / filer_cik / form / valid_from / valid_to`; one query per anchor (no `IN` on 2026.05); `MENTIONS` (multi-valued) filtered after search. |
| Rerank | Cross-encoder over the top ~20; hosted (Cloudflare Workers AI bge-reranker-base ≈ $0.00005/query; Cohere) with local MiniLM-L6 as dev fallback; bge-v2-m3 on CPU rejected (1.09 s/pair). Adaptive k 3–8. |
| Graph | Typed 2-hop traversal + XBRL metrics + bitemporal lineages + export-control edges as agent tools; **ranked and capped** (edge block ≤ ~20 lines, hop-1 first, ExportControl only at hop 1, unit-aware metrics). |

One batched ablation (vector → +BM25 → +RRF → +filter → +rerank → +graph caps) on the grown benchmark. Not adopted unless it shows a gap: HyDE, ColBERT, community-summary GraphRAG.

## 4. Freshness and versioning model (buyer-demo core)

Two axes kept apart: **document-version status** (this section, `versions.py`) vs **fact validity** (`temporal.py`: Active/Deleted lineages).

- **Filing versions (deterministic, no LLM).** Per company: a newer period *rolls* the older forward (older stays valid for its own period); a **parsed amendment (10-K/A) corrects its original** as the period's effective filing (AMD case); an unparsed amendment is inert; a new annual retires earlier quarterlies; a newer quarterly retires the previous one. Order = `(filing_date, accession_no)` (`acceptanceDateTime` later).
- **Properties (M1 rebuild):** `Filing{status,supersede_kind,superseded_by,is_current,snapshot_id}` + `(:Filing)-[:SUPERSEDES]->(:Filing)`; `EvidenceSpan{content_hash,filer_cik,form,accession_no,filing_date,valid_from,valid_to(sentinel 9999-12-31),is_current,retrievable,status,source_type,snapshot_id}`; `RiskFactor{filer_cik,is_current,…}`; relation edges `has_current_evidence`, `last_evidenced_at`; `ExportControl{kind,topics,relevant}`; `Snapshot{id,as_of,…}`. `retrievable` keeps historical annual risk text retrievable by default while dropping superseded quarterlies and corrected originals.
- **Content hash spec (frozen):** sha256 of NFKC-normalised, whitespace-collapsed, stripped text (case preserved). Caches become hash-keyed for uploads (M4); chunk ids stay positional so citations and cached benchmark answers stay valid.
- **Retrieval:** default `retrievable/is_current`; as-of via `valid_from/valid_to`; history/trend questions drop the currency filter; recency prior only for "latest" intents and only as a small ranking feature (LLM rerankers over-favour recency — keep it out of the LLM).
- **Answer + UI:** prompt receives `data_as_of` and per-item form/date/status (M2); stale-citation post-check mirrors the hallucinated-id check; per-citation badge ("Current (verified <date>)", red "Corrected by 10-K/A …", grey "Prior period"); header "Data as of … · checked … · N filings pending"; "possible late filing" using SEC deadlines; feed-stale banner.
- **Cache invalidation:** key = (normalized question, `snapshot_id`, model set, prompt version, retrieval flags, web on/off); remove the benchmark TTL exemption; bump snapshot on promotion.
- **Monitor (M4):** in-app scheduler on the warm API machine guarded by a lease + admin "Check now" + external GitHub Actions heartbeat; on a hit: pending event → fetch/parse → hash-diff → embed/extract only changed chunks → gates → promote in one transaction → bump snapshot → invalidate cache. EDGAR via `usgaap.rss.xml` (ETag) + submissions; Federal Register by `publication_date` watermark (−3 days), corrections are separate documents.
- **Tests:** two real snapshots (A = current dump, B = + AMD 10-K/A, MSFT FY26 10-K, Q2 10-Qs, FR correction): AMD Client-revenue answer flips; NVDA "latest quarter" flips while as-of 2026-06-30 still returns Q1; MSFT latest annual flips while as-of 2026-06-01 returns FY25; invariants (≤ 1 current filing per company/family; no current span under a non-current filing); filtered search excludes spans after `SET is_current=false` and top-k is not diluted by superseded near-duplicates; re-polling changes nothing and does not bump the snapshot; stale-citation rate 0 for current-intent questions.

## 5. Documents (upload) — design

Bring-your-own documents with **updated versions**, safe by construction:
- Isolation (Community has one database): separate labels `UserDocument/UserVersion/UserChunk/UserRisk`, mandatory `workspace_id`, one `WorkspaceRepo`, own vector index `WITH [workspace_id, is_current]`; workspace token = 128-bit random (sha256 stored), TTL sweeper; uploads never create edges between canonical companies; public queries cannot see uploads by construction; separate citation grammar `U:{ws}:{doc}:v{n}:{seq}`; **CI leak-test harness** across retriever, cache and evidence routes. Answer-cache key includes workspace + data version.
- Parse in a **separate on-demand app** (Docling, CPU torch, OCR off, baked models, `allowed_formats` = PDF/DOCX/HTML/MD, page/size caps; MarkItDown fallback; scanned PDFs rejected clearly) → normalized JSON blocks → trusted ingest stage. Job states `queued→parsing→chunking→embedding→[extracting]→indexing→ready` streamed by SSE; RQ + Valkey.
- Upload gate: content-type by magic bytes, streaming size cap, zip/page limits, reject PDF active content, uuid storage names, quotas, Turnstile required; transient originals in R2 with 1-day lifecycle; ClamAV optional/off.
- **Updated versions:** explicit "new version of X" (auto-suggest, never silent merge); identical hash = no-op; diff at heading-section level by normalized-block hashes (+ fuzzy match to tell *modified* from added+removed); only changed sections re-embedded/re-extracted; old chunks superseded, never deleted; `what-changed` report reusing our lineage clustering (new / dropped / changed risks; same-day versions use a version ordinal); default extraction-free (chunk+embed) with an opt-in, cost-previewed deep mode.
- Prompt-injection defences: untrusted block with random delimiters, no tool calls driven by upload text, strip image markdown/non-allowlisted links, "user-provided, not filing evidence" label.

## 6. Milestones and gates

**M1 — v1.1: fresh data, correctness, freshness model, cheaper answerer (in progress; ≈ $15–19 API).**
Done/underway: foundation refactor (`universe.py`, `snapshot.py`, `versions.py`) — committed; workers: **A** ingestion (incremental per accession, FR recall fix + rule classification, XBRL refresh/units/TSM, freshness checker), **B** (append-only chunker, scope by filing_date, settings-based cost estimate, per-chunk embedding cache) — done, **C1** graph model (versioned properties, filtered/fulltext indexes, loaders load every extracted chunk, deterministic lineages, Snapshot, `neo4j_database` wrapper), **C2** retrieval fixes (units, relation-query explosion, filtered Active-risks/excerpts, honest default anchor). Next: **D** segmentation of 10-K/A + extraction scope through `versions.py`; CLI wiring (`ingest --as-of --refresh`, `freshness`, `build-graph --rebuild`); real data pull to 2026-09-25; delta extraction (estimate → your approval); rebuild into a local `sg…` database; before/after report of every answer-relevant fact; then **P5-lite**: judge calibration (your ~30–40 hand labels), bake-off on v1's fixed path via model swap, ship cheap-default + escalate, redeploy v1 on Neo4j.
**Gates:** latest filing per ticker == live submissions API (≤ as-of); counts derived from data; chunk-id stability; FR count == union; every changed fact reported; 162 + new tests green; Neo4j filtered-search/`WHERE` probed on **Community 2026.07.1** (needs the Community tarball — permission requested below). **Model pass gate:** 100% on the 13 mechanical questions + citation-id validity 100% + calibrated-judge correctness ≥ baseline − 0.05 on the 7 open ones + **escalation rate ≤ 15%** + non-empty answers; per-stratum reporting.

**M2 — Search upgrade (M).** Full-text BM25 (quoted IDs), RRF in Python, in-index filters, rerank + adaptive k, edge cap/rank, freshness labels in the prompt + stale-citation check, block token instrumentation; one batched ablation on the grown benchmark (20 → ~60–100 graph-verified questions).

**M3 — Agent + evals (L).** LangGraph (deterministic router first; fixed path; bounded tool loop; structured claims `{text, cite_ids}`; deterministic verifier; single fallback layer via LiteLLM Router; buffer-verify-release with live step timeline), read-only async Cypher tools, Langfuse, AI SDK stream encoder + contract test, web-search tool (Tavily, ≤ 2 calls, labelled non-filing, injection-hardened, off in benchmark); DeepEval T0 (every PR, $0) / T1 (rescore saved runs) / T2 (weekly Langfuse experiment); live probes per model.

**M4 — Freshness + documents (L).** Poller + admin "Check now" + dashboard + banners + badges; upload workspace (§5) with version diff and what-changed report; risk-change delta and company dossier data.

**M5 — Serving + frontend + buyer features (L, parallel).** Async serving; Valkey admission (atomic reserve/reconcile) and limits; `CF-Connecting-IP` + origin-auth; ledger/cache/kill switch out of Neo4j; retrieval-only degrade; Locust load test to **1,000 VUs** (ramp ≥ 10 min, spike, soak, injected 429/slow; pass: TTFB(first step event) p95 ≤ 1.5 s, ≥ 150 streams held / 0 drops, errors < 0.5%, CPU ≤ 70% incl. embedder/rerank). Frontend: Home, Answer (step timeline, `[n]` chips → source drawer, freshness badges, "N risks dropped"), dossier + what-changed, supply-chain map (React Flow + table toggle; centrality precomputed offline via GDS Community, labelled "as-disclosed"), export to PDF/Markdown with footnoted citations, Freshness page, Method/Limits page; conversation history; API + OpenAPI + hashed keys; read-only MCP server (FastMCP) for the public corpus; Cloudflare Access invite gate + roles + tamper-evident audit log; admin console (cost/answer, routing, eval scores, Langfuse link). Lighthouse a11y ≥ 95.

**M6 — Deploy, demo, cutover (M).** Fly apps, Cloudflare, secrets scripts, generation dump-swap runbook (built in a Community container at prod version so dumps are natively aligned), RUNBOOK/ADR/README, demo script + recorded fallback, CI (unit + T0 + pip-audit + gitleaks). Cut over only after every gate.

## 7. Buyer demo

Ranked features (impact/effort in `research/research-buyer-demo.md`): cited agent with visible steps; freshness dashboard; eval-transparency + Limits page; risk-change delta; company dossier; export; upload + version diff; MCP; API keys; auth/audit; watchlist alerts (stretch); GDS centrality (stretch, precomputed). **Positioning:** not breadth (vendors are orders of magnitude larger) — provenance on every edge, lineage-level "what changed" beyond a text redline, and published evals with cross-judge deltas. **Honest disclaimers (exact text in the research):** informational research tool, not advice; AI-generated from a dated snapshot; coverage stated; "citation check confirms retrieval, not entailment"; **not SOC 2/ISO certified** (Fly hosting SOC 2 Type II is a hosting fact only); uploads private and deletable. **Storyline (7 min):** trust opener with freshness banner → multi-hop question with citation click-through → risk delta (script on NVDA or AMD; show both texts + similarity because greedy clustering can false-split rewordings) → **updated version demo (AMD 10-K/A correction or a live upload of v2)** → export + MCP → eval/limits/cost close; fallbacks: pre-cached answers, kill switch, recording.

## 8. Budget (API ≤ $30; stop-and-ask > $1)

Delta extraction ≈ $2–4 · judge calibration ≤ $1 · bake-off (hybrid-only candidates, mechanical first, cheap judge) $8–10 · retrieval ablation ≈ $3 · agent evals ≈ $2 · headroom ≥ $5. A Sonnet-judged six-candidate bake-off (~$34) is excluded. Hosting separate (~$17/mo online today; scale layout ≈ $25–30/mo idle, estimate). Uploads: extraction-free by default; deep mode ≈ $0.01 per page on today's model, per-workspace token cap.

## 9. What v2 cannot prove

A mock-LLM load test proves the app tier, Neo4j reads and the degrade path — not provider capacity, real TTFT under load, true cache-hit rate, or cheap-model quality at scale. 1,000 users = static UI + cached/starter answers + admission-controlled live answers; the daily ceiling and kill switch stay on. Cross-model differences < ~0.05 on the gold set are noise; graph-generated questions are partly circular. Neo4j Community has no HA, RBAC or online backup; app-level isolation is logical, not physical. Delta risk detection can false-split reworded risks. No compliance certification is claimed.

## 10. Skills / MCP (recommendations only; install after approval, user scope, pinned, `-g --copy`, read-only)

LangChain docs+reference MCP; `langchain-skills` subset; Langfuse skill + docs MCP; DeepEval skill; Anthropic `frontend-design`; shadcn skill; Docling docs via Context7. Rejected: FalkorDB skills/MCP (FalkorDB dropped), mcpdoc (archived), lca-skills (no license), community Langfuse forks, Confident AI/LangSmith MCPs, Fly MCP, EDGAR community MCPs. MCPs that read traces feed attacker-controllable text to Claude Code ⇒ read-only/allowlisted.

## 11. Verification

`pytest` (≥ 80% coverage; LLM mocked) incl. opt-in Neo4j integration tests on a scratch database; two-snapshot freshness tests; chunk-id stability; parity/ablation; T0/T1/T2 evals; Locust pass criteria; upload leak harness; browser E2E + Lighthouse; `security-reviewer` (serve/auth/upload/CF-IP), `python-reviewer`, `code-reviewer`; Opus `verifier` per phase; pip-audit + gitleaks.

## 12. Open items (owner actions)

1. **Approve a download:** Neo4j Community **2026.07.1 tarball** (≈ 150–250 MB, from neo4j.com/download-center) to run production-equal locally (Community has no `CREATE DATABASE`; local Desktop is 2026.05 Enterprise; dumps must be natively aligned). I will not download without your explicit yes.
2. **API keys** for the bake-off in `.env` (never paste them in chat): `.env` currently holds only `ANTHROPIC_API_KEY`. Needed: OpenAI (GPT-6 Luna), Google AI Studio (Gemini), and one US open-weight host (DeepInfra / Fireworks / Together) or OpenRouter with US-only provider routing.
3. **~30–40 hand labels** on judge-dependent answers (I generate the review sheet) before judge calibration.
4. **LICENSE** for our code (Apache-2.0 or MIT) before public cutover.
5. Approval of the delta-extraction estimate when the CLI prints it.

## 13. Risks

Filtered-search `WHERE` and full-text behaviour on Community 2026.07.1 unverified until the tarball probe; dev/prod version skew; brand-new models (Luna 3 days old) and Haiku retirement; escalation rate above the ceiling would erase the cost win; AI SDK protocol churn (pin the major + contract test); Ragas breakage; LiteLLM supply chain; prompt injection via filings, web search, uploads and traces; upload parser CVE surface (pin ≥ fixed versions); Docling does not fit the API machine; Langfuse Hobby unit budget (sample); false-split in risk delta; single-writer Neo4j makes uploads/ledger a replication problem (kept out of scope by using Valkey and one writer).

## 14. Audit trail

Rev 3 changes: FalkorDB and 10k-user goals dropped by decision; added the freshness/versioning core, upload design, buyer-demo scope, Neo4j-native search (probed), Community constraints and dump-swap rebuilds, AMD 10-K/A case, effective-annual/quarterly supersession module, `retrievable` vs `is_current` split, IN-free filtered queries for 2026.05 compatibility, generation-based deployment. Earlier corrections retained: the "ANN post-filter shortfall" does not occur in v1's saved runs; the AI SDK "three new majors" was a wording error (three maintained lines were patched 2026-09-23/24).
