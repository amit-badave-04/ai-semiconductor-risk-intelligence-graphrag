# semigraph roadmap from 2026-11-20

Status: **DRAFT for owner approval.** INTERNAL document. It states limits plainly. Do not copy it to the README or the site.

Basis: `docs/v2/research/m2-triage-and-next-milestones-2026-11-11.md` (fact-checked triage), `docs/decisions/004-next-milestone-m2-or-m5c.md` (council), `docs/v2/PLAN.md` section 6, three read-only checks on 2026-11-20 (code map, tooling, live re-verification), and two adversarial reviews of the first draft of this plan. This file replaces the order in PLAN.md section 6 for everything after M5b. PLAN.md and decision 004 are not edited before approval.

## 1. Owner replies (2026-11-20), verbatim, and how this plan reads them

| The proposal line | The owner's reply | Reading used here |
|---|---|---|
| "the triage fixes, with the 'since our data' panel alongside; a trimmed M2; the company pipeline (command-line), then 8-K ingestion; official-source search" | "start the work step by step." | This order. One step at a time. |
| "M5c only for a named buyer; M6 moves to the front if a buyer meeting is booked." | "drop it" | Both lines are out of this plan. No M5c work and no M6 work is scheduled. **To confirm (Q1).** |
| "Internet search" | "treat this as a separate milestone" | Milestone WS. Official sources first; the open web is a second stage. **To confirm (Q2).** |
| "Add or remove a company by trigger" | "treat this as separate milestone" | Milestone CP: one local command to add, one to remove. 8-K ingestion is its own milestone after it. **To confirm (Q3).** |
| | "continue, by first creating detailed plan." | No code starts before the owner approves `docs/specs/m2a-answer-integrity.md`. |

**Questions for the owner, recommendation first.**

