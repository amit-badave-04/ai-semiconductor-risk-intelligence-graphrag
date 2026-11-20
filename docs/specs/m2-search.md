# M2: search upgrade (trimmed)

Status: **DRAFT. For approval only after M2a closes.** Numbers marked "after M2a" are filled in then. INTERNAL document. Path: full (DB queries, LLM context, a new eval dependency).

Basis: decision 004 (recommendation 3), the research note sections 2 and 3, and the live re-verification of 2026-11-20. Ids: Q = requirements, S = security claims, T = plan steps.

## 1. Why

Two of the triaged defects are retrieval scope. The excerpt query keeps a passage only if it MENTIONS an anchor company (retriever.py:302-306). A filer's own passage that says "we" can never be an excerpt. That is D1 and, by inference, X4(b). BM25, fusion or reranking alone change nothing while that hop stays. The fix is a second search leg scoped by filer; `filer_cik` is already a filter property of the vector index (schema.cypher:45-47).

The full-text indexes exist (schema.cypher:53-57) and no retrieval code queries them. Nothing fuses ranked lists today.

## 2. Scope

**In.** Structured gold ids; logged traces; a filer-scoped vector leg; a full-text leg; Python reciprocal rank fusion; one flag; ir-measures; one $0 ablation; a paid answer-level eval if the ablation passes.

**Out.** neo4j-graphrag (the research note found its hybrid retrievers take no filters and have no RRF; not re-checked); adaptive k; edge cap and rank; a Neo4j upgrade (production stays on 2026.07.1); `SEARCH ... FULLTEXT` (the Cypher manual gives it from 2026.09); a reranker, unless section 5 triggers a spike. The company and entity lists are frozen from the gold freeze to the end of the ablation.

## 3. Requirements

**Q1 Gold ids first.** Add a structured `retrieval_gold` list of EvidenceSpan and rule ids to the benchmark questions. Sources: the chunk ids already in `facts`, the ids in free-text `judge_notes`, and new labels seeded with D1 and X4. Every id is checked to exist in the M2a snapshot. Target: at least 40 questions with gold. Labelling is capped at 2 working days. The file's hash is recorded here before any candidate runs. After that the gold does not change.

**Q2 Logged traces.** `scripts/compare_retrieval.py` (extended and tested in M2a I0) also keeps scores and the candidates before the post-filter, per leg.

**Q3 Baseline.** On the frozen gold, record the current path's Success@8, R@5, R@8, R@20 and nDCG@10. $0. Run with the production embedder (`EMBEDDING_BACKEND=onnx`).

**Q4 Metrics library.** ir-measures 0.4.3 over pytrec-eval-terrier 0.5.11 (MIT, a Windows cp313 wheel is listed), in the eval extra only, never in the serve image. Before installing: read the ir-measures LICENSE file (the PyPI licence field is empty) and test install and import on this machine. The lockfile changes, so osv-scanner runs.

**Q5 Filer-scoped leg.** A vector search with `filer_cik` in the index filter, `retrievable` kept, over-fetched (a filtered search can return fewer than top-k). Start with one search per anchor using `=`. `IN $ciks` is documented from 2026.06, and production is 2026.07.1, so it should work, but it is **untested**: prove it on a 2026.07.1 instance or keep `=`.

**Q6 Full-text leg.** `db.index.fulltext.queryNodes` on `evidence_text_ft`. That it is not deprecated on 2026.07.1 is the research note's claim; confirm on the instance first. Over-fetch, then post-filter (`retrievable`, current, public). The query string is built by one escaping helper (S1).

**Q7 Fusion.** About 15 lines of Python RRF, k = 60, over the mention leg, the filer leg and the full-text leg. Rank-based, because raw scores are not comparable across sources. No fusion library.

**Q8 One flag.** `RETRIEVAL_FUSION`, off by default. Off means byte-identical contexts to M2a (a golden test).

**Q9 Cache and examples.** The flag's effective value is part of the template fingerprint through M2a's `CONTEXT_VERSION` mechanism. Flag on and flag off never share a cached answer, and examples generated under one are refused under the other.

**Q10 Ablation.** One batched $0 run on the release graph: baseline against (a) plus filer leg, (b) plus full-text, (c) all three with RRF. Primary metric and candidate are fixed now: **Success@8 of candidate (c)**.

**Q11 Answer-level eval.** Only after a pass: one paid `eval-deployed` with the flag on, then the agent gate, with the same commands, caps and approval as M2a section 8; the M2a thresholds are non-regression gates, plus the D1 row. Then regenerate and reseed examples (a code-only deploy if the snapshot is unchanged).

## 4. Minimum detectable effect and kill rule (fixed in advance)

Per question the outcome is binary: at least one gold id in the top 8, or not. Compare baseline and candidate per question. Count wins W (candidate hits, baseline misses) and losses L. Ties are ignored. Two-sided exact sign test at 0.05:

