# Decision 010: M2 (state-of-the-art search) design

Date: 2026-11-26. The owner, on the deferred M2: "I want this, state of the art system", and on the order: "M2 first, then CP".
Uploads for standard SEC filing sizes are built inside M2 (decision 009). Research (workflow wf_3be56c03-bcc, read-only) re-checked
the 2026 state of the art live (T2-RAGBench, financial text and tables: dense R@5 0.587, BM25 0.644, hybrid RRF 0.695, contextual
hybrid 0.717, hybrid plus a hosted reranker 0.816), designed a measurable benchmark, and analysed the deep agent's failed ship gate.
Then the `llm-council` skill (workflow wf_7f6b97be-282): five advisors (Sonnet), five anonymised peer reviews (Sonnet), a chairman (Opus).
This supersedes decision 006's deferral (the owner's call); decision 006's tripwire gold and tool stay in use.

Status: **council advice; the owner decides with the M2 spec (docs/specs/m2-search.md).** INTERNAL document.

## Decisions put to the council

E1 reranker (none, CPU MiniLM, hosted, or a trigger); E2 keyword engine (Neo4j full-text procedure, in-process bm25s, or a Neo4j
upgrade); E3 benchmark (a blind M2-Bench of about 90 questions); E4 contextual prefix; E5 re-enabling the deep agent inside M2;
E6 per-company query decomposition; and the build order.

## Chairman's verdict (verbatim)

## Where the Council Agrees

- **Measure before building.** The 60-question benchmark has 0 retrieval misses, so it cannot show a gain. Without a new, harder benchmark, any "state of the art" claim is unmeasured. All five advisors and all five reviewers agree.
- **E2: use bm25s in-process (b).** All five chose it. It is MIT-licensed, costs $0, has no deprecation cliff, needs no Neo4j migration, and one engine serves both filings and uploads (decision 009). Option (a) builds on a procedure deprecated from 2026.10 whose replacement has no WHERE pushdown. Option (c) is a migration with no payoff.
- **Hybrid + RRF fused into the existing 8 slots is the capability to prove.** It adds no context, makes no LLM call, needs no vendor, and has the largest cheap public gain (dense 0.587 to hybrid 0.695 R@5).
- **No hosted reranker without evidence and owner vendor approval. Uploads are never eligible.**
- **E5:** keep the failed agent out of M2's critical path. **E6:** defer, because the $0.76 cap has no headroom.
- **Response B (Expansionist) is the outlier.** Every reviewer named it the biggest blind spot.

## Where the Council Clashes

- **Benchmark size.** Options were 90 (D, B), about 30 (A, E), or a 20-question dev slice (B). C says about 15 per type is underpowered either way. **Ruling:** the power problem is solved by how the test is pre-registered, not by shrinking it (see Recommendation).
- **Reranker.**
  - (a) none: C, E.
  - (d) defer to a trigger: D, A.
  - (b) MiniLM plus a pluggable hosted stage: B.
  - D tests MiniLM offline; C tests it on the machine.
  - **Ruling:** (d). The trigger is a written rule. MiniLM is the first candidate, checked for quality offline and for latency on the machine.
- **Prefix timing.** D and C say defer. A and B say ride the CP rebuild. **Ruling:** ride the CP rebuild, which happens anyway, so there is no extra rebuild.
- **Agent.** B wants it as an M2 goal; everyone else wants it separate. A's middle path wins: fix the shared search-tool bugs, do not re-gate.

## Blind Spots the Council Caught

1. **The anchor-mention rule is probably the real weak spot, and the cheapest fix.** Right now a company's own "we..." passages can never become excerpts. A filer-scoped dense leg without the mention requirement is free on 2026.07.1. Measure it first, as its own ablation, so hybrid retrieval does not get credit for it.
2. **Slot allocation.** Fusion into 8 slots needs an explicit policy: a guaranteed share for filer-own passages, a cap per source document, and diversity. Otherwise one leg crowds out the others.
3. **Upload isolation.** Build one bm25s index per user and per upload, never a shared index. Isolation then holds by construction, not by a filter. Add a negative test that user A's query never returns user B's text.
4. **Memory and cold start on the 4 GB machine.** bm25s sits beside the fp32 ONNX model, and rebuilds per graph and per upload are unmeasured. Make RSS, index build time and p95 retrieval latency explicit ship gates.
5. **Latency users feel is the query embedding (5.5-36 tok/s), not BM25.** Embed the query once and reuse the vector across every dense leg. Extra legs must not add embedding calls.
6. **Statistical power and independence.**
   - A paired sign test counts only discordant pairs, so per-type tests on about 15 items will show nothing.
   - The gold labels are written by the same agent family that builds the retrieval.
   - Fixes are in the Recommendation.
