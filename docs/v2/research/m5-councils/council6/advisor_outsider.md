# Outsider

**Recommendation: (b), in this order.**

1. **Run the Fly timing (S6, under $0.10) first.** The whole case rests on "3.96x faster", and that was measured only on the owner's desktop. If Fly shows less than about 2.5x, or the median embed is still above about 0.6 s, the fix can't rescue S2 and you can stop. That would also save the $2.
2. **Ask the owner for a go on the benchmark.** Frame it as evidence for an exception request, not a gate re-run. Record gate 1 as FAILED, permanently.
3. **The owner then decides.** If they decline, go to (a): run S2 on today's embedder and report the predicted failure. Or use (d) as a new, labelled run.

**What the question should expose:** gate 1 measures the wrong thing. It compares the new model to today's model, which isn't the truth. Today's model isn't the reference, and the answer path uses the top 8 chunks, not the top 1. All four changed top-1s were near-ties. The builder can't re-define the gate after seeing results, because that is the exact relabelling the owner forbids. The builder can, however, tell the owner that the gate measured the wrong quantity and propose a better measure for future gates only.

**Also:** the live site gets a few asks per hour, so a 1.2 s embed is fine today. The 1,000-user test is for a claim, not a need. Don't spend $2 and a rule exception to buy a claim.

**Pre-registered benchmark rule.** Write this before running. The rule can only ever unlock the owner's choice. It can never ship the fix.
- Recommend an exception only if all of these hold:
  - correct is at least 40/41;
  - the strict subset is at least 17/23;
  - citation validity is 1.0;
  - every correct-to-wrong flip has its cited chunk still in the top 8 under both models;
  - cost is $2 or less;
  - Fly timing meets the step 1 thresholds.
- Anything else means the fix stays held.
- Judge-only flips go to the owner and are not counted.
- No re-rolls.
- State up front that 41 questions can detect only gross harm. A pass means "no harm detected", not "equivalent".