| W-L | Two-sided p | Passes |
|---|---|---|
| 5-0 | 0.063 | no |
| 6-0 | 0.031 | yes |
| 7-1 | 0.070 | no |
| 8-1 | 0.039 | yes |
| 9-2 | 0.065 | no |
| 10-2 | 0.039 | yes |
| 11-3 | fails | no |
| 12-3 | 0.035 | yes |

The smallest passing results are 6-0, 8-1, 10-2, 12-3. With at most 3 losses that is a net gain of 6 to 9 questions: on 40 questions, **15 to 22.5 points of Success@8**. With more losses, more net wins are needed. A smaller true gain will usually not be detected. That is accepted here, in advance.

**Kill rule.** If candidate (c) does not reach a passing result: stop. The flag stays off. Keep the gold, the traces and the baseline. Record the result and move to CP. No second ablation with new settings. Also stop if the time-box (10 working days from Q5) runs out.

## 5. Reranker trigger

A spike is opened only if the ablation shows a **ranking** gap: for at least 5 gold questions a gold id is in candidate (c)'s top 20 but not its top 8. A spike needs owner approval for the weights download, and CPU latency and memory measured at the production machine size.

## 6. Security requirements and abuse cases (checkable)

- **S1 Lucene escaping (direct input).** One helper escapes `+ - && || ! ( ) { } [ ] ^ " ~ * ? : \ /`, drops bare AND, OR, NOT, caps the length and the term count, and quotes ids as phrases. Tests: each special character; a 10,000-character question; an unbalanced quote; `*:*`; a fuzzy `~` bomb. No input raises a parse error or widens the query.
- **S2 Parameters only.** The query string is a Cypher parameter. The index name is a constant.
- **S3 No upload chunks.** Neither new leg can return workspace evidence. Integration test on the throwaway instance with an upload fixture whose text matches the query exactly. How upload chunks are labelled against `evidence_text_ft` is unverified; check it first.
- **S4 No superseded text as current.** Both legs filter or mark currency the way the mention leg does.
- **S5 Indirect injection.** The new legs bring more filing text into the prompt: the same trust class as today's excerpts. The M2a promptfoo cases, PII case included, are rerun with the flag on.
- **S6 Cache-key isolation.** Q9's test: the key differs between flag values.
- **S7 Bounded work.** Fixed over-fetch sizes; at most one extra vector search per anchor and one full-text query per ask. Latency is measured before and after.
- **S8 Spend.** If the context grows, the M2a invariants are re-proved (hybrid at most $0.76, agent at most $1.02). The agent shares the retrieval path; whether the flag reaches it is decided against its margin, as in M2a OD6.
- **S9 No new tool.** The agent's tool list is unchanged.

## 7. Plan

| Step | What | Cost |
|---|---|---|
| T0 | Owner approves this spec with the M2a numbers filled in | |
| T1 | Q4 licence and install test; Q1 gold; freeze and record the hash | $0 |
| T2 | Q2 traces; Q3 baseline | $0 |
| T3 | Q5-Q9, test first, flag off | $0 |
| T4 | Q10 ablation; apply section 4 | $0 |
| T5 | Section 5 spike, only if triggered | $0 API; approval for weights |
| T6 | Stage 4 merge gates and the owner's `/red-team` on the frozen code | $0 |
| T7 | Q11 paid eval and examples, only after a pass | about $2.7-4.1 likely, about $10.0 worst, as M2a |
| T8 | Verifier, deploy with the flag on, live check of D1 | |

## 8. Risks and where the obvious implementation is wrong

1. Adding BM25 and RRF but keeping the MENTIONS post-filter on every leg. D1 stays broken.
2. Fusing raw scores from the vector and full-text legs. They are not comparable.
3. Asking a filtered vector search for exactly k rows. It can return fewer; over-fetch.
4. Labelling gold after seeing candidate output. The gold is frozen first.
5. Tuning k, weights or legs after the ablation and testing again on the same questions.
6. Flipping the flag by environment variable without changing the fingerprint. Stale answers replay from the cache and seeded examples replay forever.
7. Using `SEARCH ... FULLTEXT`. Production's version does not have it.
8. Running the ablation on the throwaway test instance, or with a different embedder from production.

## 9. Acceptance checks for the verifier

Gold hash recorded before the first candidate commit (check by git history). Flag off is byte-identical to M2a. The ablation report shows per-question outcomes, W, L and the result applied. S1's cases pass. No ir-measures import under `src/semigraph/serve`. The kill or the pass is recorded with its numbers.

## Status

| Stage | Gate | Evidence needed |
|---|---|---|
| 1 Spec | owner approval after M2a | the owner's words |
| 2 Dev | RED then GREEN; gold frozen first | hash; test counts |
| 3 Regression and ablation | section 4 applied | the report |
| 4 Security | pre-push and merge gates as in M2a; the owner is asked to run `/red-team` before the flag-on deploy | commands and results; the report path |
| 5-6 Fix, re-test, verifier | as in M2a | as in M2a |
| 7 Deploy or kill | recorded | image tags, or the kill record |
| 8 Archify | only if the architecture changed | decision and reason |
