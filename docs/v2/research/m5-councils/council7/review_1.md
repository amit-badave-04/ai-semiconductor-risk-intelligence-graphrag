# Peer review 1

**1. Strongest: A.**
- It is the only response that puts a real wall behind the budget. Physical RAM is 4.01 GB and about 2.43 GB is used, so actual OOM is roughly 1.5 GB past the 2.4 GB limit.
- It pre-registers a 2.30 GB gate before the live-process run.
- It tries free levers first: ORT arena off, a batch cap, and a serialized upload with a 503 guard.
- It ships behind an `EMBEDDER` flag, so rollback is one secret change.
- D's observation that weights are 2.06 of 2.13 GB, so cutting the upload cap barely helps, is the best single insight. A also keeps the 30-page cap.

**2. Biggest blind spot: D.**
- Its 2.20 GB gate is below the already-predicted 2.385 GB, so it moves the goalposts after the fact and nearly guarantees "hold q8". Every response treats that as the worst outcome.
- It also ignores the real 4 GB wall.
- E is second. It ships before measuring and adds a new $2 S2b run on the class R1 already says will fail.

**3. All five missed:**
- Fly swap (`swap_size_mb`) as a free memory safeguard.
- The transient peak while loading a roughly 2.4 GB fp32 model, plus the larger image and slower boot. The live start script creates a new machine each time, so boot cost recurs.
- Pre-embedding the example questions. E gestured at a cache but never proposed this.
- Owner consent. A and D redefine the M5 claim themselves. Only B, C and E route the deploy or claim change to the owner, and C is the only one to say the page must state the claim was dropped.
- Any new gate must be pre-registered before its run, and the existing pass must be recorded as the prediction it is.
