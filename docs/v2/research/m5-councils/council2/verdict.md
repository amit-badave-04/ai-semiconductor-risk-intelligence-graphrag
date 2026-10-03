## Where the Council Agrees
Unanimous for Defer: live is one machine, where process-local state is correct; Neo4j's limit is unmeasured; Valkey adds an unreplicated failure point with deploy downtime and $8.65/30 days before evidence; Upstash adds a vendor with 2026 incidents. Tested against Plan-now's best case, atomic metering: it needs no Valkey (in-process critical section; Neo4j counter proven by test). Consensus holds.

## Where the Council Clashes
- Embedding slots, per-machine in-flight cap: Executor defers; its reviewer wins (~40 streams need them).
- "Fail closed for paid only": wrong; cached answers live in the same store.
- "1,000 users on one machine" (Expansionist): rejected; breaks Council 1's staging-fleet wording.

## Blind Spots the Council Caught
Accepted, all fixed below: uncounted restart-killed streams and reset windows; check-then-act cap overshoot; staging polluting the live ledger; burst-flattered S7 on the wrong shape; Neo4j as the real single point; denial of wallet, pre-Turnstile reads, client-IP trust, pepper continuity; stubbed Turnstile (stated untested); vague, unregistered triggers. Rejected while deploys stay one-machine restarts: limiter doubling. Out of scope (Council 1): a shared-cpu-2x API run. Added: Valkey moves service state, never the ~5 retrieval reads per live ask.

## The Recommendation
**(a) Architecture.** Now: async rewrite and embedder fix (binding); `StateBackend` protocol; in-process backend for live (counters in memory, durable rows in Neo4j); neo4j backend (per-day counter; rollback, any multi-machine staging). Deferred: Valkey, Lua, job streams, fleet cap, sweeper lock.

*S7 (pre-registered):* Neo4j-only replay harness in `sin` (no API or LLM) against a staging clone of live (shared-cpu-1x, 1 GB). Levels: 5/10/20 live asks/s plus cached at the gate ratio (~0.8 per live). M5a op mix (live: 1 cache read, 1 reserve write, ~5 retrieval reads, 2 writes after); today's as control. Each level: 60 min after a burst-draining soak (120 min if drain is not visible; burst length assumed), judged on the last 30. Pass: pre-stream state p95 ≤50 ms, p99 ≤200 ms; writes p95 ≤100 ms; retrieval p95 ≤1.5× its own at 0.5 live asks/s; errors ≤0.1%; zero lost ledger rows; no Neo4j restart or OOM. M5a needs 5 (~1.9× the gate's 2.6); 10 and 20 chart capacity.

*S2 (pre-registered):* one performance-class machine (size fixed beforehand), cold, `sin` generator, full load model, ≥60 min. Pass: the gate's PLAN.md criteria, ~40 streams without pool starvation, zero lost ledger rows, zero overshoot in a 10-min sub-run with the cap below demand. A fail stays a fail; a bigger size is a new run.

*Valkey triggers* (to the owner): T1, S2 fails on one machine and S7's state limits fail at 5 while retrieval passes (if S7 passes, a fleet uses the neo4j backend; if retrieval fails, price a bigger Neo4j first); T2, the owner wants a multi-machine live fleet.

**(b) Contract.** `reserve` atomically checks kill switch, 150/day, $10, per-IP and in-flight caps, increments, and writes a durable `reserved` ledger row before returning a lease; failure rolls back and denies. Contract tests, every backend: 200 concurrent reserves against 150 grant exactly 150; double reconcile charges once; kill-then-boot leaves counters equal to the ledger. Boot rebuilds count, spend, per-IP daily counts and paid windows from the ledger (rows need the hashed IP), charging pre-boot `reserved` rows (killed streams, dead leases) their estimate. Other windows and caches reset; killed upload jobs show "interrupted"; freshness shows "not checked since restart". SIGTERM drain: refuse new paid asks and uploads; finish streams within kill_timeout 300 s.

**(c) Neo4j unreachable, or a state op over 1 s:** paid asks fail closed (503, paused copy); cached answers unavailable, never falling through to paid; kill switch cached (10 s refresh, admin flip immediate), paid off when stale over 30 s or at boot until read; failed reconciles retried, else charged estimate at boot; uploads refused; reads 503.

**(d) Policy.** Both caps bind; whichever trips first pauses paid asks for the day. Accept the $10 cap, the kill switch, and charging an expired lease its estimate, set as a per-ask-type upper bound (agent separate). Pepper: HMAC-SHA256, Fly secret, version id per row; rotate only on exposure (it resets windows, breaks correlation). Accept IPv6 /64; trust only `Fly-Client-IP` (assumed; verify in code). Defer the shared Langfuse salt (one machine). Embedding slots: 1 live, ≥2 staging (derived: 2.6×0.31 s ≈ 0.81 core-s/s at desktop speed). In-flight: live 2 until S2 measures per-stream memory; staging above ~40.

**(e) Abuse.** Before Turnstile only the answer-cache read remains (10 reads/s process budget). A per-IP daily paid cap of 20 forces ≥8 Turnstile-passing IPs to drain 150; worst case: a pause, never spend above $10. Staging: own app, Neo4j and secrets; it refuses to boot against live's Neo4j host; live refuses stubbed Turnstile or raised caps.

**(f) Order.** 0 commit the addendum; 1 S7 in week 1, parallel with 2; 2 async rewrite and embedder fix (check: 40 local streams, stub LLM); 3 protocol, backends, contract tests, boot rebuild, drain; 4 S2; 5 apply triggers.

## The One Thing to Do First
Commit the M5a addendum (Valkey deferral and why, S7/S2 limits, triggers) and put it to the owner before any spike or state code; otherwise the failed-gate rule has nothing to hold.

## Decisions for the Owner
1. Approve the M5a amendment record: deferring sections 4.1-4.5's Valkey-first design (one live machine; Neo4j unmeasured), plus reserve-before-stream rows, boot rebuild, kill-switch caching, per-IP daily cap, spike limits and triggers. Yes; saves $8.65/30 days; blocks everything.
2. New secret `IP_HASH_PEPPER`, no cost: yes. Null any stored unsalted hashes (IPv4 brute-forceable; irreversible): yes. Blocks only the cutover.
3. Confirm $10/day alongside 150/day; worst case ~$300/30 days (derived). Blocks metering go-live.
4. Staging spend, unless Council 1 covered it: Neo4j clone ~$0.012/h (derived); performance machine and S2 LLM spend quoted before the run. Blocks S7, S2.
5. Valkey ($8.65/30 days) only when a trigger fires.

**Decided by the council, no owner approval needed:** rejecting Upstash and Neo4j-only-as-never; contract tests; drain; failure-policy detail; staging boot guards; build order; measured cap values; the Expansionist's headline.
