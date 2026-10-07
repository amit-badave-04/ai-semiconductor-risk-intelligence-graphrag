# Peer review 5

**1. Strongest: D.** It is the only one that covers all three asks. The order is Fly timing, then the owner's go, then the owner decides. It fixes the benchmark rule before the run and ties it to the Fly thresholds. Its flip test is the sharpest: the cited chunk must stay in the top 8 under both models. It admits that 41 questions detect only gross harm. It also gives a fallback if the owner declines. Borrow C's point that S2 on today's embedder ends void rather than failing, and E's free check for padding or a long prefix.

**2. Biggest blind spot: A.** Its speed paths don't help S2:
- S2 uses uncached asks, so an LRU cache does nothing for it.
- Micro-batching adds queue wait against a 1.5 s TTFB budget with only 0.27 s of slack.
- Truncation and padding changes can alter retrieval.
- Re-testing each path against the gates is unplanned scope creep.
- Its rule has no Fly speedup threshold and no power caveat.

**3. Missed by all five:**
- Run a free, deterministic retrieval diff on the 41 benchmark questions before the paid run. Only questions whose top-8 or graph context changed can flip, so this may cut the cost and sharpen the test.
- Nobody checked that S2 passes even with level 4. A CPU limit of 70% on 2 cores is 1.4 cores. Embedding alone is about 1.0 core-s/s, which leaves little for the app, SSE and the rest. Extend the S6 timing to run at 2.6 asks/s and record CPU before spending $2.
- The rule must fix the noise floor. A 40/41 baseline with a 2.4-point step means "no flip" is the only meaningful pass, so say so in the rule.
- The owner may amend S2's pre-registration before any run. That is a legitimate decision and not a waiver.
