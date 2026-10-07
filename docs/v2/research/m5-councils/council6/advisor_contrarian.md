# Contrarian

Recommend (b), but only after fixing four holes in it.

1. **The council's headline weakens option (a).** Running S2 on today's embedder cannot produce a FAIL. The ledger rate falls below 2.47 asks/s and the void rule fires, so S2 yields nothing usable. Option (a) is therefore "S2 is unmeasurable", not "S2 fails honestly".

2. **Gate 1 is probably mis-specified.** Top-1 identity is brittle when all 4 changes are near-ties. Against fp32 the patched model is worse on top-1, yet slightly better on top-8 overlap. The answer path uses 8 chunks plus graph blocks. Even so, a measure picked after seeing the numbers is exactly what the owner's rule forbids. Do not swap it. Report gate 1 as FAILED and give the owner the top-8 evidence.

3. **The benchmark cannot discriminate.** It has 41 questions with a 40/41 baseline. One judge flip is 2.4 points, so the benchmark is noise. A decision rule written on the aggregate score would be gamed by the noise.

4. **The 0.32 s speedup is desktop-only.** Fly's core speed, the ONNX thread count, and the 1.5 s TTFB budget are all unverified.

**Order**
1. Run the Fly S6 timing first (<$0.10) on both models. If level 4 isn't at least 2.5x faster on Fly, the fix is moot and the benchmark money is saved.
2. Run the benchmark only if step 1 passes, and only with the owner's explicit go.
3. Ask the owner for an exception, which the builder never grants itself.

**Rule, written before any run**
- **Pass:** zero new correctness losses on the diagnosed flips, citation validity stays 1.0, the strict subset stays at or above 17/23 or any drop is traced to a judge-only flip, and the Fly speedup is at least 2.5x.
- **Fail:** any non-judge regression, or any retrieval-caused flip.
- **A pass is not a waiver.** It only qualifies the exception request. The owner decides.

**Option (d) is the safe fallback.** A bigger S2 machine is a new run, which the pre-registration already allows, and it changes no retrieval.
