# Peer review 1

**1. Strongest: D.** It is the only response that:
- Names the real asymmetry: the fix buys a claim, not a need, and real traffic is a few asks per hour.
- Gives a complete rule: cost cap, Fly thresholds, "cited chunk still in the top 8 under both models", and no re-rolls.
- States that 41 questions detect only gross harm.

C's point that S2 on today's embedder goes void rather than fails should be merged in. C's rule is looser, though: a drop in the strict subset is excused if it is "traced to a judge-only flip".

**2. Biggest blind spot: A.**
- It adds unregistered scope: micro-batching, sequence caps, and an LRU cache.
- The cache is irrelevant to S2, which is defined as uncached asks.
- Batching adds queueing latency against a 1.5 s TTFB budget.
- It frames a bad Fly result for today's model as "the strongest case for the fix", which is motivated reasoning.

**3. What all five missed:**
- **A free pre-test.** Diff the full pre-LLM context (top-8 plus graph blocks) between the two models across all 41 benchmark questions and the 60 gate-1 questions. A question whose context is unchanged can only flip through LLM or judge noise, so only the changed-context questions need paid runs. That gives more power at lower cost.
- **A noise floor.** "Never re-rolled" means the rule never measures baseline flip variance. Flips should count as embedder-caused only when the retrieved context differs.
- **The owner signs off on the rule itself.** One combined, conditional approval (Fly timing, then benchmark) is better than separate asks.
- **Staging versus shipping.** S2 could run level 4 on the temporary machine as a labelled measurement. That keeps production on level 0 and limits any claim to "with level 4".
