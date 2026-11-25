# Decision 006: M2 (search upgrade) after M2a - build, re-scope, defer or narrow?

Date: 2026-11-25. Asked by the owner: "start work on M2? First detail it". Before detailing, three read-only research agents
re-checked the draft (docs/specs/m2-search.md) against the post-M2a code, the live package and Neo4j documentation, and M2a's
passing paid run 3 (artifacts/eval_report.m2a3-deployed.json, data/processed/eval_deployed.m2a3.jsonl). Their findings changed the
premise, so the `llm-council` skill was convened (workflow wf_a073476f-894): five advisors (Sonnet: Contrarian, First Principles,
Expansionist, Outsider, Executor) answered independently, five anonymised peer reviews (Sonnet), a chairman (Opus).

Status: **council advice; the owner decides.** INTERNAL document. Supersedes the M2 part of decision 004 only if the owner agrees.

## Facts given to the council

- M2a run 3: mechanical 41/41, judged 21/23 (v2e 17/23), citation validity 1.0, claim support 0.985. D1 and X4, decision 004's
  reasons for M2, are answered correctly. M2a fixed them with question-ranked evidence quotes on relationship lines, not by
  widening search. The structural gap remains: the excerpt query keeps a passage only if it MENTIONS an anchor company
  (src/semigraph/retrieval/retriever.py, EXCERPTS_QUERY), so a filer's own passage written as "we ..." cannot be an excerpt.
- The two judged failures, T3 and T6, are generation and diff-count problems (T6: the context says 29 reworded risk factors, the
  verified gold says 28), not retrieval.
- Of the 36 questions with known gold ids, none has its gold absent from the model's context; most gold arrives through the
  deterministic blocks (metrics, the risk-factor diff, relationships), not through the 8 excerpts.
- Only 11 questions carry passage-level gold. The questions most likely to show the gap (D2-D4, R1-R3, Q1-Q3) carry none. On the
  existing gold the plausible Success@8 wins are about 4, below the draft's smallest passing result (6-0), and those questions are
  already answered correctly.
- Stack, re-checked live: ir-measures 0.4.3 (Apache-2.0) over pytrec-eval-terrier 0.5.11 (MIT, cp313 Windows wheel) installs and
  works; ranx 0.3.21 covers metrics and RRF but pulls about 458 MB (numba, pandas) and was last released 2025-08; neo4j-graphrag
  1.24.0's hybrid retrievers still take no filters and offer no RRF; Neo4j supports `IN` lists in vector SEARCH filters from
  2026.06 (production 2026.07.1); `db.index.fulltext.queryNodes` is deprecated from 2026.10, `SEARCH ... FULLTEXT` exists from 2026.09.
- Spend: the hybrid estimate is $0.753229 against the $0.76 invariant, so new excerpts could only replace the existing 8.

Options put to the council: (A) build M2 as drafted with a $0 headroom go/no-go after the baseline; (B) re-scope M2 around new,
blind-labelled retrieval-sensitive questions; (C) defer M2 and move to CP; (D) the filer-scoped leg only, as a correctness fix,
without the ablation.

## Chairman's verdict (verbatim)

## Where the Council Agrees

All five advisors and all five peer reviews reach the same verdict: **do not build M2 now.** Take Option C, record why, and move to CP.

- **M2's premise is gone.** Decision 004 chose M2 because D1 and X4 looked like retrieval-scope defects. M2a fixed both with question-ranked evidence quotes on relationship lines, and search was never touched. A search upgrade no longer has a demonstrated defect to fix.
- **Options A and B are expensive ways to learn what you already know.** On existing gold, the plausible wins are about 4. That is below the 6-0 bar, and those questions are already answered correctly. A's headroom go/no-go would kill the build after you had paid for the labelling. B means writing questions designed to expose the gap and then showing that the fix closes it. That proves you can write such a question, not that users hit the gap.
- **Option D as a standalone step is rejected (4 of 5 advisors).** It is a live-path change on a public demo with no failing test and no measured benefit. "Byte-identical when off" protects nothing once the flag is turned on. The spend margin ($0.7532 against $0.76) leaves no room.
- **Fix T6's diff count (29 vs 28) before anything else in the queue.** It is real, cheap and visible, and it accounts for one of the only two judged failures. It is worth more than anything M2 offered.
- **Make CP the real probe of the structural gap.** Adding companies brings more of the filer's own text into the corpus. M2 is reopened only if a pre-defined retrieval miss appears.