- **Q1 What "drop it" covers.** Recommended reading: M5c and M6 are both dropped from this plan for now. What the owner accepts with it: (a) no dump-swap, so every graph deploy (M2a's deploy C, each later add or remove of a company) deletes all upload workspaces; (b) no demo or fallback runbook; (c) the kill rule of decision 004 no longer points to M6; if M2's ablation fails, work moves to CP. The other reading: only the two conditions are dropped and the milestones stay in the backlog.
- **Q2 Open web.** Recommended: stage WS2 inside WS, started only after an llm-council. The other option: a later milestone of its own.
- **Q3 8-K ingestion.** Recommended: its own milestone (EK) right after CP, because the owner named only add/remove as the separate milestone and 8-K brings different risks (a new form type, the Archives 403). It reuses CP's stages. The other option: fold it into CP as a second stage.
- **Q4 "By trigger".** Recommended: the trigger is one local command per action. A button on the site cannot run the pipeline: the data lake is local and the serve image has no parsing or model libraries.

## 2. Milestones

| Order | Name | Spec | State |
|---|---|---|---|
| 1 | **M2a Answer integrity + "Since our data"** | `docs/specs/m2a-answer-integrity.md` | DRAFT, for approval now |
| 2 | **M2 Search upgrade (trimmed)** | `docs/specs/m2-search.md` | DRAFT, approved only after M2a closes |
| 3 | **CP Add or remove a company** (local commands) | outline in `docs/specs/company-pipeline-and-web-search-outline.md` | outline |
| 4 | **EK 8-K ingestion** | same outline | outline |
| 5 | **WS Internet search** (WS1 official sources; WS2 open web after a council) | same outline | outline |

The names M2a, CP, EK and WS are new. They avoid the numbers of the dropped milestones.

## 3. Dependencies

- M2a changes what the model sees. M2's baseline and gold labels are taken **after** M2a ships, on M2a's snapshot.
- The company list and the entity list are **frozen during M2's ablation**. CP starts after M2 closes or is killed.
- EK reuses CP's pipeline stages. It is not built before CP.
- The panel (P1-P5 in the M2a spec) is detect-only: no LLM, no graph change, no seed wipe. It deploys on its own.
- M2a introduces `CONTEXT_VERSION`, folded into the template fingerprint. M2 reuses it for its flag. No second mechanism.
- Every graph change needs the dump/seed deploy. It replaces the ledger and cache, resets the kill switch to off and deletes every upload workspace (docs/RUNBOOK.md:614-616). A rollback does the same again (docs/RUNBOOK.md:618-619). Never during a demo. M2a batches its graph changes into one such deploy.
- Two local databases, never mixed. **G** is the real local graph (port 7699 per tests/integration/test_state_neo4j.py:4): rebuilds, dumps, traces and paid evals run there. **T** is the shared throwaway on 7898: integration tests only, synthetic data only. Neither is listening today. G's Neo4j version is not verified; production is Community 2026.07.1.
- After approval: update PLAN.md section 6 and the project memory to this order. Decision 004 stays as written; the changed kill-rule target is recorded here.

## 4. M2a Answer integrity + "Since our data"

- **Goal.** Remove the measured trust defects that need no new search: a wrong badge, stale and id-only citations, one-year percentages, a rule with no text in the prompt, two wrong edges, a blind rule linker. Add a panel that names the 8-Ks and BIS rules filed since our data.
- **In.** Answer-path requirements R1-R9, data fixes R10-R11 (R12 optional), panel P1-P5, one graph rebuild, one paid eval run, regenerated examples.
- **Out.** New search legs, fusion, reranking (M2). Paid edge re-adjudication, older XBRL metadata, per-id quotes (CP). Any prompt-rule change unless the paid eval proves the need.
- **Deliverables.** Three deploys: A (badge, API only), B (panel, API only), C (graph + API + new `examples.json`). Only A and B ship alone; everything else ships together as C.
- **Gates.** RED then GREEN per requirement. Spend invariants: hybrid estimate at most $0.76, agent estimate at most $1.02. Eval thresholds in the spec. Stage 4 per deploy. The owner is asked to run `/red-team` before each production deploy. Opus verifier.
- **Rough effort.** 12-16 working days. An estimate, not a measurement.
- **Paid spend needing approval.** One `eval-deployed` run plus the agent ship gate: about $2.7-4.1 likely; about $10.0 worst case ($2 answer cap + $0.69 judge worst case + $7.31, the agent eval's last printed worst case, which moves with per-question cost).
- **Risks.** The agent's spend margin is $0.0043, about 1,900 prompt characters (derived), so the agent will not get the quote and abstract lines without a cap change. D1 may not be fixed by quotes alone. X4 is half fixed without M2. The local model for promptfoo is not running. Neither local database is running.

## 5. M2 Search upgrade (trimmed)

- **Goal.** Close the retrieval-scope gap (D1, X4b): a passage that says "we" and never names the company cannot be an excerpt today (retriever.py:302-306).
- **In.** Structured gold ids; logged traces; a filer-scoped vector leg, a full-text leg and Python RRF behind one flag; ir-measures metrics; one $0 ablation against a decision rule fixed in advance; a paid answer-level eval only if the ablation passes.
- **Out.** neo4j-graphrag, adaptive k, edge cap/rank, a Neo4j upgrade, a reranker (spike only on a measured ranking gap).
- **Gates.** Gold frozen before any candidate runs. Two-sided exact sign test at 0.05 (smallest passing results 6-0, 8-1, 10-2, 12-3). Not met: stop and move to CP.
- **Rough effort.** About 2 days of gold work, then a 1-2 week time-box (decision 004).
- **Paid spend.** $0 for the ablation. One answer-level eval, same size as M2a's, only after a pass.

## 6. CP Add or remove a company

- **Goal.** One local command to add a company and one to remove it, instead of many joined by hand (docs/RUNBOOK.md:599-620).
- **In.** `add-company`: name lookup, confirm, free stages, an exact quote and approval, paid stages under a cap, local build and checks, stop before the deploy. `remove-company`: default meaning "no longer a filer, still an entity other filers mention" (for approval).
- **Out.** A web admin console. Automatic ingestion (contradicts M4 decision D1).
- **Gates, effort, spend.** Set in its spec. Derived, not measured: about $0.3-2.3 likely and $0.5-3.5 worst per company for extraction, plus a full eval rerun.
- **Risks.** About 22 files pin the count 13 (the investigators' count, not re-checked). New filing layouts may need parser fallbacks.

## 7. EK 8-K ingestion

- **Goal.** Ingest 8-K earnings exhibits (EX-99.1) for the 11 domestic filers. 6-K for TSM and ASML only after their content is checked.
- **Risks.** The exhibit list needs the Archives filing index, which answered 403 to a generic User-Agent on 2026-11-20. Extraction cost per filing is unmeasured.

## 8. WS Internet search

- **WS1 Official sources.** Candidates: cross-filer mentions through EDGAR full-text search (endpoint undocumented); a BIS press watch (no official RSS found); a pasted-claim check against filings (no web). $0 search fees.
- **WS2 Open web.** A news panel and an agent web tool. Both need a council first. The agent tool also needs a cap redesign: its paid floors ($1.3317, $1.3357) exceed the per-address ceiling of $1.3272.

## 9. Facts that changed since the 2026-11-11 note (checked live 2026-11-20 by the reviewers)

- Two BIS rules are newer than the served snapshot: 2026-23529 "Addressing Remote Access to Advanced Semiconductors" (2026-11-19) and 2026-22793 "Extension of the Suspension of Expansion of End-User Controls for Affiliates..." (2026-11-09). The local Federal Register cache ends at 2026-09-24.
- NVDA filed two 8-Ks: 0001045810-26-000088 (2026-11-17, items 2.02 and 9.01) and 0001045810-26-000082 (2026-11-12, items 5.02 and 9.01).
- The EDGAR submissions JSON carries `items` and `primaryDocument`.
- Local promptfoo is 0.125.1 and one release behind. Nothing is bumped without the owner and a skillspector scan.

## 10. Not verified (carried into the specs)

The local model server on 127.0.0.1:8080 is down and its start command was not found. G's port, Neo4j version and current snapshot. Whether the local dump holds Svc* rows from local monitor runs. Whether the replay embedder matches production (research note line 138). Federal Register rate limits. The remaining part of PLAN.md's $30 API budget. The ir-measures licence (the PyPI field is empty). Whether syft and grype are current.
