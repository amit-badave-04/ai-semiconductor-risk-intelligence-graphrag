# M2 triage, recall comparison, company pipeline and internet search (2026-11-11)

INTERNAL document: it states limits plainly. Do not copy any of it into the README, the site or any public text.

Sources: two read-only workflows (wf_30bb21ed-3dc and wf_7e1bc0bf-c68). Each had three investigators (Opus), a synthesis
(Opus) and an adversarial fact-check (Opus) that re-opened every file:line and re-checked web claims on 2026-11-11. This file
is the synthesis **with the fact-check's corrections applied**. No live paid question, no bot check, no production write, no
secret read.

## 1. Triage of the named defects on live v23

Note on method: every one of these questions is a seeded saved answer on v23 (`source=benchmark`, no TTL, store.py:227-233),
so a "re-run" replays stored bytes and runs no retrieval. Root causes were rebuilt from the current query code, live
`/api/evidence` metadata and the older stored contexts in `data/processed/eval_runs.v2-baseline.jsonl`.

| Defect | Still on v23? | Root cause | Would M2 search fix it? | Fix outside M2 |
|---|---|---|---|---|
| D1 Nvidia suppliers: "does not specify manufacturing vs assembly" | Yes, byte-identical | **Retrieval scope.** The excerpt query keeps a passage only if it MENTIONS an anchor company (retriever.py:302-306). Passage 0320 says "we" and mentions [Foxconn, SK Hynix, Micron, TSMC, Samsung], not Nvidia, so it can never be an excerpt; it reached the prompt only as an id on edge lines, which print ids, not text (answerer.py:220-224). The model's sentence was true of what it was given. This **contradicts** decision 004's premise ("retrieved and cited, then misread"). | Only via a new **filer-scoped** search leg (`node.filer_cik = $cik`, legal in SEARCH WHERE), fused with the mention leg; BM25/RRF/rerank alone do nothing while the MENTIONS hop stays. | Print the edge's evidence quote beside its ids; make `citations_retrieved` tell "text in prompt" from "id only"; optional: Wistron and Fabrinet as entities, a role on supplier edges |
| D4 "Meta is an AMD customer" cites a superseded 10-Q chunk | Partly: the stale citation reproduces; Meta **is** in the passage (char 2,754 of 4,119) | **Graph provenance.** The edge's evidence ids list the superseded chunk before the current one; the edge line prints the edge status but no per-chunk currency mark, and the model cited the first id | No (the citation comes from an edge line) | Current-first edge ids; a stale-citation post-check on the SEC path (one exists for uploads, workspace.py:250). D4 is the evidence decision 004 asked for before un-deferring it |
| X4 NVIDIA and the Jan-2026 BIS rule: "provisions not provided" | Yes | **Two parts.** (a) Context builder: the rule query returns no abstract, so the rule line has date, id, title and link method only (answerer.py:198-208); the abstract the chip shows never reached the model. (b) Retrieval scope, as D1: gold passages 0323 and 0355 have no mentions (inferred; v2e stores no excerpt list) | (b) partly, via the filer-scoped leg (+ full-text "license", unmeasured); (a) no | Print a truncated rule abstract |
| NG06 / NG10 percentage cites only the later year | Yes; figures correct | **Context format.** The computed year-over-year line carries only the later fact's id (answerer.py:111-133, 176-185); the prompt does not ask to cite the base year | No | Put the prior fact's id on the computed line; prompt rule |
| Apple CUSTOMER_OF Alphabet; Huawei CUSTOMER_OF Qualcomm "Active" over a revoked-licence passage | Partly: v23 pages hide relation names; the edges still reach the answer prompt | **Extraction** (direction/type and tense wrong) | No | Re-adjudicate edges (direction and tense check), negative cases in the extraction eval |
| Rule exposure (ASML, GOOGL, AMZN, META show none; INTC and MU lack the affiliates rule) | Partly: v23 says "No export-control rule in our data was linked ... by keyword matching. This is not a finding that none applies." | **Linker (load time):** keyword match on risk categories containing "export control" with `is_current` (loaders.py:731-733), capped at 15 rules per company (export_rules.py:13). INTC's missing affiliates rule would rank inside its newest 8 if linked, so it is a linker miss, not the display cap | No | Normalise categories, keywords for equipment makers, store "examined, matched 0 of N", show "newest 8 of N" |
| "Numbers not checked" badge | Partly: the visible "What was checked" is accurate; the collapsed Technical details still shows the badge | **UI/schema:** seeded answers store no checks, and the page reads missing checks as "numbers not checked" (answerMeta.ts:107). Genuinely false only for N2, NG06 and NG10 (numbers_checked 1); for D1, D4 and X4 it should read "no figures to check" | No | Treat `source='benchmark'` as "checked when saved" (frontend, fast path), or persist checks (DB, full path) |

