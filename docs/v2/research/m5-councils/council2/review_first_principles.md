**1. Strongest: C.** It is the only response that stress-tests S2 itself. A protocol written against two non-atomic backends will leak. In-process counters must rebuild from the SvcQuery ledger, or every rolling restart resets the $10 cap. B is a close second: best sequencing (S7 first), concrete Valkey settings, and the right biggest risk (the async rewrite).

**2. Biggest blind spot: E.** It gives no biggest risk and no detection method, so question 5 is skipped. It puts S7 last despite "as early as possible". Its headline, "1,000 virtual users on one machine", conflates a performance-class staging box with the live shared-cpu-2x. That contradicts Council 1's binding staging-fleet wording and the owner's no-overclaim rule. D also misses the async-rewrite risk. D says fail closed affects "paid asks only", but the plan makes cached answers unavailable too.

**3. All five missed:**
- Restart semantics: the ledger row is written after the stream, so a stream killed by a rolling restart is never counted or reconciled. Leases, limiters and the freshness cache vanish.
- The Neo4j machine: a 1 GB shared-cpu-1x with no HA, deploy downtime, CPU burst throttling (S7 must run sustained) and page-cache pressure.
- Staging Neo4j and CPU class may differ from live, so S7 may not transfer.
- Under S2, the failure defaults for kill-switch and cache read errors are undefined.
- Security: Turnstile is stubbed in staging, so its latency and outage are untested. Changing the pepper breaks limiter and ledger correlation. The 150/day count and the $10 cap overlap, with no stated winner.
