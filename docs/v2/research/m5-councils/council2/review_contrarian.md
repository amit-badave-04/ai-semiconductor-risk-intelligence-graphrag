**1. Strongest: C.** It alone sees S2's own trap: a protocol built over two non-atomic backends leaks, so atomic reserve/reconcile belongs in the contract now. It also notes that in-process counters reset on every rolling restart unless rebuilt from the SvcQuery ledger, and it gives a numeric Valkey trigger (5 asks/s). B is second: concrete order, and it names the async rewrite as the real risk.

**2. Biggest blind spot: E.** It says the in-process backend makes the spend cap "atomic by construction" and promises a "hard $10/day cap". That fails across restarts and deploy overlap. Its "1,000 users on one machine" headline also overclaims. The gate machine is performance-class and live is shared-cpu-2x, which contradicts Council 1's staging-fleet wording. It has no failure analysis. D is close behind: it names Neo4j latency as the biggest risk and ignores the async-rewrite risk.

**3. All five missed:**
- Deploy effects. Per-IP windows, slots and the embedding cache reset, so abusers get a fresh 5/10 min quota. Leases are lost.
- Neo4j machine. S7 must be sustained, not a burst, because shared-cpu-1x throttles. Service state competes with retrieval, and Neo4j is the single point of failure behind fail-closed.
- The Neo4j count-then-stream check is not atomic, so concurrent asks overshoot 150/day and $10.
- Staging load aimed at live Neo4j (B) would pollute the ledger and the paid count. Passing on performance-class hardware also says little about live.
- Abuse: one actor can burn the $10 cap and deny service to everyone. IP_HASH_PEPPER handling is unspecified.
- Owner rules: nobody pre-registered numeric S2/S7 pass/fail thresholds, which a "never relabel a failed gate" rule requires.
