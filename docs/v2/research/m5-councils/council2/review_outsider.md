**1. Strongest: C.** It alone sees that S2 is not free. A protocol built against two non-atomic backends will leak, so atomic reserve/reconcile contract tests belong in it now. It also notes that in-process counters must rebuild from the SvcQuery ledger at boot, or every rolling restart resets the $10 cap. It gives a numeric Valkey trigger (5 asks/s). B is runner-up for concrete checks.

**2. Biggest blind spot: E.** It is advocacy. It sells "1,000 virtual users on one machine" as a headline, which contradicts Council 1's binding wording: a staging-fleet claim, with live on shared-cpu-2x and staging on performance-class machines. "Atomic by construction" ignores restarts. The freed-worker benefit is its own unsupported assumption.

**3. All five missed:**
- Staging uses raised caps and stubbed Turnstile, so the gate never exercises the metering path it is meant to validate. The paid-today count scans the day's SvcQuery rows, which grow under raised caps, so S7 is biased.
- A cheaper fix than Valkey: cache the kill-switch policy and keep the daily count in memory, which cuts the three pre-stream reads. Neo4j itself (1 GB, single, no HA, shared with retrieval) is the real bottleneck candidate.
- A rolling deploy briefly overlaps old and new machines, so limiters and in-flight caps double. The caches, job registry and freshness result are lost, and streams drop.
- Abuse: three Neo4j reads happen before Turnstile, which is an amplification path. Attackers can burn the $10 cap to pause the site. Client-IP trust behind Fly's proxy is unexamined. Introducing the pepper breaks continuity of existing hashed identifiers.
- No numeric pre-registered flip thresholds (the no-relabel rule). Staging fleet cost also needs owner approval.
