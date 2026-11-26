# Decision 011: superseded passages in M2's retrieval legs

Date: 2026-11-26. The graph keeps a superseded annual filing's risk section retrievable, and today's excerpt search can return such a
prior-year passage with no mark. Asked whether the new M2 legs should exclude superseded passages or admit them with a mark, the owner
said: "lllm counsil it". Method: the `llm-council` skill (workflow wf_cc250722-6b5): five advisors (Sonnet), five anonymised peer
reviews (Sonnet), a chairman (Opus). Applied under the owner's instruction to use the council for this decision.

Status: **applied (option D).** With RETRIEVAL_FUSION on, every leg (company, period, keyword and the fused mention leg) keeps only
current passages (is_current = true); the rendered format is unchanged, so no regeneration is needed for this. With the flag off,
nothing changes; today's unmarked superseded excerpt is recorded as a known defect, pinned by a test, and closes when the flag becomes
the default. The fused mention leg's change lands as its own commit so it can be reverted alone.

## The measurement the council asked for (free, on G, 2026-11-26)

On the 30 dev main questions: superseded passages were 22 of 215 excerpts (10%) for today's retrieval and 36 of 221 (16%) with the
filer leg; **no hit relied only on a superseded copy** (every gold hit had a current copy among the excerpts). Current-only therefore
costs no dev hit and removes about one excerpt in six that was stale. Not yet measured: how many questions need prior-year text the
risk-diff block cannot answer (the council's fallback trigger: more than a couple lost means marked superseded excerpts become a
separate later milestone).

## Chairman's verdict (verbatim)

## Where the Council Agrees

- **Four of five advisors pick D, and all five peer reviews rank the Executor's D plan as the strongest.** With the flag on, every leg returns only current passages. That includes the fused mention leg.
- **Flag off stays byte-identical.** This is the regression guarantee, and nobody except the Expansionist is willing to break it in this milestone.
- **D needs no change to the context format.** The fingerprint stays the same, the 56 seeded answers stay valid, no paid regeneration is triggered, and no tokens are added. The $0.7532 cost against the $0.76 cap is untouched.
- **C is rejected.** It runs two policies under one flag, so you cannot tell which leg caused a result. Its failure mode is also unpredictable.
- **A mark alone does not protect.** M2a already showed that answers citing superseded text as current still happen when the mark is present. A "(superseded)" tag in the excerpt lowers the risk but does not guarantee anything.
- **The cost of D is recall on "what did last year's filing say" questions.** It must be measured, not assumed, and there must be a fallback if the loss is large.

## Where the Council Clashes

- **B versus D (the Expansionist alone).** The Expansionist argues that superseded text is the product: a longitudinal view is what a demo should win, and the diff block gives the model no citable old passage. The other four say this is unproven. Nobody has opened the diff block to check whether it carries old quotes, and nobody has counted how many questions need prior-year text. The Expansionist also understates the cost. A regeneration and an answer-level eval are two separate paid items, and B leans on a mark that M2a showed is unreliable.
- **"Fix today's mention leg now."** Everyone agrees on fixing it under the flag. The Expansionist and Review 4 float changing flag-off behaviour too, so the fix lands in a single regeneration. The majority says no: flag-off byte-identity is worth more than saving one future regeneration. Fix flag-off when the flag graduates, since you regenerate then anyway.
- **Whether flag-on still needs a validated answer path.** The Contrarian says "do not regenerate." Review 2 objects that flag-on users still deserve a validated answer path. Both hold: D adds no format change, so no regeneration is needed. But the flag cannot graduate until an answer-level eval has been run with the flag on.

## Blind Spots the Council Caught

1. **Where the filter goes.** is_current must be a pre-filter inside each leg's query, applied before that leg's top-k and before RRF. Filtering after fusion leaves the 8 slots short, distorts the RRF ranks, and quietly cuts recall.
2. **is_current must be reliable.** A filing with a missing or wrong flag would silently remove a whole company's passages. Check coverage: every company needs at least one current filing that is flagged correctly.
3. **The 10-to-17 gain was measured without the filter.** Some of those hits may be superseded passages. Re-measure with D before quoting the number.
4. **Nobody has counted the year-over-year questions.** That covers both the 30-question dev set and the diff block's actual content. "The diff block covers it" is an assumption until someone checks it against real questions.
5. **There is no fallback for year-over-year questions.** If D cannot answer one, the answer should say so or decline, not guess from current text.
6. **Cheaper provenance options were not considered.** Two examples: show the filing year inside the chunk_id token the model sees, or route by query intent (relax the filter only for year-over-year questions). Both are valid future options, and the first still changes the fingerprint. They belong in a later milestone, not this one.
7. **The guard for flag-off.** Extend the stale-citation check to flag any cited chunk whose accession is superseded, whichever leg returned it. With the flag off, it should only log, not reject or retry. Otherwise flag-off answer behaviour changes and the regression guarantee breaks.

## The Recommendation

**Pick D.** When the flag is on, apply is_current = true as a pre-filter inside every leg: company, period, keyword (BM25) and the fused mention leg. No change to the render format, no regeneration.

**Today's mention leg: change it now under the flag only.** Make the mention-leg change its own commit inside the flagged change, so it can be reverted alone. Do not change flag-off behaviour in M2. Record the unmarked superseded excerpt with the flag off as a known defect, with a ticket and a test that pins today's behaviour. Close it when the flag becomes the default and you regenerate anyway. Turn on the stale-citation logging for superseded accessions with the flag off as telemetry only.

Before merge:
- Add a fixture test where a superseded chunk ranks high in every leg. Assert that with the flag on, no excerpt after RRF is superseded and all 8 slots are filled.
- Rerun the 30-question benchmark, retrieval only.

Fallback trigger: if D loses more than a couple of year-over-year questions that the diff block cannot answer, plan marked superseded excerpts as a separate later milestone with its own budget. That would mean B on the keyword leg, or a year in the chunk_id. Do not use C.

## The One Thing to Do First

Run a free, retrieval-only measurement on the 30-question dev set before writing any filter code:
- Of the 17 hits, how many were superseded chunks?
- How many questions need prior-year text, and does the diff block actually contain a citable answer for each?
- Does is_current cover every company correctly?

If superseded hits are close to zero and the diff block covers the year-over-year questions, D costs nothing and you ship it. If not, you have the data to choose the fallback.
