# 003: Evidence highlight, show or not (2026-11-11)

Context: docs/specs/m5b-redesign.md RD6 and RD18. The exact-quote highlight in the evidence panel marks words only when an
answer puts a quotation in quotation marks right before its chip and those words appear in the passage exactly. Measured
before R2: 5 of 108 chunk chips in 53 saved answers (2 answers), and 13/178 and 17/212 in two eval runs, so about 93% of
panels show no highlight. The first-screen sample "What an answer looks like" (RD6) showed a highlight and its legend, which
promised newcomers something real answers rarely show.

The owner asked for an llm-council on show or no show (workflow wf_cd477044-14f: five Sonnet advisors, five anonymous Sonnet
peer reviews, an Opus chairman). Options put to it:
- A, keep as is;
- B, remove the highlight;
- C, change the answer prompt to quote (paid evals);
- D, keep the highlight but make the sample honest;
- E, add a "paraphrased" line to panels with no quote (new limit-stating wording).

## Verdict: D, plain form

**Changes made:**
- The RD6 sample keeps the sentence, its stamp, the full cited passage and its source line, with no `<mark>` and no legend.
- The evidence panel (RD18) is unchanged. It marks words only on an exact quote and shows the legend only beside a mark.
- A panel with no exact quote shows nothing extra.

**What it does not change:**
- No new public wording.
- No paid run.
- No fuzzy marking: the abuse case stays.
- It is one commit to reverse.

**Why not the others:**
- A keeps a first-screen promise the site rarely keeps.
- B deletes the only exact-copy grounding signal, which is working and spec-protected code.
- C needs paid evals and owner approval of a cost, and may change answer length and quality just before sign-off.
- E adds limit-stating wording to most panels and makes the common case look like a failure.

**Where the council split:** some advisors wanted the sample to keep a caption, "When an answer quotes a source word for
word, those words are highlighted". The chairman ruled against it: the panel's own legend explains each highlight where it
appears, and the caption would be new first-screen copy.

## Follow-ups (backlog, not part of this deploy)

1. **A free check of the matcher.** On the 53 saved answers, check whether quotation-mark styles, a quote not directly
   before the chip, or the 20-character floor cause misses. Any fix stays exact: the answer's quotation marks may be
   normalised, but the passage matching may not be loosened.
2. **Re-measure the hit rate in production.** If highlights should become common, C becomes its own spec, with eval cases, a
   threshold for length and quality, and a dollar figure for the owner to approve.
3. **The NVDA pass.** It checks whether a highlight and its legend are announced (guide row Ask-7b).
4. **The first-visitor rerun.** It shows whether the plain sample leaves anyone confused. If it does, choose between the
   caption on the Method page and B.

The RD6 change is recorded as a deviation in the spec's R4 Status entry.
