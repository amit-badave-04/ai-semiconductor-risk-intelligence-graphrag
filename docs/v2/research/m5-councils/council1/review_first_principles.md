**1. Strongest: A.** It reframes "live == proven" as the wrong question. The per-IP window (5 asks/10 min) and the 150/day ceiling make the gate load unreachable on the live site, so B and C buy a property the policy forbids. A's gate wording is the most precise, and its risk is right: the 19% Fly slowdown comes from document embedding only (brief item 7), so the proof may not transfer. It also rehearses the scale-up drill. C is a close second (ordered pass/fail steps, a CI parity test).

**2. Biggest blind spot: E.** It recommends B outright and calls +$51.53/month "a modest dinner". That ignores the owner rule that new recurring cost needs an explicit decision. Its proposed sales line, "the machine serving you right now was load-tested at 1,000 virtual users", is the overclaim the brief warns against, because the caps and the mock LLM still apply. "Every buyer click" is also overstated: 45% of asks are cached and skip embedding, and pool questions hit the embedding cache. D also picks B, but it at least polices the wording.

**Checked arithmetic:** the $51.53 delta, C's 2.6x, B's ~0.09 core-s/s, D's "about a minute", 17% and ~4x are all correct. Unsupported: B's "Neo4j is the likelier bottleneck" and "soak drains burst" (the refill rate is unpublished). C says 2.6x but the 70% gate threshold is 0.0875 core-s/s, so the true ratio is about 3.8x.

**3. All missed:**
- A middle option: scale live up only for scheduled buyer-demo windows, with no recurring cost.
- Memory and process model: ~1 GB RSS per process, and one uvicorn process uses one core, so performance-2x may buy nothing.
- A cold embedding cache after each deploy or soak start, which could break the p95 1.5 s gate.
- Rewording the owner's recorded "1,000 concurrent users" decision is the owner's call, and none said to ask.
