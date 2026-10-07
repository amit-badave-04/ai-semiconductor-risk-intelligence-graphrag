# Council 5 verdict

## Where the Council Agrees

All five advisors choose **(b), the per-ask salt**, and reject (c) because it costs money and shifts retrieval. The code shows a stronger reason to reject (a) than the advisors gave. "Dead code ships" is a weak objection, because the image already carries staging paths guarded by validators (`X-Test-Client-IP`, the Turnstile stub, origin auth). The real problems are these:
- (a) skips the Neo4j answer-cache write on every live ask, which makes the test easier than real traffic.
- (a) needs server changes. (b) needs none.

(b) also extends a mechanism the plan already pre-registers: M5_PLAN §6 includes a 10% "live unique suffix" population.

## Where the Council Clashes

- **Salt form.** The year detectors in `retriever._YEAR` and the router (`\b(19|20)\d\d\b`) match a standalone 4-digit number. A counter without padding would pass through 1900-2099 and change routing and retrieval. The Expansionist's rotating year and company clauses hit the same code paths, so they are rejected. Use a fixed-width, zero-padded counter. That makes the collision argument moot, and E's 4-5% figure was wrong anyway: it is about 1-2 collisions per hour.
- **Normalization.** `cache_key` lowercases, collapses whitespace and strips trailing `?.! ` only. An appended `(ref N)` survives.
- **Counters.** `LimitedEmbedder` has no hit or miss counters, so Reviewer 3 is right. Proof must come from independent oracles, with no server change.
- **"The salt is conservative."** This holds only for the unpatched embedder, which has a ~1 s floor, so about 7 extra tokens add about 3%. The patched model costs about 9 ms per token, so the same salt adds about 20%. Measure the delta and judge the CPU gate on the raw number.
- **Warm Neo4j.** This is an inference: it is probably moot, because the live site searches about 3,152 vectors and production is just as warm. Report store size against page cache, but do not gate on it. E is wrong that (b) avoids the warm page cache.

## Blind Spots the Council Caught

1. **Headline (A and D half-saw it).** M5_DECISIONS §1.1 gives the shipped embedder 1.22 s per question on one thread. At 2.6 asks/s that is about 3.2 core-s/s, and M5_PLAN records the same figure. A performance-2x has 2 cores, so it cannot physically embed 2.6/s: the ceiling is about 1.6/s, and lower if Fly cores are slower. The ledger rate then falls far below 2.47 and the void rule fires. **On today's embedder, an honest S2 can only end VOID, never FAIL.** This outranks item P.
2. **Void versus fail (Reviewer 5).** The void rule came from review, not from the original pre-registration, so the owner should sign off on this clarification:
   - VOID only if the generator's offered live rate is below 95% of 2.6, the generator exceeds 70% CPU, or an offline check failed.
   - A shortfall the server causes (shedding, queueing, timeouts) is a FAIL, reported with the maximum sustained live asks/s.
3. **The plan's own sizing assumed warm embedding** ("~1.1 warm, pool repeats hit qemb"). Salting is stricter than the plan, and the owner must be told.
4. **Cold-start contamination (Reviewers 1-3).**
5. **Length.** `max_question_chars` is 500.

The build plan already covers per-VU IPs, the generator CPU void and the lost-row definition. These are not blind spots.

## The Recommendation

Use **(b)**. Salt every live ask (pool and agent). Leave the 10% unique-suffix population as it is, and send the cached examples unsalted.

- **Format:** `f"{q} (ref {worker}{n:06d})"`, meaning a worker digit plus a 6-digit zero-padded counter. It is deterministic and logged with the run-id.
- **Offline checks, before staging (no contamination):**
  - Run every pool and agent question with 3 salts through `validate_question`, company/alias detection, year detection and the router. Decisions must be identical, and length must stay at or below 500.
  - The number of distinct normalized keys must equal the number of sends.
  - On the local graph, with the embedder that will ship, pre-register a mean top-8 overlap of at least 0.90 and a median context size within 5%.
  - If a check misses, allow one fallback format. After that, the decision goes to the owner.
- **Salt CPU cost:** measure the delta on Fly hardware and report it beside the CPU gate. Judge the gate on the raw number.
- **Staging order:** pre-cache the examples, run a 5-minute full-rate pilot, restart the API machine and the Neo4j clone, then run the soak.
- **Validity evidence:**
  - Distinct salted strings in the generator log equal the number of sends.
  - Ledger rows per run-id roughly equal the mock-LLM request count, which roughly equals live sends.
  - Process CPU-seconds per live ask must be at least 80% of the Fly per-embed cost, or the run is invalid.
- **Embedder:** S2 runs on whichever embedder will ship (decision 14), with the predicted outcome written down first.
- **Citation:** "One performance-2x [passed/failed] the 1,000-user test with a mocked LLM. Every live question was made unique, so none came from cache, which is stricter than the plan's own warm-cache sizing."

## The One Thing to Do First

Get the per-embed cost on performance-2x for both models. Check whether the planned S6 Fly timing run (M5A_BUILD_PLAN line 86, under $0.10) already gives it. Put the predicted S2 outcome and the void/fail proposal in the item-P note to the owner before any salt work. On the unpatched model, S2 as written cannot produce a verdict.
