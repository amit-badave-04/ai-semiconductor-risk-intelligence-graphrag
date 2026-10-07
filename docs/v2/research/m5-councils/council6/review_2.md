# Peer review 2

**1. Strongest: D.** It puts the Fly timing first, which can end the question for under $0.10. It names the real stake: the fix buys a claim, not a need, since live traffic is a few asks per hour. It forbids redefining gate 1 after the fact. Its rule is also the most complete: no regression, cited chunk still in the top 8 under both models, cost cap, and Fly thresholds. It states that 41 questions show "no harm detected", not "equivalent". C's point that S2 on today's embedder would be void rather than failed is the one thing D lacks. Fold that in.

**2. Biggest blind spot: A.** Its cache and micro-batching ideas don't help S2. S2 uses uncached asks, so a cache is irrelevant. Micro-batching adds queueing delay against a 1.5 s TTFB budget. Padding changes also alter int8 numerics, which would re-trigger gate 1. A also lists the benchmark rule after proposing new code paths, so the order is backwards.

**3. What all five missed:**
- **Run the paid benchmark only where context changed.** If a question's retrieved top-8 and graph blocks are identical under both embedders, its answer cannot flip because of the embedder. Reuse the baseline for those. Paid calls then go only to questions with changed context, which cuts cost and removes generation noise. Computing this is free.
- **No noise control.** Without a baseline-vs-baseline noise estimate, none of the rules can attribute a flip to the embedder.
- **Owner sign-off on the rule text before the run.** Otherwise the builder is pre-negotiating its own exception.
- **Check whether the four changed top-1 questions are in the 41-question benchmark.**
