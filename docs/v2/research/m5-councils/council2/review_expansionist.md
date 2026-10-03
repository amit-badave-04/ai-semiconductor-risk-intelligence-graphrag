**1. Strongest: C.** It is the only one that sees S2's trap: a protocol built over two non-atomic backends will leak, so put atomic reserve/reconcile and contract tests in now. It also catches the restart hazard: in-process counters must rebuild from the SvcQuery ledger at boot, or every rolling restart resets the $10 cap. B is a close second for its concrete order and checks.

**2. Biggest blind spot: E.** Its "1,000 users on one machine with a hard $10 cap" headline contradicts Council 1 (a staging-fleet claim on performance-class machines, live being shared-cpu-2x) and the owner's never-overclaim rule. It gives no biggest risk (Q5) and treats in-process atomicity as free, ignoring restarts. A skips Q4 and Q5 entirely.

**3. Missed by all five:**
- **Staging contamination:** staging with raised caps and stubbed Turnstile could write to the live Neo4j. That would pollute the SvcQuery ledger behind the all-time cost figures and could trip live's 150/day ceiling or kill switch. B's "real 1 GB Neo4j" makes this concrete. Staging needs its own graph or tagged rows.
- **Restarts and deploys:** per-IP windows (5 per 10 min) reset on every deploy, giving an abuse window. In-flight reservations are lost on a crash. The cached freshness result also resets.
- **Neo4j box:** a shared-cpu-1x is burst-throttled, so a short S7 run may flatter it. Nobody costed upsizing Neo4j against Valkey.
- **Denial of wallet:** a botnet can burn the $10 cap and pause paid answers for everyone.
- **Pre-registration:** the S7/S2 flip thresholds ("degrading at 10-20 asks/s") are vague. Set numeric pass criteria before running, per the owner's failed-gate rule.
