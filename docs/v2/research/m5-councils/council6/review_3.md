# Peer review 3

**1. Strongest: D.** It is the only response whose rule is complete and falsifiable. It names Fly timing thresholds, a $2 cap, and a per-flip check that the cited chunk stays in the top 8 under both models. It also says 41 questions detect only gross harm, so a pass means "no harm detected", not "equivalent". It states that the rule can only unlock the owner's choice and never ships the fix. C's point that S2 on today's embedder would be void rather than failed is the best single insight, and D should absorb it.

**2. Biggest blind spot: A.** Its option (e) proposals are wrong for S2.
- An LRU cache does nothing for a load test of uncached asks.
- Micro-batching adds queueing latency against a 1.5 s TTFB budget and barely cuts core-seconds.
- It sets no non-inferiority margin: "≥40/41" on a 40/41 baseline lets noise through.
- It suggests shipping the fix behind a flag, which treats the exception as a formality.

**3. Missed by all five:**
- **A free, high-power retrieval test.** Offline top-8 overlap between the two models over hundreds of questions costs $0 and has far more power than the paid 41. It could be registered as supporting evidence only.
- **Gate 1's sample size.** At n=60, 56/60 against 57/60 is a one-question miss with a wide confidence interval. Present the interval to the owner without changing the verdict.
- **S2's own cost.** Nobody asked whether S2 stubs the LLM. At 2.6 asks/s for 60 minutes, live answers would be a large cost the owner pays.
- **Running order.** Nobody pre-registered the owner-facing decision tree, so every outcome of the Fly timing and the benchmark maps to a fixed recommendation.
