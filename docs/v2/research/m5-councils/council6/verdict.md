# Council 6 verdict

## Where the Council Agrees

- All five advisors pick (b). Nothing ships, nothing is waived, and gate 1 stays FAILED on the record. The benchmark can only qualify the builder to ask for an exception. The owner decides.
- The Fly timing (S6, under $0.10) goes first. It is the cheapest way to end the question.
- 41 questions can detect only gross harm, so a pass means "no harm detected", not "equivalent".
- Top-8 overlap against fp32 is a better measure, but only for future embedder swaps. It must never rescue this result.

## Where the Council Clashes

- **S2 on today's embedder: FAIL or VOID?** The Contrarian is right under the current wording, and council 5 found the same thing. A 2-core machine cannot reach 95% of 2.6 asks/s, so the void rule fires. If the owner signs council 5's void/fail clarification, today's embedder produces an honest FAIL instead, reported with the maximum sustained asks/s.
- **Speed thresholds.** The advisors proposed 2.5x, under 0.5 s and under 0.6 s. None of these is gate 3. Gate 3 as pre-registered (§2.1) is: patched beats unpatched by median on both shared-cpu-2x and performance-2x, over at least 50 interleaved calls. Whether S2 is feasible is a separate question about absolute CPU.
- **Option (e) is rejected on the data.**
  - The cache does not matter, because S2 asks are uncached.
  - Today's model has a 1.04 s floor even at 4 tokens: 98% of the time is in MatMulNBits, which suggests the weights are dequantized on every call. Trimming tokens cannot fix that.
  - One 1.22 s embed already uses 81% of the 1.5 s time-to-first-event budget, so no batching scheme can meet the p95 target.
  - Prefix-KV reuse was already tested and failed.
- **Option (d).** It is the fallback after the owner declines an exception, run as a new pre-registered run. It is not a substitute for the decision.

## Blind Spots the Council Caught

1. **Gate 2 already has a pre-registered rule.** It says: no headline metric more than 2 points below the production baseline, every flip read and explained, a miss blocks the fix, never re-rolled. At n=41 and n=23, a single net loss fails it. The advisors' "≥40/41, ≥17/23" is that rule restated. A parallel new rule would be relabelling.
2. **The v2e baseline's embedder is UNVERIFIED** (build plan line 88: "likely fp32 torch"). The plan already offers a paired unpatched arm (about +$0.9, still ≤ $2 in total). That arm is the true production baseline and the noise floor the reviewers asked for. It needs the owner's yes, because decision 4 says one run.
3. **All four changed top-1 questions are in benchmark.json.** The paid run tests exactly them.
4. **Level 4 is not shown to make S2 feasible (derived).** The CPU budget is 1.4 core-s/s (70% of 2 cores).
   - 0.316 s on the desktop × 1.19 for a slower Fly core ≈ 0.38 s per embed.
   - Council 5's per-ask salt adds about 20%, so about 0.45 s.
   - At 2.6 asks/s that is about 1.17 core-s/s, which leaves about 0.23 core-s/s for Neo4j, SSE and prompt building.
5. **Use the free context diff for attribution only.** Do not use it to shrink the pre-registered 41-question run.
6. **S2 cost is not a worry.** S2 uses the mock LLM and is quoted at about $1.30.
7. **S6 is not approval-free.** Each staging window is quoted to the owner before it runs.

Also rejected: running S2 on level 4 in staging while live stays on level 0. The plan runs S2 on the embedder that will ship.

## The Recommendation

**Order**
1. Send the owner one message containing all the asks.
2. Run S6 on both machine classes.
3. Apply R1 below. Run the benchmark once only if R1 passes.
4. Report the results; the owner decides.
   - If the owner grants an exception: ship level 4 and run S2 on it.
   - If not: run S2 on today's embedder and report it as FAIL under the clarified rule. Then run (d) as a separately pre-registered run, if the owner wants the capacity answer.

**Pre-registered rule.** Commit it, and have the owner ratify it, before S6 runs.
- **R0.** Gate 1 stays FAILED. Nothing below turns it into a pass.
- **R1 (whether to spend the $2; not a gate).** Gate 3 passes as written. In addition, the predicted S2 embed load must be ≤ 1.1 core-s/s, or about ≤ 0.42 s per embed.
  - Predicted load = 2.6 × the patched model's median per embed, on performance-2x, using salt-format questions.
  - If R1 fails, do not run the benchmark. Recommend (a) or (d).
  - The 0.3 core-s/s reserve for everything outside embedding is an assumption.
- **R2 (gate 2, unchanged).** It is judged against v2e and, if approved, against the paired level-0 arm. A miss against either one blocks the fix. A pass means all of:
  - correct ≥ 40/41;
  - strict subset ≥ 17/23;
  - citation validity ≥ 0.98.
- **R3 (qualifies an exception request).** All of these must hold:
  - R2 passes.
  - Every flip is diagnosed with `scripts/compare_retrieval.py`.
  - No correct-to-wrong flip occurs on a question whose top-8 chunks or graph blocks differ between the two models.
  - All four changed-top-1 questions are read and reported, whether or not they flipped.
  - Flips on questions with identical context count as LLM or judge noise.
  - Judge-only flips go to the owner.
- **R4.** Any miss means the fix stays held and S2 runs on today's embedder.
- **R5.** A pass is reported as "no harm detected at n=41". The owner decides.

## The One Thing to Do First

Send the owner one decision-14 message containing:
- the S6 quote (under $0.10);
- R0 to R5 for ratification;
- a yes or no on the paired arm (about +$0.9);
- council 5's void/fail clarification.

Run S6 as soon as the owner approves it. The desktop-derived estimate (about 0.45 s per salted embed) already sits on the wrong side of R1, so this $0.10 run may settle the question.