## Where the Council Clashes

1. **Should the filer-scoped leg be folded into CP "as correctness"?** The Expansionist says yes: add a TDD test showing a "we ..." passage can become an excerpt, and skip the sign test. Everyone else, and all five reviews, say no. **The majority is right.** That test shows the mechanism works, not that answers improve. Switching the flag on is an unmeasured live-path change, and the excerpt budget forces a swap rather than an addition. The BM25 half also depends on the fulltext procedure, which is deprecated from Neo4j 2026.10. Build none of it until the reopen trigger fires.
2. **Can a solo developer label "blind"?** The Contrarian says no, so B is circular. The reviewers point out that the same objection applies to the CP tripwire. **Resolution:** perfect blindness is not possible, but pre-registration is. Write each question and its passage-level gold from the filing text, commit it, and only then run the system. A commit timestamp is enough rigor for a solo project.
3. **Should CP generate its own gold (about 5 labelled questions per company)?** The Expansionist says this gets B's benefits at near-zero marginal cost. The Executor wants a lighter 3-to-5-question tripwire. Reviews note that it adds scope to CP. **Resolution:** keep it, but small. Use 3 to 5 questions per new company, at least 2 of which target the filer's own voice. This is CP's acceptance gate, not a research project.
4. **Is the gap real, or speculative?** The Contrarian and First Principles treat "0 of 36 misses" as close to proof that the gap does not matter. The reviews correctly push back, as the next section explains. The gap is unmeasured, not disproven.

## Blind Spots the Council Caught