7. **Retrieval gain is not answer gain.** Pre-register the paid answer-level run ($2-5) as a decision rule. Write down beforehand what counts as success.
8. **Rollback.** Ship hybrid behind a flag, with dense-only as the instant fallback.
9. **A free prefix win the council missed.** bm25s can index a deterministic context field (company, form, year, section) right away with no re-embed. Most of E4's benefit for exact-term and period queries arrives through the keyword leg at zero cost.

## The Recommendation

**E1 Reranker: (d), defer to a written trigger.**
- None ships in M2 unless hybrid still leaves at least 15% of M2-Bench dev misses that are ranking misses: gold found in the top 50, not in the top 8.
- If triggered, test MiniLM first. Check quality offline on dev, then latency on the machine. The gate is p95 added latency of 2 s or less, throttled.
- A hosted reranker (c) goes to the owner only if MiniLM fails both conditions: the trigger still fires and MiniLM misses its quality or latency gate.
  - Voyage rerank-2.5 is the cheapest option.
  - Public filing text only.
  - Enforced structurally: the upload retrieval path has no reranker call in its code, so this is not a routing flag.
  - It needs vendor approval and a data-egress note.

**E2 Keyword engine: (b) bm25s.**
- One index per graph snapshot for filings, built at startup or deploy.
- One index per upload, isolated per user.
- BM25 document text includes the deterministic context field.
- Gates: RSS headroom on 4 GB, build time, p95 latency.
- Do not touch the deprecated Neo4j full-text procedure. Do not upgrade Neo4j for this.

**E3 Benchmark: the blind M2-Bench, about 90 questions, split 30 dev / 60 test. Write and hash-commit all of it in one labelling pass, before any retrieval change.**
- **Blind labelling:** gold is written from filing text by an agent pass with no access to retrieval code or outputs. Opus checks it, and the owner spot-checks about 10 items.
- **Difficulty target:** the current system should miss at least 30% on dev. If it misses less, add harder items before freezing the test split.
- **Primary test:** one pre-registered pooled paired sign test on Success@8 over the 60 test questions. Per-type results are descriptive only.
- **No-loss gates:**
  - the old 60 stay at 0 misses;
  - no per-type net loss on the new set;
  - latency and RSS gates pass.
- Dev is used freely during development. Test runs once.

**E4 Contextual prefix: deterministic prefix only, and only when the CP rebuild re-embeds the graph anyway.**
- Its BM25 half ships now, free (blind spot 9).
- No separate rebuild. No LLM-written prefixes.

**E5 Agent: separate from M2's goals.**
- Inside M2, fix only what shares code with the new retrieval: search_filings uses the hybrid legs, larger k, and dedup against the prefetch (the X4 0-excerpt bug).
- No planner work and no paid re-gate in M2.

**E6 Query decomposition: later.**
- In M2, handle multi-company questions deterministically instead: one filtered dense leg plus one bm25s leg per detected anchor company, fused by RRF. This needs no LLM call and stays inside the cap.
- The LLM decomposition call is reconsidered only if the multi-company slice still fails after that.

**Build order (each step behind a flag, each measured on dev):**
1. Write and hash-commit M2-Bench (dev + test), then run the baseline. About 2 days.
2. Free legs:
   - a filer-scoped dense leg without the anchor-mention requirement;
   - period-filtered legs;
   - per-anchor legs;
   - a slot-allocation policy.
   Measure this step alone.
3. bm25s with the context field, plus RRF fusion into 8 slots. Then measure the RSS, latency and no-loss gates. **This is the "capability proved" milestone to show the owner.**
4. Uploads keyword search on the same engine, with per-user indexes and an isolation negative test.
5. Fix search_filings dedup and k.
6. Run the test split once and apply the pre-registered decision. Then the owner-approved paid answer-level run ($2-5).
7. Check the reranker trigger. Only if it fires: MiniLM, then a hosted reranker with owner approval.
8. The prefix re-embed rides the CP rebuild.

Tell the owner plainly: "state of the art" in M2 means measured hybrid retrieval (dense plus BM25 plus filtered legs, fused). It is proven on a blind benchmark at $0/month and with no added cost per answer. A reranker or vendor comes only if the data asks for it.

## The One Thing to Do First

Write and hash-commit the blind M2-Bench (30 dev + 60 test) and run today's system on it. Then make the first change: the filer-scoped leg that allows a company's own "we..." passages, measured on its own. Every later decision (bm25s value, reranker trigger, prefix, decomposition) depends on that baseline and that first ablation.

## Owner decision

2026-11-26: the M2 spec is approved with one change from the council: **"Approve, include the agent"** (the agent moves
onto the new retrieval and its paid ship gate runs inside M2). Cap $15; blind Claude drafters for M2-Bench; public wording only after
the pre-registered test passes. See docs/specs/m2-search.md, "Owner decisions 2026-11-26".
