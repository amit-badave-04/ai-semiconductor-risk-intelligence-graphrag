# Decision 012: the rerank fix after R24 failed on Fly

Date: 2026-11-26. The M2 flag-on deploy (release v28) passed every boot check, but the on-machine probe failed gate R24 (rerank
adds at most 2 s at p95): 2.54 s at 20 candidates and 4.38 s at 35, single thread, on shared-cpu-2x. A second run with 2 threads
gave 35 s / 64 s (burst credits drained). Production was rolled back to v27 on the owner's choice. Asked to "do the correct fixes",
the decision was taken to the `llm-council` skill (workflow wf_ecc38f87-75d): three fact-finders (Fly CPU model and prices; cheaper
rerank settings timed locally and scored on the free M2-Bench dev split; hosted and smaller rerankers), five advisors (Sonnet), five
anonymised peer reviews (Sonnet), a chairman (Opus). Applied under the owner's standing instruction to use the council for decisions.

Status: **approved by the owner 2026-11-26 ("Approve all")**; built test-first (192 tokens, cap 25, 1.8 s guard, spinning off); dev served arm 21/30, 30/48, nDCG@10 0.544 as pre-registered. The fusion-on, rerank-off fingerprint also moved (the config repr gained two fields; now `7a1f985a96`). Paid checks done: old-60 eval-deployed judged 19/23 (was 21/23; T9 and T15 answer variance, same excerpt ids), mechanical 41/41, citation 1.0; examples regenerated at `f604c90263` (55 of 56 seed); dev answer-level 26/35 vs 17/35, headroom 8-1, control 2-0, pass; claim support 0.906 on vs 1.0 off. The regeneration ran before the on-machine probe (this decision's order was probe first), so it was at risk if the probe failed. **Result:** release v30 (7555eb0) probed once about 80 min after boot: p95 1.13 s at 20 candidates, 1.41 s at 25 (192 tokens), not throttled: R24 PASS; M2 live.

Measured on the dev split (30 main / 48 with reserves; retrieval only, no paid call; the test split is not re-run):

| Variant | Success@8 main | all 48 | nDCG@10 | est. Fly p95 (real inputs) |
|---|---|---|---|---|
| frozen: depth 20, 384 tokens | 22/30 | 32/48 | 0.547 | about 2.4 s |
| fused legs, no rerank | 19/30 | 27/48 | 0.401 | 0 |
| depth 10, 384 | 20/30 | 29/48 | 0.504 | |
| depth 20, 256 | 21/30 | 30/48 | 0.524 | about 1.5 s |
| depth 20, 192 | 21/30 | 30/48 | 0.544 | about 1.1 s |

Real candidate counts on dev are 20 to 22 (35 is a theoretical worst case). The Fly estimates scale one Fly reading by local ratios.

## Council verdict

## Where the Council Agrees

- **Do not buy a bigger machine.** All five advisors reject performance-1x (+~$25/month in sin) and performance-2x (+~$51.5/month). The facts back them. A performance CPU has the same per-thread speed and only removes throttling. The facts estimate performance-1x at about 2.2-2.6 s at 20x384, which still fails, and performance-2x still fails at 35. These sin prices are the researcher's own arithmetic, not quotes from Fly.
- **Do not add a hosted vendor now.** It would mean a new account, a paid key, user queries leaving the machine, unclear Voyage training terms, and a conflict with "local first". The trans-Pacific latency has not been measured.
- **Do not switch models first.** MiniLM-L4, L2 and TinyBERT have no dev-split evidence, need a download approval, and would make the published numbers describe a different model.
- **The best measured lever is the same MiniLM-L6 at depth 20 with MAX_TOKENS 192.** Four of the five advisors pick it (all but the Contrarian, who keeps it as a fallback). On the dev split it scores 21/30 against 22/30, nDCG 0.544 against 0.547, and context-hit 26 against 26. It still beats no-rerank (19/30). Estimated Fly p95 is about 1.10 s on real inputs and about 1.95 s at the synthetic 35-candidate worst case. Ongoing cost is $0.
- **Use 1 thread.** The 35 s / 64 s run with 2 threads was burst credits draining: fully throttled speed is about 8x slower, and 2.54 x 8 is about 20 s, which matches the 17 s p50. Do not use 2 threads under a pooled quota.
- **Probe on the machine from a rested state, once, and read the throttle metric.** The 1.1 s and 1.95 s figures are extrapolated from a single Fly reading.
- **Never re-run the test split.** Any change to depth, tokens or candidates changes the fingerprint. That forces a paid regeneration of the cached examples and an honest restatement of the claims.

## Where the Council Clashes

1. **Ship the frozen setting if a rested re-probe passes (Contrarian), or change the setting now (everyone else).** The data settles this against the Contrarian.
   - If the first probe had been throttled, a 20x384 run would sit near 20 s per run.
   - It measured 2.54 s p95, so it was mostly unthrottled. 2.54 s is close to this vCPU's real speed, and the dev PC is about 3.3x faster per thread.
   - On real inputs the frozen setting estimates about 2.41 s. A rested probe will very likely fail again.
   - The frozen setting stays alive only if the throttle metric shows the first probe window was heavily throttled. Checking that is free. Do it first, then stop debating this.
2. **Cap the candidates: at 20 (Executor), about 25 (Expansionist), about 30 (First Principles), or not at all (Outsider).**
   - A cap of 20 removes the slot-guarantee heads. "Heads-off" quality was never measured, so a cap of 20 is unvalidated.
   - A cap of 25 never binds on dev, because real counts run 20 to 22 across all 48 questions. Its quality is therefore identical to the measured variant (f) by construction, and it can be checked for free.
   - At 192 tokens, 25 candidates estimates about 1.40 s on Fly worst case, which gives real margin over the thin 1.95 s at 35.
   - Pick 25.
   - Correction: the Executor's "1.67 s at 20 worst-case passages" is wrong. The facts table gives 1.11 s at 20x192. 1.67 s is the figure for 30 candidates.
3. **Ship fusion-only first (Expansionist, Contrarian, Executor) or go straight to the final setting.**
   - Fusion-only also changes retrieval relative to v27, so it almost certainly needs its own regenerated examples. That would mean two paid regenerations instead of one.
   - It also ships 27/48 on dev when 30/48 is available.
   - Reject it unless reading the fingerprint code shows that fusion-only reuses existing examples. Keep it only as the emergency fallback.
4. **Rewrite the R24 gate as "p95 on real counts" (several reviewers) or keep it.**
   - Do not weaken the gate. With 192 tokens and a cap of 25, the worst case as written is estimated to pass.
   - One clarification needs the owner's written approval. The text "on the (throttled) Fly machine" must mean a shared-cpu-2x machine in its provisioned, rested state. A machine whose credits are drained cannot meet any rerank gate: 20x192 fully throttled is about 9 s. That drained state is handled by the guard below, not by the gate.

## Blind Spots the Council Caught

- **No runtime latency guard (all five reviewers).** This is the most important miss. A passing probe does not protect production when burst credits drain.
  - After every deploy the machine starts with only a 5 s balance (Fly figure).
  - Loading the 2.1 GB embedder at boot probably uses up that 5 s. This is my inference, not measured.
  - So the first asks after a deploy, or after a bot burst, run throttled.
  - Fix: a rerank time budget. Score pairs one at a time, as today at batch 1. If the elapsed time passes the budget, drop the rerank for that request, return the fused order, and log or count the fallback.
  - This bounds "rerank adds" at about the budget plus one pair, makes the gate hold under any throttle state, costs nothing, and does not touch the fingerprint. The offline bench and example generation run with the guard disabled.
- **Order of operations (Review 4).** Measure on the machine before paying for regeneration, so you regenerate only once and only for a setting that passed.
- **Check the fingerprint code first (Review 5).** Confirm exactly which knobs are in the answer-template fingerprint. That decides whether the cap or a fusion-only stage forces regeneration.
- **Pre-register acceptance before measuring (Review 3).** The 192-token setting was picked on dev, which is legitimate tuning on dev. Record it as post-hoc with respect to test.
- **The benchmark claims no longer describe the shipped system.** 40/60 Success@8 and 37/55 answer-level were measured at depth 20 with 384 tokens. The owner's public-surfaces rule means public copy must not imply those numbers for the 192-token build.
- **The burst-refill figures are derived, not Fly's.** The ~67 min to refill from empty is the researcher's derivation. Decide on the measured throttle metric.
- **Unresolved discrepancy.** v28 booted with "53 examples", but the regeneration scope says 58. Find out why before the paid run, so you pay for the right set.

## The Recommendation

Ship M2 with the same model and machine and a smaller rerank workload, plus a runtime guard:

- **Model:** MiniLM-L6 ms-marco, ONNX quint8 avx2, unchanged. No download.
- **Rerank settings:** depth 20, MAX_TOKENS 192, total candidates capped at 25. The cap keeps the slot-guarantee heads first and trims the lowest-ranked fused tail.
- **Runtime:** RETRIEVAL_RERANK_THREADS=1, intra-op spinning off (`session.intra_op.allow_spinning=0`).
- **Machine:** shared-cpu-2x, 4 GB, sin. No change.
- **New guard:** a rerank time budget of 1.8 s per request. Past it, skip the rerank, use the fused order, log the event, and count it in the metrics. The guard stays off in the bench and in example generation.

**Steps, each gated on the one before:**

1. **Free checks, no spend:**
   - Read `fly_instance_cpu_throttle` for the first probe's window. If it was heavily throttled, re-probe the frozen setting from rest before changing anything.
   - Read the fingerprint code and settle the 53 vs 58 discrepancy.
2. **Code, test-first, on the fast path:**
   - Add the MAX_TOKENS, cap and budget config, with tests for the cap ordering and for the guard falling back.
   - Add `--max-tokens` and `--max-candidates` flags to `scripts/rerank_latency_probe.py`.
   - Re-run the dev bench, which is free. The cap must not bind on any of the 48 questions, and results must match variant (f): 21/30, 30/48, nDCG 0.544.
3. **Pre-register the gate:** before probing, write down "p95 ≤ 2.0 s at 20x192 and at 25x192 worst case, rested machine, 1 thread, throttle metric near zero during the run".
4. **On-machine probe:**
   - Run it on a temporary machine of the same size in the app: no public service, idle at least 70 minutes, one probe run, then destroy. That costs cents.
   - The alternative is a short deploy-and-rollback window on production.
   - Run it once only, never back to back.
5. **If it passes:**
   - Regenerate the examples once. This is a paid run of a few dollars.
   - Run answer-level on dev only, also a paid run of a few dollars, as the honest substitute for the test-level 37/55. Then deploy behind the flag.
   - Public copy cites only what was measured on the configuration it was measured on. Do not re-run the test split.
6. **Fallback ladder if the probe fails:**
   1. Cap at 20, after a free dev check of heads-off quality.
   2. MiniLM-L4, which needs a download approval (file, source, size) and a dev check.
   3. Ship fusion-only, which needs its own regeneration.
   4. Hosted Voyage rerank-2.5-lite last, which needs vendor approval.

   Performance CPUs do not appear in the ladder: they do not improve per-thread speed.

**Cost:** $0 per month. One-time: a few dollars to regenerate the examples, a few dollars for the dev answer-level check, and cents for the temporary probe machine.

**The owner must approve:**

1. The setting change: 192 tokens, cap 25, and the new fingerprint.
2. The 1.8 s guard with fallback to fused order.
3. The written reading of R24 as "rested shared-cpu-2x machine", with drained-credit states covered by the guard.
4. The paid regeneration of the examples.
5. The paid dev answer-level check.
6. The temporary probe machine, or the production probe window.
7. The wording of the benchmark claims: 40/60 and 37/55 are labelled as measured at 384 tokens, and the shipped setting is described as dev-validated.

**Uncertain facts:**

- Every Fly latency figure for 192 tokens is extrapolated from one Fly reading.
- Dev is 30 questions, so 21 vs 22 cannot be told apart statistically.
- The burst-credit drain at boot is an inference.
- The sin prices are arithmetic, not Fly quotes.

## The One Thing to Do First

Pull `fly_instance_cpu_throttle`, and the steal metric if it exists, for the window of the first probe (the 2.54 s / 4.38 s run). It is free, needs no deploy, and is the only open fact that could keep the frozen 384-token setting alive. If it shows little throttling, as the 2.54 s figure suggests, the frozen setting is ruled out: go straight to the 192-token, cap-25, guarded build and its rested on-machine probe.