- **"0 retrieval misses in 36 gold questions" is partly a measurement artifact.** Most gold reaches the model through the deterministic graph blocks. Only 11 questions have passage-level gold. The questions most likely to expose the gap (D2-D4, R1-R3, Q1-Q3) have no gold at all. Every advisor used the 0/36 figure as evidence of absence. It is not. A tripwire built on the same gold has the same blind spot.
- **A tripwire needs a defined metric, a threshold, passage-level gold and trace logging before it can fire.** "Reopen if a miss appears" is not a test. Without $0 trace logging of what reaches the context, a miss cannot even be detected cheaply.
- **There is no baseline before CP.** CP changes the corpus, so a snapshot of the current state is needed first. Otherwise a later miss cannot be attributed to anything.
- **T6 may be a gold error.** Check whether the right count is 28 or 29 against the filing before writing a fix.
- **T6 is itself a live-path change.** The diff block feeds deterministic evidence, so the fix needs a $0 regression run. Its confirmatory paid run should be batched with the next paid eval rather than paid for separately. Cap the effort: if 29 vs 28 turns out to be a diff-engine semantics problem, time-box it.
- **T3 was ignored.** The Expansionist's suggestion that a T6 fix could reach 23/23 overclaims, because T3 is a separate generation failure. Triage T3 alongside T6 and promise nothing.
- **The Neo4j fulltext deprecation (2026.10)** is a standing reason not to build the BM25 leg against the current procedure. Record it in the decision.
- **Two other cheap $0 options:** a characterization test that pins down the gap (for example an xfail asserting that a filer's own unmentioning passage can be an excerpt), so it cannot be forgotten; and, when M2 is reopened, an offline replay that counts how many excerpts the filer-scoped leg would change, to size the gap without labelling.
- **CP design option:** CP's extraction could bring filer-own passages in through the graph path, as M2a did, instead of through search. That may be the cheapest structural fix of all.
- **The spend margin limits CP too.** New companies must not raise per-answer cost past the $0.76 invariant.
- **Public surfaces:** the Expansionist's "platform" and buyer story is not a public claim to make. Public surfaces stay strengths-only, buyer features were dropped, and no buyer exists.

## The Recommendation

**Option C, made rigorous: defer M2 (do not drop it), fix T6, pre-register a tripwire, then build CP.**

1. **Decision 005** (`docs/decisions/005-defer-m2.md`) supersedes 004. It should record:
   - **Why:** the D1 and X4 triggers were resolved by M2a, run 3 judged 21/23 correct, and no observed retrieval misses.
   - **The structural gap** stays known and documented.
   - **BM25 leg:** off the table until the fulltext deprecation path is settled.
   - **Reopen trigger, fixed now:** reopen M2 if 2 or more pre-registered questions have filer-own passage-level gold missing from the model's context. The pool is the 9 gap-prone questions plus CP's per-company questions. Also set a review date (for example, after the first two CP companies or about 6 weeks, whichever comes first).
2. **T6 (about 1 day, capped):**
   - Check the 28-vs-29 gold against the filing first.
   - If the code is wrong: write the failing test, fix, then do a $0 regression run.
   - Batch the paid confirmation with the next paid eval.
   - Triage T3 at the same time and decide whether it is a cheap fix or a logged one.
3. **$0 pre-CP insurance (about half a day, capped):**
   - Turn on trace logging.
   - Write passage-level gold for D2-D4, R1-R3 and Q1-Q3 from the filings, and commit it before running anything.
   - Snapshot the baseline on those questions.
   - Add the characterization test for the gap.
   - If this snapshot alone hits the trigger, M2 reopens with real evidence. A measured problem is the only legitimate way back into M2.
4. **CP:**
   - Time-box it.
   - Acceptance gate per new company: 3 to 5 pre-registered questions with passage-level gold, at least 2 of them in the filer's own voice, checked against the tripwire.
   - Design constraints: no new excerpt slots, and respect the $0.76 invariant.
   - Consider routing filer-own passages through the graph path.

Do not build the filer-scoped leg, the BM25 leg, RRF or the sign-test ablation unless the trigger fires. If it does, the draft M2 plan, with its kill rule and time-box, is the right one to run.

## The One Thing to Do First

**Write and commit decision 005 with the reopen trigger fully specified:** the metric, the threshold, the question pool, the rule that gold is written before the run, and the review date. This one step turns "skip M2" from drift into a rigorous, pre-registered deferral. Every later step (the T6 fix, the pre-CP snapshot, the CP gates) depends on it.

Note: the verdict's "decision 005 (`docs/decisions/005-defer-m2.md`)" is this record. Number 005 was already taken by the agent
spend budget decision, so the deferral, if the owner chooses it, is recorded here as 006.

## Owner decision

2026-11-25, the owner: **"Council plan (Recommended)"**. M2 is DEFERRED, not dropped. Order:
1. This record, with the reopen trigger below fixed before any new gold or trace exists.
2. T6 (the 29 vs 28 reworded count), capped at about one day: the gold is checked against the filings first, then the failing
   test, the fix and a $0 regression run; the paid confirmation is batched with the next paid eval. T3 is triaged alongside and
   either fixed cheaply or logged.
3. The $0 pre-CP snapshot (about half a day): the gold below written from the filings and committed, then traces and the
   baseline on the local graph G (snapshot snap-20260924-b1128354c4), and a characterization test for the gap.
4. CP (docs/specs/company-pipeline-and-web-search-outline.md, section A) is specced next, with the per-company gate below.

Not built unless the trigger fires: the filer-scoped vector leg, the full-text leg, RRF, the sign-test ablation. A full-text leg,
if ever built, must not rely on `db.index.fulltext.queryNodes` (deprecated from Neo4j 2026.10).

## Reopen trigger (fixed 2026-11-25, before any gold or trace for the pool exists)

- **Pool.** The 9 gap-prone benchmark questions D2, D3, D4, R1, R2, R3, Q1, Q2, Q3; plus every question CP adds as a company's
  acceptance gate (3 to 5 per new company, at least 2 of them answerable only from the company's own voice, "we ...").
- **Gold.** For each pool question, the EvidenceSpan chunk ids (`<accession>:<section>:<seq>`) a correct answer needs, written
  from the filing text alone. The gold file is committed, with its SHA-256 recorded in the commit message, BEFORE any retrieval
  trace or context is produced for those questions. It does not change afterwards; a wrong label is recorded, not edited.
- **Measure.** Each pool question is run through the production hybrid path at $0 (context only, no answer model call) on the
  local graph with the production embedder settings. A question is a **filer-own miss** if none of its gold chunk ids appears
  anywhere in the context (any block), and at least one gold chunk is filer-own: its `filer_cik` is an anchor company's CIK and
  it has no MENTIONS edge to an anchor.
- **Threshold.** M2 reopens if **2 or more** pool questions are filer-own misses (in the pre-CP snapshot, or later in any CP gate).
- **If it reopens.** docs/specs/m2-search.md is detailed and run with its kill rule and time-box; the pool's misses become part of
  its gold.
- **Review date.** After the first two CP companies, or on 2027-01-06, whichever comes first: the owner decides whether to keep
  the deferral, drop M2 or reopen it.
