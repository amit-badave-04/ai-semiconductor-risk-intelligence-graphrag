# Executor

**Recommendation: (b), in this order. Don't ship anything, don't waive anything, and don't skip S2.**

**Monday morning**
1. **Run the Fly S6 timing today.** It costs under $0.10. Use the same performance-2x class that S2 uses. Time today's model and the level-4 model with 1 and 2 concurrent embeds. If level 4 isn't at least 2.5x faster on Fly, or today's model already fits S2, the question goes away at no cost.
2. **Ask the owner for one line.** Say: "Run the approved ≤$2 benchmark as evidence only? Gate 1 stays FAILED on the record." The approval covered the benchmark, so this is only confirming that the order changed. Run it only if S6 supports the fix.
3. **Run the benchmark once, with no re-roll.**
4. **Run S2 exactly once, on whichever embedder the owner picks.** It is pre-registered, so I would not change its plan or size (that kills (d)). If today's embedder is the one live, run S2 on it and report the failure plainly.

**Decision rule (commit it before the run)**
The benchmark can never turn gate 1 into a pass. It can only make the builder eligible to *request* an exception. The request is allowed only if all of these hold:
- Correct answers are at least 40/41.
- Citation validity is exactly 1.0.
- The strict subset is at least 17/23.
- Every flip is diagnosed per question.
- No correct-to-incorrect flip traces to a changed retrieved top-8 or graph block (that would be an embedder-caused regression).
- Judge-only flips go to the owner and are not counted.

Any single miss means level 4 stays held, and the S2 failure is reported honestly. A pass means the builder presents the results and the owner decides.

**Also record this.** The top-8 overlap measure is a better-matched gate for future runs. It applies to future gates only and is never used to relabel today's fail.
