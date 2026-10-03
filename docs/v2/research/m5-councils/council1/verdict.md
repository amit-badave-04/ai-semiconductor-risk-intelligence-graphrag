## Where the Council Agrees
- Adopt the `accuracy_level=4` fix (desktop 1.22 s to 0.31 s, same weights, cosine 0.9988, top-1 identical for 96 %), and never waive the ~$1.5-2 benchmark. Reject D and E.
- shared-cpu-2x cannot meet the CPU criterion: 70 % of its 0.125 core-s/s is 0.0875, and derived warm embedding alone is ~0.33.
- No bare "1,000 concurrent users". Measure on Fly (S2) before sizing.

**Corrections:**
- Warm load is 2.6x the sustained CPU but 3.8x the gate line (~11x with a cold cache). All three figures are derived.
- "The soak drains the burst balance" is an inference (the refill rate is unpublished), and it is moot because the gate measures sustained CPU.
- "Neo4j is the likelier bottleneck" has no support.
- Staging is one-off spend that the original plan (E) already contained.
- A performance class does not amend the gate, because the CPU criterion is relative to the machine class.
- "4x on every click" ignores that 45 % of asks skip embedding.

## Where the Council Clashes
- **Live fleet: A (Contrarian, First Principles, Executor) against B (Expansionist, Outsider). A wins.** The caps make the gate load unreachable live: 150 asks/day lasts ~58 s at 2.6 live asks/s, assuming live asks are the paid ones. So +$51.53/month buys capacity nobody can use, and "the machine serving you was load-tested" would still overclaim.
- **Fly timing as a ship gate (First Principles):** it blocks only if the patched model is slower on Fly. Otherwise it feeds staging sizing.

## Blind Spots the Council Caught
- **Reviewers:**
  - The test stubs Turnstile and raises the caps, so it does not prove live admission.
  - A restart empties the embedding cache.
  - The load generator's location and capacity are unstated.
  - Per-process state (per-IP window, daily ceiling, cache, slots limiter) splits across machines.
  - Deploys drop open streams.
  - The benchmark had no threshold.
  - Option C, performance-1x 2 GB (+$9.65) and a demo-window scale-up were never weighed.
  - Rewording the recorded decision is the owner's call.
- **Chairman:**
  - Never pre-warm the cache with the 300-question test pool; that games the test.
  - "Per-machine CPU" plausibly covers the staging Neo4j machine (our reading).
  - A fresh shared machine starts with a 5 s burst balance.

## The Recommendation
**(a) Embedder.** Set `accuracy_level=4` in the builder. Do not re-embed the corpus. These gates are fixed before any run:
1. CI parity on the built file (53 questions, against the current vectors): all 196 nodes carry the attribute, cosine mean ≥ 0.998, min ≥ 0.997, top-1 identical ≥ 95 %, mean top-8 overlap ≥ 0.97.
2. Paid benchmark: no headline metric more than 2 percentage points below the production baseline (council-set), and every flipped verdict read and explained. A miss blocks shipping, is reported, and is never re-rolled.
3. On Fly, on both the live and the staging class: the patched model beats the unpatched one, by median over ≥ 50 interleaved calls.

**(b) Live.** One shared-cpu-2x ($32.24/30 days) behind its caps. Ask for recurring spend only if S2 shows it failing real demo traffic: p95 time to first event above 1.5 s within the caps, including right after a restart. Then propose performance-1x 2 GB (+$9.65), if ~1 GB RSS fits. For buyer demos, offer a temporary scale-up using the rehearsed runbook.

**(c) Wording.** Thresholds, load model and the 60-minute soak stay unchanged. Record the exact fleet (class x count, Neo4j included) before the run. A resized re-run is a new run, and both are reported. Public sentence: "On a temporary staging fleet of [N x class] in sin, with a mock LLM and the pre-registered load model (1,000 virtual users, ~2.6 live asks/s, ≥ 40 concurrent live streams), the app tier held p95 time-to-first-event ≤ 1.5 s, 0 dropped streams and errors < 0.5 % through a 60-minute soak." The internal report lists every test exception and states that the live fleet differs. Owner approval is needed for this sentence (it rewords the owner's recorded decision) and for any change to thresholds, load model, soak or CPU criterion.

**(d) Staging.**
- **Admission:** the stub and raised caps exist in staging config only, and a test proves live rejects them. The slots limiter and degrade path stay at production values. A separate run at production settings proves the sixth ask per IP is refused and the site degrades cleanly at the daily ceiling.
- **Cold cache:** every gate run starts on freshly booted machines, with the first 10 minutes reported separately.
- **Generator:** k6 or Locust on its own machine in sin, through the public hostname. If the generator exceeds 70 % CPU, the run is void.
- **State:** use one machine if S2 shows it passes. Otherwise move the daily ceiling and per-IP window into an existing shared store first.
- **Deploys:** set the kill signal and `kill_timeout` explicitly, drain open streams on shutdown, never deploy during gate windows or demos, and report one rolling-deploy drill separately.

**(e) Hosted query embedding: no.** Revisit only if the fix fails, and only with the owner's sign-off for a new vendor.

## The One Thing to Do First
Set `accuracy_level=4` in the builder, rebuild, and commit the 53-question parity test as CI on the built file, so the benchmark, the Fly timing and S2 all run on the file that ships.

## Decisions for the Owner
1. **Approve the scoped public sentence.** Recommend yes. No cost. Without it, the staging gate run (so the spend proves a sentence the owner accepts) and any capacity claim are blocked.
2. **Only if S2 shows the live class failing: move live to performance-1x 2 GB.** Recommend yes if that happens. +$9.65/30 days. Blocks nothing now.
3. **Optional: temporary scale-up of live for buyer demos.** Recommend yes, decided per demo. One-off, ~$0.12/hour for a performance-2x 4 GB ($83.77 per 720 h, assuming pro-rata billing). Blocks nothing.

**Decided by the council, no owner approval needed:** the embedder fix under (a), including the benchmark run; live stays one shared-cpu-2x (B and C rejected); thresholds and load model unchanged; staging as designed in (d); hosted embedding rejected.
