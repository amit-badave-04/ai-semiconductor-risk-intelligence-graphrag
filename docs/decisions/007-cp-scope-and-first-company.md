# Decision 007: CP (add or remove a company) - scope, removal semantics, first company, one source of truth

Date: 2026-11-26. Context: decision 006 deferred M2 and put CP next. The owner, going to sleep on 2026-11-25: "If you need help with
decisions use llm counsil." Three read-only research agents mapped the company list and pipeline in the code, re-checked SEC EDGAR
access, the EDGAR libraries and orchestration options against live sources, and measured the eval, deploy and graph impact of a
company change (workflow wf_154dd3f5-e20). The `llm-council` skill was then convened on the four open decisions (workflow
wf_0b3afa72-5f5): five advisors (Sonnet), five anonymised peer reviews (Sonnet), a chairman (Opus).

Status: **council advice; the owner decides when approving the CP spec (docs/specs/cp-add-company.md).** INTERNAL document.

## Decisions put to the council

- D1 Scope of CP v1: add only, or add and remove together.
- D2 Removal semantics: (a) not a filer, still a mentioned entity; (b) purge every mention; (c) hide on the site only.
- D3 First company: TXN (plain Delaware 10-K filer, safest layout bet), MRVL (strongest AI-silicon story; already a mentioned
  non-filer entity, so it exercises the "promotion" path), or another; and whether the first run is a dry run.
- D4 One source of truth for the company list, or the current two sources (FILERS and canonical_entities.json) with a consistency test.

## Chairman's verdict (verbatim)

## Where the Council Agrees

- **D1: build add-only.** All five advisors agree. Removal has no user. It forces a semantics debate nobody has validated, and it collides with hand data: edge overrides, tripwire gold, the Nvidia default anchor and the NVDA/ASML e2e fixtures.
- **D2: decide now, build later.** No one wants removal built. Four advisors lean to (a) as the recorded direction.
- **D4: build the single source of truth as part of CP.** All five agree that a consistency test only catches drift after it happens. The two sources plus the ~15 hand-pinned "13"s are where the next silent bug will come from.
- **Free checks come before paid ones.** Everyone wants a $0 local parse to check the Item 1A layout before any paid extraction, with the existing dry-run, cap and confirm idioms reused.
- **Pre-registration.** The 3-5 tripwire questions required by Decision 006 must exist before the run, not be written after it.
- **No framework.** Use plain Python with a JSON state file.

## Where the Council Clashes

**1. D3: TXN first or MRVL first?**
- The Contrarian wants TXN first and MRVL second. The argument: MRVL stacks two unknowns (an unverified layout and the promotion path that silently drops edges), so a failure can't be attributed to either one.
- First Principles, the Executor and the Outsider want MRVL. Their argument: TXN gives a green run that proves nothing, and promotion has to work eventually anyway.
- The Expansionist also wants MRVL, but for a product-strategy reason ("every mentioned entity is a free future filer").
- **Resolution:** both camps are right about different things. The two unknowns can be separated without shipping TXN first, because both are testable at $0:
  - Layout: a free local parse.
  - Promotion: a fixture or integration test (Marvell with cik None becomes a filer, edges are preserved, counts are asserted).
- Once both are proven separately for free, MRVL no longer stacks two untested risks. The Contrarian's attribution concern is met without spending a paid run on a company that teaches nothing.

**2. Contrarian's D2 lean toward (c).**
- Choosing (c), hide on the site, for reversibility is wrong. Hiding a company in the UI leaves it in the graph and in retrieval, so answers can still cite a company the site claims is gone. That is a silent inconsistency on a public demo.
- (a) matches how the graph already treats Marvell, Arm and GlobalFoundries.

**3. The Expansionist's platform framing.** All five reviewers rejected it, and they were right to:
- There is no buyer.
- "Removal is a flag flip plus a rebuild" contradicts the facts found: lake files are glob-discovered and hand data breaks.
- A public "time to add a company" headline before the pipeline has run twice would overclaim. Revisit it only after two real adds.

**4. Contrarian vs. Executor on whether TXN's green run is worthless.** It is worthless as a shipped first company. It is still useful as a free parse-only fallback, so keep it in that role.

## Blind Spots the Council Caught

The reviews raised these points. All of them are valid, and several are gates.

1. **Regression on the existing 13 is the real risk of CP.** Only the Contrarian named it. The whole-corpus rebuild changes MENTIONS and entity resolution for every company. "One command" with no before/after gate is a regression generator.
   - The pass criterion must be pre-registered before the run.
   - Free gate: per-company invariant diff and anchor-outcome golden test, exact or with a stated tolerance.
   - Paid gate: the 60-question benchmark diff against the current baseline, with a pre-registered tolerance.
   - Passing "the eval" without a pre-registered threshold amounts to tuning on the test set.
2. **The dry run must be isolated mechanically, not just "not deployed."** The lake is glob-discovered and shares checkpoints, the snapshot id and examples.json. A staging run needs its own output root (lake, checkpoints, state file) and a separate Neo4j database. Merging into the real lake is an explicit step after the gates pass.
3. **Atomicity, undo and idempotency.**
   - An aborted add must not leave the repo or the lake half-updated.
   - The command must be resumable from the JSON state file after a paid step.
   - It must refuse to run on a dirty working tree.
   - It must refuse, or handle explicitly, a company that is already present.
   - The Executor's `--undo` (restore the list files from git, delete that company's glob files) is the cheap version, and it is enough.
