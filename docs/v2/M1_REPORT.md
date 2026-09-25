# M1 (v1.1) status report — 2026-09-25/26

Branch `v2` (base tag `v1-final`). **M1 is not finished**: data, correctness fixes and the freshness model are built and verified; the cheaper-answerer bake-off (P5-lite) and the redeploy are waiting on owner inputs (see the end).

## Delivered and verified

| Gate | Evidence |
|---|---|
| Data refreshed to 2026-09-25 | `semigraph freshness --as-of 2026-09-25`: **0 filings pending** (15 ingested: 9 new 10-Qs, MSFT FY26 10-K, and 5 current-fiscal-year 10-Qs v1's "latest only" rule had skipped); Federal Register stored **166 = live 166** BIS rules (v1: 13). |
| Chunk ids never change | `4,933` pre-existing chunk rows byte-identical across 13 parquets (verified against the pre-pull backup), `+961` appended. |
| Delta extraction | 660 chunks (Sonnet 5 extractor + Haiku 4.5 critic, checkpointed), 0 remaining. Estimate $3.67 likely / $5.51 worst case (approved cap $5.60). Check the Anthropic console for the exact spend. |
| AMD 10-K/A case | The 10-K/A restates **only Item 7**. In the rebuilt graph the original's Business (40 spans) and Risk Factors (38) stay current; its MD&A (31 spans, wrong "31% units / 15% ASP") is `corrected`, not retrievable, `corrected_by = 0000002488-26-000021`; the 10-K/A's MD&A (31) is current. `/api/evidence` exposes this. |
| Graph rebuilt on the production engine | `build-graph --rebuild` into **Neo4j Community 2026.07.1**: snapshot `snap-20260924-7feaaf9bfe`; 74 filings, 3,152 spans, 9,775 risk factors, 739 metrics, 166 rules (26 relevant), 94 AFFECTED_BY. `scripts/verify_graph.py`: **10/10 invariants pass**. |
| Filtered search + full-text on Community 2026.07.1 | Live probe and the integration suite: **28/28 passed on Community and 28/28 on Desktop 2026.05**; `IN` works on 2026.07 (not on 2026.05), a later `SET is_current=false` is honoured, `3A090`/`H200`/quoted ids/FR numbers are found by full-text. |
| v1 graph untouched | Desktop `neo4j` DB still 12,606 nodes / 22,792 relationships; v1 dump backed up at `data/backups/v1-final.neo4j.dump`. |
| Independent review | Opus verifier: verdict NOT DONE with 2 HIGH + 7 MEDIUM findings; all HIGH/MEDIUM fixed except two deferred (below). |
| Tests | 781 passed, 28 opt-in integration tests (Neo4j). |

## Bugs found and fixed (v1 defects unless noted)

1. Federal Register query behaved as AND (13 of 166 rules).
2. Ingestion could not pull new filings (per-ticker skips, string-sorted "latest 10-Q", caches that never refresh); one unfetchable filing could abort a run (fixed with a submissions fallback + per-filing isolation, found by review).
3. TSM/ASML metrics printed as USD; TSM FY2021 stored in USD instead of TWD; TSM FY2025 filled from the filing's inline XBRL.
4. AMD's corrected MD&A was never ingested; whole-filing supersession would have retired its Business/Risk text (section-level supersession).
5. Stale quarters/annual MD&A stayed retrievable (per-section `retrievable`); building mid-extraction would have falsely closed lineages (build now refuses).
6. Snapshot identity did not cover chunks, sections, entities or graph code (fixed).
7. Rule lines ~100 tokens each × 12 per company over anchor + neighbours added ~8k tokens/answer (cost regression) — capped to anchors, 8 rules.

## Retrieval before/after (20 gold questions, `scripts/compare_retrieval.py`)

Edge lines 1,561 → 1,438 in total (Nvidia questions 86 → 79); metrics identical except TSMC/ASML (unit fix) and MSFT FY26; risks/excerpts differ by 2–8 items per question (new filings, freshness filter); every lineage id differs (deterministic re-clustering) — correctness of the temporal questions is decided by the benchmark, not by ids.

## Known limits and deferred items

- `--as-of` bounds EDGAR ingestion only; the Federal Register cache and XBRL are not bounded, and `build-graph --as-of` only *validates* against the lake. Bounded rebuilds (needed for two-snapshot as-of tests) come with M4.
- The relation block is still large (company edges ~71 lines for Nvidia); ranking/capping is M2. `DEFAULT_ANCHOR_CIK` (Nvidia) is kept because benchmark R2 relies on it and is now reported as `anchor_defaulted`.
- An amendment is attached to the latest original filed before it (a 10-K/A filed after the next 10-K would overlay the wrong year); `period_of_report` is not stored yet.
- XBRL restatements still lose to the first disclosure.
- The example answers shipped with v1 belong to the v1 graph and are **not seeded** into the new cache until regenerated.

## Waiting on the owner

1. Approve a **baseline benchmark run on the new graph** (Sonnet 5, 20 questions, hybrid + vector, judges): about **$3**.
2. **API keys** in `.env` (never in chat) for the bake-off: OpenAI (GPT-6 Luna), Google (Gemini) and one US open-weight host (DeepInfra/Fireworks/Together) or OpenRouter with US-only routing. `.env` has only `ANTHROPIC_API_KEY`.
3. **~30–40 hand labels** on judge-dependent answers for judge calibration.
4. Permission to **redeploy v1.1** on Fly (new Community dump baked into the DB image; regenerate example answers first).
