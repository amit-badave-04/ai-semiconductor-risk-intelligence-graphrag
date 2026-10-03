**1. Strongest: C.** It alone raises restart semantics: in-process counters reset on every rolling deploy unless rebuilt from the durable SvcQuery ledger. It also warns that a "drop-in later" protocol will leak unless atomic reserve/reconcile contracts exist now. B is close (best build order, names the async rewrite as the biggest risk).

**2. Biggest blind spot: E.** It names no risk and no detection method. Its headline, "1,000 virtual users on one machine", overclaims: the gate runs on performance-class staging, live is shared-cpu-2x. "Atomic by construction" is wrong for the neo4j backend, and wrong in-process once the async rewrite adds await points between check and reserve. A also skips build order and biggest risk. B defers per-machine embedding and in-flight caps, yet one machine holding ~40 streams needs them.

**3. All five missed:**
- Deploy effects beyond C's ledger point: a restart zeroes per-IP windows (a free paid-window bypass), in-flight leases, the embedding cache and the job registry. Nobody specifies drain or warm-up.
- The Neo4j machine (1 GB shared-cpu-1x) is the single point of failure. S7 must also watch memory, CPU-burst throttling and retrieval contention. Fail-closed is never specified per feature for neo4j-backed state.
- Staging versus live: D notes divergence only for Valkey. Nobody asks for a run on the actual shared-cpu-2x shape.
- Security: the 150/day check-then-act overshoots under 40 concurrent streams. Turnstile is stubbed in staging, so the abuse path is untested. Nobody covers IP_HASH_PEPPER storage or rotation.
- Owner rules: deferring Valkey amends a pre-registered plan, so record it before any run.
