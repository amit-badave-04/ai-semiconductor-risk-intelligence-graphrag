# Council 8 verdict

## Where the Council Agrees

- W1b has not passed. Under the aggregate rule (fewer than three valid boots) it is INCOMPLETE, which is not a FAIL. Decision 18 says fp32 ships only if W1b passes, so q8 stays as it is now.
- Option (c) is out. Those boots are INVALID under rules written before any run.
- Option (d) stays out of the gate. The timeout gets its own spec.
- The owner should see the margin itself, 35-49 MB (1.5-2%), not just "PASS".
- The owner must hear about the timeout before any deploy.

## Where the Council Clashes

- **Who may change the workload.** A, C and E let the builder amend it. B and D require the owner. The owner wins. Decision 18 ties fp32 to W1b, and any new window needs a quote the owner has approved (README step 0).
- **Mixed boots.** D keeps saturated boot 1 next to two paced boots. Three fresh boots are better, because a mixed set looks selected.
- **Option (a).** The advisors call it futile, but the reviewers point out that boot 1 finished the same load. It is possible, though at the observed 1-in-3 rate the chance of two valid boots in four attempts is about 40% (inferred).
- **What to anchor a new workload to.** One side uses G4's 5 asks, the other a rate taken from the live limits. G4 is a weak anchor because it ran with its burst balance still full (M5_DECISIONS:44).

## Blind Spots the Council Caught

- **The saturating load was the registered plan.** README:80 says asks run "until the job is ready plus 30 s", with at most 2 in flight. Changing it is an amendment, not a fix to an "interpretation".
- **Memory barely depends on the ask load.** The four peaks sit within 14 MB of each other, and the boot whose upload completed had the lowest peak. The margin decides this, not the boot count.
- **The denial-of-service claim does not hold for paid asks** (checked in code).
  - One address cannot hold both slots: a second hybrid ask would exceed the $1.32 share (config.py:41-42).
  - The $10 day cap admits about 14 hybrid asks across the whole site ($10 / $0.709573).
  - The harness bypassed both limits on purpose (fly.w1b.toml:19-21).
  - Still open: whether free or cached answers (30 per address per window) take a slot or run an embed.
- **The throttle explanation does not add up.** README's pre-run estimate was 190 CPU-s per job. At W0's ~15% that is about 1,270 s with zero asks, yet boot 1 finished at 842 s with 118 asks. config.py:297 chose 1,200 s to cover risk 13, so that safeguard is now in doubt for both embedders.
- **Still pending:** recalibrating the MemAvailable guard, which council 7 scheduled for after W1b.

## The Recommendation

1. Record W1b as INCOMPLETE. Keep all four boots as extra stress data: peaks of 2,350,568, 2,364,488 and 2,364,660 kB, and boot 1 ready at 842 s.
2. The owner chooses before any machine is created. Thresholds stay the same in both options.
   - **W1c (recommended).**
     - A new name, 3 fresh boots and the same fp32 image.
     - Workload: 14 salted asks. That number is the live day cap's capacity at the hybrid estimate; commit how it was derived from config.py.
     - Send one ask every 30 s from upload accept, each from a different address, at most 2 in flight. Then measure until the upload is ready plus 30 s.
     - Recorded reason: the pre-run judgment that these are "admission caps, not memory" held for memory, but the uncapped load stopped the upload from completing.
     - Stop rule: an INVALID boot is re-run unchanged once. If it is still INVALID, W1c is INCOMPLETE and nothing else gets amended. A memory FAIL counts as a FAIL.
   - **R.** Finish W1b as registered. Re-run boots 2 and 3 unchanged, at most two more attempts each, and report every attempt. If that does not produce valid boots, W1b ends INCOMPLETE.
3. Afterwards, under its own spec: run one boot each on q8 and fp32 with no asks, starting the upload after the burst is used up. That shows what actually causes the timeout.

## The One Thing to Do First

Send the owner this status before creating any machine:

"Boot 2 is INVALID twice. Both times the upload hit its 1,200 s timeout under about 150 asks. Memory held: peak 2,364,660 kB, 35 MB under the limit, no OOM. W1b is INCOMPLETE, so fp32 is not adopted. Nothing is running. Please choose W1c or R."

Files read: docs/v2/research/m5-councils/council7/verdict.md, docs/v2/M5_DECISIONS.md, tools/probe/README.md, tools/probe/w1b.py, deploy/staging/fly.w1b.toml, src/semigraph/config.py, artifacts/m4_live_gates.json, artifacts/m5_spikes.json (all under C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag).