**Tally:** retrieval scope 2 (D1, X4b); context builder 2 (X4a, NG06/NG10); provenance 1 (D4); extraction 2 (Apple/Alphabet,
rule linker); UI 1 (badge). None is "the model misread a passage it was given".

**Shipping blocker for any fix:** the answer cache key is `prompt fingerprint | snapshot id | strategy | question`
(store.py:32-39). Fixes in retrieval or context code (no graph rebuild) do not change the key, so the seeded D1, D4 and X4
answers keep replaying until a new paid `semigraph eval` run regenerates examples (`build_examples.py` itself makes no LLM
call). Fixes that rebuild the graph (edge order, linker, re-adjudication, new entities) change the snapshot id, and then every
example is un-listed until regenerated (a refused example is not listed; it does not become a bot-check click).

## 2. Recall today vs M2's proposals (PLAN.md:97)

| M2 item | Today | Standard option | Recommendation |
|---|---|---|---|
| BM25 full-text (quoted ids) | Full-text indexes `evidence_text_ft` and `risk_summary_ft` exist (schema.cypher:53-57) but **no retrieval code queries them** (only a test does). Lucene's default scoring is BM25 (docs/v2/research/research-neo4j.md:62; k1/b not tunable, IDF counts superseded chunks) | Neo4j native full-text (Community). On 2026.07 via `db.index.fulltext.queryNodes` (deprecated in 2026.10, not removed); `SEARCH ... FULLTEXT` needs 2026.09+; no in-index WHERE either way. bm25s / tantivy (MIT) would duplicate index state | **Upgrade: use the existing index**, over-fetch and post-filter, a Lucene-escaping/quoting helper. No new library |
| Fusion / RRF | **Absent.** "Hybrid" = separate prompt blocks; nothing fuses ranked lists | RRF k=60 is standard practice. Neo4j's guide shows weighted RRF in Cypher, including a procedure-based form that runs on 2026.07 (but loses the vector leg's in-index WHERE) | **Add ~15 lines of Python RRF** fusing: mention-scoped vector, **filer-scoped vector (the D1/X4 fix)**, full-text. Compare with a linear variant offline |
| In-index filters | Partial: excerpts filter `retrievable` in-index (60 candidates) then post-filter by MENTIONS (the D1/X4 root cause); risks filter `filer_cik` and `is_current` per anchor. `form`, `valid_from`, `valid_to` are declared on the index (schema.cypher:45-47) but unused; no as-of filter on the SEC path | Native `SEARCH ... WHERE` (GA 2026.02; `IN` from 2026.06, unsupported on local Desktop 2026.05) | **Upgrade:** add the filer-scoped leg (one search per anchor, `=` only) |
| Rerank | **Absent** (token-overlap ranking for temporal items only) | Torch-free ONNX cross-encoders (Apache): gte-reranker-modernbert-base (150M, official int8 ONNX), mxbai-rerank-xsmall-v1 (71M), ms-marco-MiniLM-L6-v2 (23M); onnxruntime and tokenizers are already dependencies. Qwen3-Reranker-0.6B has no official ONNX. CPU latency/RAM unmeasured | **Defer.** Spike only if the ablation shows a ranking gap (D1/X4 are scope gaps) |
| Adaptive k | k fixed at 8 | No standard package; tuned from recall@k curves | **Drop for now** |
| Edge cap / rank | Company edges: no LIMIT, no ORDER, every status; rules newest 8 per company; neighbour rules off ("M2 re-adds them behind a relevance ranking") | Plain Cypher ORDER BY / LIMIT | Small; only if token measurement shows bloat |
| Freshness labels + stale-citation check | Weak on the SEC path (stale check exists only for uploads) | Reuse the project's own upload check | **Un-defer, ahead of search work** (D4) |
| Block token instrumentation | Only total `context_chars` | Trace attributes | Keep, minimal (per-block character counts) |
| Benchmark growth + ablation | 60 questions; 24 have xbrl gold ids (`metric_ids`), several temporal questions carry chunk ids in `facts`, some ids sit in free-text `judge_notes` (X4's 0323/0355/0368 included). **No structured EvidenceSpan/fr retrieval-gold field.** Retrieval is measured only by LLM-judged context recall (v2-baseline: hybrid 0.745, vector 0.657). `scripts/compare_retrieval.py` stores order-free id sets only (no ranks, scores or pre-filter candidates) | ir-measures 0.4.3 (Apache) over pytrec-eval-terrier (MIT) for recall@k / nDCG; ranx 0.3.21 (MIT, heavy deps, slowing) | **Do first:** structure the existing ids and add missing gold; extend compare_retrieval to keep ranks; score with ir-measures (eval extra only) |

**Replace or upgrade?** Upgrade in place (option b): native full-text + filtered vector + a filer-scoped leg + small Python
RRF; no new runtime dependency; graph, temporal layer, citations and checks unchanged. **Do not replace with neo4j-graphrag**
(1.24.0, Apache-2.0, released 2026-11-10): its hybrid retrievers reject filters (`neo4j_queries.py:494`), fuse only NAIVE or
LINEAR (no RRF), cap the full-text leg at top_k, cannot express the graph/temporal layers, would add pypdf and json-repair and
force a neo4j driver bump (needs >= 6.3.1; serve pins 6.3.0). The more valuable "standard" adoption is in **evaluation**
(ir-measures recall@k/nDCG against gold ids).

## 3. M2 sub-steps (full path; time-box about 1-2 weeks for step 4 onwards)

0. Spec `docs/specs/m2-search.md` with security requirements (Lucene query escaping, prompt injection through retrieved
   text, no upload chunks in public retrieval, cache-key isolation), the **minimum detectable effect fixed in advance**
   (about 40 gold questions with a paired test needs roughly 8 wins vs 1 loss: a 15-20 point hit-rate gain) and the **kill
   rule** (no gain at the MDE: stop, keep the non-search fixes, move to M6). Owner approves.
1. Logged traces ($0): extend `compare_retrieval.py` to keep ranks, scores, pre-filter candidates and edge chunk ids; run on a
   throwaway Community 2026.07.1 graph (7898) for D1, D4, X4, NG06, NG10. Gate: X4(b) measured, not inferred.
2. Non-search fixes first (each test-first): the badge; current-first edge ids + SEC stale-citation check; base-year id on
   computed lines; rule abstract and edge quote in the context; split `citations_retrieved`; a "cited passage supports the
   claim" eval check. Data fixes (edge re-adjudication, rule linker, new entities) are adjacent and may get their own spec.
3. Cache and seed plumbing: a retrieval-version component in the cache key; plan for the snapshot change; owner-approved paid
   eval to regenerate examples.
4. Benchmark gold before any retrieval change; freeze it; record the current path's recall@{5,8,20} and nDCG@10 ($0).
5. Candidates behind a flag: filer-scoped leg, full-text leg (over-fetch, post-filter, escaping), Python RRF.
6. One batched $0 retrieval ablation against the MDE; apply the kill rule.
7. Optional reranker spike only if step 6 shows a ranking gap (weights download needs approval; latency/RAM at Fly size).
8. Paid answer-level eval of the flagged path (approval), regenerate and reseed examples.
9. Security stage, verifier, deploy with the flag on, live-check D1's new answer.

**Is M2 mostly backend?** Yes: queries, index use, prompt context, checks and evaluation. Frontend touches are small
(citation status labels, the badge, "newest 8 of N", the "cited sources" wording). Several triage fixes are **data/ingestion**
(edge order in the loader, rule linker, entities, edge re-adjudication, older XBRL metadata).

## 4. Adding or removing a company today

**No one-trigger flow exists.** Every stage is a separate local CLI command joined by hand (RUNBOOK.md:599-620):

| Stage | Exists? | How today |
|---|---|---|
| Company list | Code, not data | `FILERS` (13, universe.py:12-26), `UNIVERSE` (14), `canonical_entities.json` (26; the agent's filer list is built from it, not from FILERS), generated `web/src/lib/pages.generated.ts`; about 22 files mention the count 13, several hard test pins |
| Name to ticker/CIK | No | `ingest` refuses tickers outside FILERS; `company_tickers.json` is cached forever |
| Download, parse, chunk | Yes | `semigraph ingest -t X` ($0); new filing layouts may need parser fallbacks (Intel, ASML did) |
| Extraction (LLM) | Yes, paid | `extract -t X --max-usd N` with dry-run, cap refusal and confirmation; derived (not measured) about $0.3-2.3 likely, $0.5-3.5 worst per company (58 to 417 chunks per filer) |
| Graph build | Yes | `build-graph --rebuild` ($0), `verify_graph.py`; new snapshot id invalidates cached answers |
| Benchmark and examples | Yes, paid | a full `eval` rerun (judge spend not capped by `--max-answer-usd`) then `build_examples.py` |
| Production data | Manual | dump, seed image, kill switch on, DB deploy (wipes upload workspaces, ledger and cache), API deploy, verify, kill switch off. Generation dump-swap is an M6 item, not built |
| Website page | Manual | regenerate pages.generated.ts and redeploy |
| Remove a company | Partial | only a rebuild drops it; other filers' extractions keep it as an entity |

Admin surface today: one shared `ADMIN_TOKEN` for the kill switch, a state report and a freshness "check now"; nothing changes
data. The freshness monitor detects new filings and never ingests them (M4 decision D1). Production machines cannot run the
pipeline (the 691 MB data lake is local; the serve image has no pandas, sec-parser or torch).

Options: **(a) a local CLI orchestrator** (`add-company`: name lookup, confirm, free stages, exact quote and approval, paid
stages, local build and checks, then today's reload deploy) - recommended; (b) a web admin console - later, needs a build
worker off the laptop, real admin authentication (Cloudflare Access, roles, audit log are tentatively M6), and the dump-swap;
(c) fully automatic - rejected (contradicts M4 D1). Full-path work (DB, file I/O, external APIs, LLM extraction). Freeze the
company list during M2's ablation, then build it. Settled by the fact-check: the missing `*_extractions_resolved.jsonl` for
asml/mu/tsm is harmless (the graph loader reads the raw files).

## 5. Internet search

**Today:** none. The agent has 7 closed tools, none web; M3 deferred the planned Tavily tool. Live external calls in the
served product: the EDGAR submissions API and a Federal Register count query (monitor). Filing scope is 10-K/10-Q/20-F; no
8-K or 6-K.

| Feature | Value | Credibility risk | Cost / terms | Recommendation |
|---|---|---|---|---|
| F1 "Since our data" panel per company: new 8-Ks and new BIS rules by accession/date, no LLM | High (event desk asked "new 1, but which?") | Low | $0; needs a Federal Register listing query (today it is a count) and 8-K targets in the monitor | **First** |
| F2 Ingest 8-K earnings exhibits (EX-99.1 press release; EX-99.2 CFO commentary is an NVDA practice) and 6-K for TSMC/ASML | High (this quarter, with filing citations) | Low | one-time paid extraction per filing | **Second**; shares the company pipeline |
| F3 Cross-filer mentions via EDGAR full-text search | Medium-high | Low-medium | $0; endpoint undocumented, stability unverified | Later, as a page block (no LLM) |
| F4 BIS press-release watch | Medium | Low | no official RSS found; scraping is brittle | Optional with F1 |
| F5 Check a pasted news claim against filings | High | Medium ("not in filings" must not read as "false") | one ask within caps | Good candidate; needs no web |
| F6 Open-web news panel (Tavily/Brave links) | Medium | High next to filing-grade evidence | effectively $0 at low volume (free tiers); Brave needs a custom plan to store results; Tavily terms unverified | Later; council |
| F7 Agent web tool | Medium | High (largest injection surface) | paid floors ($1.3317 Tavily, $1.3357 Anthropic search) exceed the per-IP ceiling $1.3272 | Later; council and a cap redesign |

Why official sources first: exact provenance (accession numbers), no open-web injection path, $0 search fees, and they close
named buyer gaps. Filing text is company-authored (redistribution not legal-checked); Federal Register documents are US
government works.

## 6. Proposed order (for the owner to decide)

1. Decision 004 triage fixes (section 3, steps 1-3), with F1 alongside.
2. Trimmed M2 with the company list frozen, the MDE and the kill rule.
3. Company pipeline (CLI) milestone, then F2.
4. Official-source search blocks (F3/F4, possibly F5).
5. M5c only for a named buyer (PDF export first).
6. M6 jumps the queue when a buyer meeting is booked; its dump-swap is a prerequisite for any web admin console.
7. Open-web search only after a council decision.

Still unverified: stage wall times; extraction cost per company (derived); reranker latency/RAM; ASML's exact linker failure;
X4(b) (no stored excerpt list); 6-K content for TSMC/ASML; Tavily/Exa display terms; whether v23 runs the fp32 embedder.
