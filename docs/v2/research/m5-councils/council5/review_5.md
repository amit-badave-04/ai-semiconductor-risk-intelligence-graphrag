# Peer review 5

**1. Strongest: D.** It is the only response that predicts the outcome and pre-registers how to read it.
- It does the arithmetic: about 2.9 cores of embedding demand against a ceiling of about 1.4 cores.
- It writes an addendum before the run and says a low embedder-calls-per-ask count means the run is invalid, not a pass.
- It spots the warm-Neo4j blind spot, which survives both (a) and (b).

A is close (arithmetic and counters) but has no addendum and misses the warm graph.

**2. Biggest blind spot: C.**
- Rotating "filing-year" or "company-context" qualifiers would trip company/alias detection and shift the retrieval distribution. That is the cost it used to reject (c), and it breaks the controlled salt.
- Its thresholds are vague ("if median overlap is high").
- It says the ledger shows cache and LRU hit rates, but a spend ledger doesn't record those.

E also errs. It claims (b) avoids the warm page cache, but low-drift salting hits the same neighbourhoods. E and B both skip the CPU arithmetic.

**3. Missed by all five:**
- **Void versus fail.** If the server saturates (429s, caps, backpressure), ledger paid asks/s falls below 95% of 2.6. The run would be declared void when it is really a failure. Pre-register that shortfalls caused by server rejections or timeouts count as FAIL, and that void applies only when the generator's offered load is short.
- **Generator validity.** One Locust box driving 1,000 users and 40 SSE streams can inflate time-to-first-event on its own. If all users share one source IP, per-IP caps and rate limits will also throttle them. That differs from production. Require generator-headroom evidence and a disclosed IP strategy.
- **Other downstream caches.** Each pool question repeats about 31 times an hour. Any cache keyed on retrieval output would hit.
