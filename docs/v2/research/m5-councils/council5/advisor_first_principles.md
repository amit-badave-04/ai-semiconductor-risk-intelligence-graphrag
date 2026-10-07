# First Principles Thinker

You're asking the wrong question. The test exists to find the first bottleneck under production-shaped traffic. So the real question is which realization makes the server see traffic that is indistinguishable from real uncached asks, and how you prove that.

**Pick (b), the per-ask salt.**

- **Production behavior.** A real live ask is a unique question that misses both caches, writes a cache row, and pushes an entry through the 2,000-entry LRU. Salting reproduces all of that through unmodified production code. At 2.6/s the LRU fills in about 13 minutes, so its eviction path gets exercised too.
- **Easier than reality.** Option (a) skips cache writes and disables the LRU. That removes real write load and replaces a production code path with one that never runs in production. (a) is the easier test, and it also ships dormant code in the production image.
- **Option (c).** It buys diversity you don't need at real cost. What the run needs is a miss rate and a cost per ask, and neither depends on semantic diversity.

**Safeguards, because the salt is only trustworthy if it is measured:**

1. Use server-side counters, not assumptions. Over the soak, require embedder-calls/s within 5% of ledger paid-asks/s, live-stream cache hits under 1%, and LRU hits near 0. If those fail, the run is void.
2. Do a pre-run calibration on about 300 questions. Salt format is random digits only, passed through the real normalization and validation. Require 100% acceptance, no company or alias detection change, and no length-limit trips. Report top-k retrieval overlap, and use a salt whose token count matches a typical real-question tail.
3. The cached stream is unsalted example questions that were pre-cached. Require at least 99% hits and near-zero embedding CPU from it.
4. Record the RNG seed and log cache-row growth as a result.

**Flag now, before the run.** I'm assuming performance-2x has 2 vCPUs. At 2.6 live asks/s and about 1.1 core-seconds per embedding, that is roughly 2.9 cores of demand against a 1.4-core ceiling (70% of 2). If that holds, the honest run fails the CPU gate by arithmetic. That would point to a bigger machine or an embedder moved off-box, not a shared state store. Write that interpretation into the report before you run.
