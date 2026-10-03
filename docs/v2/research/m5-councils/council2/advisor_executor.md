S2, with S7 pulled to the front.

Monday morning, in order:
1. Async rewrite of the ask path plus the embedder fix (both already binding). Check: 40 concurrent streams locally without thread-pool starvation.
2. `StateBackend` protocol with in-process and neo4j backends only. Check: existing ask and limiter tests pass unchanged on both.
3. S7 on staging at 5/10/20 asks/s against the real 1 GB Neo4j, with the ~5 retrieval reads per live ask included, since service state (about 12 reads/s, 9 writes/s) shares that box. Check: p95 of the three pre-stream reads.
4. S2 on one performance-class machine, cold, load generator in `sin`.

S1 and S3 have no evidence-driven first step: they commit to three backends, four Lua scripts, a volume machine and about $8.65 a month (owner approval needed) before anything is measured. S3 also adds a vendor and an engine that is not Valkey. S4 as "never" is wrong if S7 fails.

Flip to Valkey if S2 shows one machine cannot pass, S7 shows Neo4j degrading at 10-20 asks/s, or the owner wants a multi-machine live fleet. Then: self-hosted Valkey 9.1.2 or later, `limits` with `valkey://`, volume at /data with `--appendonly yes`, explicit socket_timeout, DNS retry, and run our Lua and streams on it before trusting it (assumptions: appendfsync everysec, noeviction).

Policy: accept the $10/day cap and three-level kill switch. Accept HMAC `IP_HASH_PEPPER` and /64 bucketing now; it needs no store. Defer the shared Langfuse salt, fleet in-flight cap and per-machine embedding slots until a second machine exists. Fail closed for paid answers if a shared store is ever used.

Biggest risk: the async rewrite does not let one machine hold ~40 streams. Detect it at step 1.

Recommendation: S2, running S7 and the one-machine S2 spike before any Valkey code exists.
