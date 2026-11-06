# Peer review 5

**1. Strongest: Response A.** It is the only one that pairs a pre-registered gate on the real uvicorn process (peak ≤ 2.30 GB) with free levers tested in the same $0.13 run: ORT arena off and a passage batch cap. It also adds a sanity check against physical RAM (about 2.43 GB used of 4.01 GB), a serialized-upload guard that returns 503, and an `EMBEDDER=fp32|q8` flag for one-command rollback. It spends nothing and asks the owner for the 8 GB machine only if the gate still fails. Response E has the best single idea, mmap'd weights as reclaimable page cache, but it ships before its soak test.

**2. Biggest blind spot: Response D.** Its gate (peak ≤ 2.20 GB) sits below the 2.385 GB prediction, and the weights alone are 2.06 GB. The gate therefore fails by construction, and its "hold q8" fallback is a hold in disguise. That ignores the drained-state cost of 9.96 s per embed. B and C also lean on the upload-cap lever, which D and E correctly show barely moves memory.

**3. What all five missed:**
- **A middle model.** fp16 storage or true int8 kernels (e.g. MatMulNBits) would give most of the speedup at a fraction of the memory. That needs a new parity pre-registration.
- **Burst balance.** fp32 uses 3.6x less CPU per ask, so the throttled state may arrive much later under real demo traffic. Nobody proposed measuring time-to-drain.
- **Cold start.** The artifact grows by 1.26 GB, so every new-machine boot pulls and loads more. Model-load peak was also not discussed.
- **New thresholds.** The 5% and 8% margins are invented after the original gate passed. They need pre-registering before the run.
