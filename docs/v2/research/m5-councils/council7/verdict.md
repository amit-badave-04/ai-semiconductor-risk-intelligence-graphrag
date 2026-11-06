# Council 7 verdict

## Where the Council Agrees

- **Adopt fp32.** On a drained machine one embed takes 2.76 s with fp32 and about 10 s with q8. Retrieval is identical.
- **The 14,872 kB margin is a prediction.** It adds together two separate runs, and nobody measured it on the live process.
- **Cutting the upload cap won't help.** fp32 memory grows only about 176 MB between the after-queries and after-passages readings.
- **No new spend yet.**

## Where the Council Clashes

- **New memory gates.** Every threshold proposed (2.20 to 2.30 GB) sits below the 2.385 GB prediction, and the weights can't shrink, so those gates would fail by construction. Decision 18 pre-registered "predicted peak under 2.4 GB", and that passed. Confirm against that limit; do not move it.
- **performance-4x is not a recurring cost.** The staging fleet in section 2.1 is destroyed after each window.
- **The 1,000-user claim.** Dropping it rewords a decision the owner made, so the owner decides. Under the strengths-only rule the page simply stops making the claim, and the limits stay in the internal report.

## Blind Spots the Council Caught

- **Already handled in the repo:**
  - Live already has 1 GB of swap (`swap_size_mb = 1024` in fly.toml).
  - Nothing enforces the 2.4 GB figure. The real wall is about 4 GB of RAM plus swap. In G4 the lowest q8 MemAvailable was 3,007,356 kB.
  - Uploads already run one at a time, and query embeds get one slot.
  - The fp32 peak at model load (1,915,212 kB) is below the peak under work.
- **Use MemAvailable for the memory guard.** A guard on RSS near 2.2 GB would trip while fp32 is almost idle.
- **The "native int8" middle path is today's q8.** `accuracy_level=4` failed gate 1, and R0 keeps it failed.
- **Rollback means redeploying the previous image.** fp32 is a build-time variant, so changing a secret cannot switch it back.
- **Running S2 before decision 16 is signed risks paying $1.25 for a VOID.** Separately, the bigger image is pulled on every machine start; measure that.

## The Recommendation

**Decision 1: adopt fp32 after one confirmation run.**
1. Record W1. Gate 2 passed on both classes. The memory rule passed as a prediction, with a 0.6% margin.
2. Build and test two free changes:
   - A guard that refuses new uploads with 503 plus Retry-After when MemAvailable falls below 1,250,000 kB. That is the parse child's 1 GiB cap plus a margin, against an expected minimum of about 1.75 GB. Recalibrate it after W1b.
   - Logging of VmRSS and MemAvailable.
3. Commit the W1b plan before the run:
   - **Machine:** a fresh shared-cpu-2x with 4 GB in sin, running the fp32 image that would ship and the real uvicorn.
   - **Load:** M4's 30-page PDF plus mock-LLM asks at the live concurrency caps, on 3 fresh boots.
   - **PASS:** max VmHWM ≤ 2,400,000 kB, no OOM or restart, and machine creation to healthy ≤ 180 s. Report the spread, the lowest MemAvailable and swap use.
   - **Cost:** quote it to the owner first; the estimate is under $0.15.
4. **On PASS:** fp32 goes into the decision-21 deploy once the owner says go.
5. **On FAIL:** keep q8 and pre-register a separate run with the ORT memory arena off. If that also fails, the owner chooses between raising the 2.4 GB budget and moving to 8 GB. 8 GB is a new recurring cost of about +$30/month and needs the owner's approval.

**Decision 2: do not run S2 on performance-2x.**
1. Record: "S2 not attempted: predicted FAIL on measured Fly numbers (embedding alone needs 1.39 to 1.77 core-s/s, against 1.1 under R1 and 1.4 under S2)." This is not a pass:
   - the 1,000-user sentence stays unusable;
   - T1 is not evaluated;
   - Valkey stays deferred.

   Decision 20 already makes skipping the default.
2. The owner picks one:
   - **A, if the 1,000-user sentence still matters to buyers.** Pre-register S2 on one performance-4x with the same thresholds (CPU ≤ 2.8 core-s/s). Run it after W3 passes level 5 and decision 16 is signed, and only at a verified price. One machine keeps state in-process, so a pass also answers whether a shared state store is ever needed.
   - **B, at $0.** Drop the claim from the public site. Report capacity internally: about 0.36 asks/s on a drained machine, and about 3/s for roughly 116 s on a fresh one.
3. **Demos.** A drained machine with fp32 still misses the 1.5 s first-event target, so keep the per-demo scale-up to performance-2x.

## The One Thing to Do First

Commit the W1b plan before any run. Then send the owner one message with the W1b quote, decisions 16, 18 and 20 for ratification, and the choice between A and B.
