# Expansionist

**Recommend (b), sequenced so the cheap, upside-heavy steps come first. I'd also add a speed path that skips gate 1 entirely.**

1. **Fly timing now (<$0.10, no approval needed).** Time both models on the 2-core machine. If today's model is slower than the plan's 19% assumption, S2 is dead on the old embedder, and that is the strongest case for the fix. If level 4 gives about 0.35 s there, the "1,000 users on one machine" claim becomes possible. That claim is the best public-demo credential this project can get, and it may let you stay on the small machine, which saves the $9-52/month.

2. **Build the speed paths that don't touch retrieval (option e).** None of them triggers gate 1. They stack with level 4 or replace it:
   - Micro-batch concurrent query embeds. One batched forward pass of 3-4 short queries costs far less than 3-4 separate passes.
   - Cap the sequence length and remove padding for short queries.
   - Add an LRU cache keyed on the normalized question.
   Test each against the same gates. If batching alone gets S2 to about 1.5 core-s/s, the exception question goes away.

3. **Ask the owner for the $2 benchmark go**, with the rule below written into the repo first.

**Pre-registered rule (committed before the run, no re-rolls):**
- Recommend an exception only if all of these hold:
  - correct is 40/41 or better,
  - citation validity is 1.0,
  - the strict subset is 17/23 or better,
  - no flip to wrong is traced to a missing top-8 chunk.
- Flips that only the judge sees go to the owner, not to the builder.
- Any flip to wrong traced to retrieval means the fix stays held.
- Gate 1 stays recorded as FAILED. A pass on the benchmark is evidence for the owner's exception, not a relabelled pass.

**If the owner grants the exception:** ship it behind a flag so rollback is one secret change. For future embedder swaps, pre-register top-8 overlap against fp32 as the gate measure. That measure matches how the answer path uses retrieval, and it applies only to later swaps, not to this one.
