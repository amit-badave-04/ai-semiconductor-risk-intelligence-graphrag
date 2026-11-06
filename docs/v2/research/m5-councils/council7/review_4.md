# Peer review 4

**1. Strongest: A.** It is the only response that pre-registers a numeric gate before the live-process run (peak ≤ 2,300,000 kB, and a fail stays a fail). It keeps the 30-page cap. It tries free levers that act on the real memory: ORT arena off and smaller batches. It adds an upload semaphore plus a 503 guard, and rollback is one secret via an EMBEDDER flag. It also treats holding q8 as costly (about 10 s versus 2.8 s per embed). Borrow from the others:
- C's three runs with the spread reported, and not publishing the 0.33 s burst figure.
- B's owner approval before deploy.
- E's mmap spike.

**2. Biggest blind spot: B.** C shares it. B says an upload cap "solves the same problem for free", but the weights are about 2.06 of 2.13 GB. Cutting tokens saves tens of MB at most. That fallback lever doesn't exist, which leaves only 8 GB or holding q8. D's ≤2.20 GB gate is also unreachable, so it amounts to a hold under another name.

**3. What all five missed:**
- The 3.6x comes from removing per-call dequantization. A native int8 kernel path (a MatMulNBits or integer-MatMul re-export) might give the speed at about 870 MB. It is untested and would need its own bounded, pre-registered spike.
- Fly's swap setting (`swap_size_mb`, verify it is supported) is a free backstop against OOM kills. Nobody measured the model-load transient peak either.
- Nobody derived the required asks/s from the owner's spend caps. They may make S2's 1,000-user premise moot.