4. **A spend cap and stop rule for the milestone, not just per run.** Use a hard cap of $15 for CP in total. Stop and report to the owner if the cap is reached or if two paid attempts fail.
5. **Policy for the 14th company in the benchmark.**
   - Freeze the 60-question set as the regression instrument, so before/after comparisons stay valid.
   - Cover the new company through the Decision 006 tripwire questions.
   - Do not grow the 60 during CP.
6. **Timing of pre-registration.** The tripwire gold is passage-level. Write it from the raw filing and commit it (hash recorded) before the pipeline parses, chunks or retrieves anything for that company. Reading the filing is fine. Seeing pipeline output first is not.
7. **Refreshing `company_tickers.json`** must be a tested first step of every run. Use a freshness assertion, not a manual reminder.
8. **Shared adjudication checkpoints make the other companies look stale.** Make the free re-run an automatic step in the command so it is not a surprise.
9. **D4 must keep entity ordering.** The order of `canonical_entities.json` decides which anchors survive MAX_ANCHORS=4.
   - The single source needs an explicit order or priority field.
   - It covers all canonical entities (filers and mentioned non-filers, with a filer flag), not just the 13 filers.
   - The refactor must be a proven no-op: generated files byte-identical to today's, the golden anchor test unchanged, the graph invariants unchanged. All of that is $0, with no paid eval.
10. **Honest naming.** The command ends at a staged, gated, merge-ready corpus, before the dump and deploy, which stay manual. The README should say exactly which steps remain manual. The rollback for the deploy is the previous dump, kept until the new release passes the post-deploy benchmark.
11. **Decision record.** D3 was split and expensive, so save this verdict to `docs/decisions/007-cp-scope-and-first-company.md` (or the next free number).

## The Recommendation

**D1: Add-only for CP v1.**
- Include a cheap `--undo` for a failed or abandoned add (restore the list files from git, delete that company's glob-discovered lake files, drop its staging database). This is not removal.
- Put this line in the spec: "Removal is unsupported. Reopen only on a concrete need."

**D2: Record (a) as the direction and build nothing.**
- (a) means the company stops being a filer and stays a mentioned entity.
- It matches the existing model for mentioned non-filers. It doesn't rewrite other companies' edges. It is the only option that keeps the site and the graph consistent.
- Reject (b): it is irreversible and blind to the glob.
- Reject (c): it leaves the data in retrieval while hiding it in the UI.

**D3: MRVL is the first shipped company, gated by two $0 proofs, with TXN as the fallback.**
1. Free local parse of both MRVL and TXN to check the Item 1A layout.
2. Promotion test, written TDD: Marvell goes from cik None to filer, edges are preserved, MENTIONS counts are asserted, and nothing drops silently.
3. Pre-register MRVL's 3-5 tripwire questions from the raw filing, with at least 2 answerable only from Marvell's own "we..." voice. Commit them before the pipeline parses it.
4. One capped paid extraction for MRVL in the isolated staging workspace.
5. Rebuild staging, then run the 13-company free invariant diff and the golden anchor test.
6. Run the tripwire tool at $0.
7. Run the paid 60-question benchmark diff against the pre-registered tolerance.
8. Merge into the real lake. The owner then dumps and deploys by hand.

Fallback: if MRVL's layout parses badly, switch to TXN. If the promotion test can't be made green at $0, also switch to TXN and tackle MRVL as the second company. In either case, pre-register TXN's questions before its parse.

On the "dry run on an unshipped company" question: no throwaway company. The dry run is the isolated staging run of MRVL itself. Shipping is a separate gated step, so you never pay extraction for a company you then discard.

**D4: Build the single source of truth first, as a proven no-op refactor.**
- Create one ordered data file, e.g. `companies.json`, covering all canonical entities. It carries an explicit priority field and a filer flag.
- Everything else is generated from it and checked by a test: `FILERS`/`UNIVERSE`, both copies of `canonical_entities.json`, and the web file.
- Replace the ~15 hand-pinned "13"s with derived counts, and add a test that fails if a literal filer count reappears.
- Gate: a $0 no-op proof (byte-identical outputs, unchanged anchor golden test, unchanged graph invariants) before any company is added.

**Order of work:**
1. Baseline snapshot.
2. D4 refactor (no-op proven).
3. Free-stage orchestrator with state file, isolation, undo and the tickers refresh.
4. MRVL parse check and promotion test.
5. Pre-register the tripwire questions.
6. Capped paid run.
7. Gates.
8. Merge.

## The One Thing to Do First

Freeze the baseline of the current 13 at $0, before touching anything:
- a golden test of the anchor outcomes for the existing multi-company questions;
- a committed per-company graph invariant snapshot (entity ids, MENTIONS and edge counts per company, resolution results);
- the current 60-question benchmark scores, taken from the last recorded eval rather than a new paid run;
- written, pre-registered tolerances for what counts as a regression.

This one artifact is the gate for both the D4 no-op refactor and every later company add. Without it, neither can be proven safe.

## Notes

- The verdict's $15 total cap for CP is the council's proposal, not an approved budget; the owner sets it in the spec.
- Decision numbers: 005 is the agent spend budget, 006 is the M2 deferral, so this record is 007.

## Owner decision

Pending (asked with the CP spec).
