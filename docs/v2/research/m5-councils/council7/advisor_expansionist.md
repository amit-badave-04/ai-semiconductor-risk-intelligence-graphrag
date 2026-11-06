# Expansionist

**Decision 1: adopt fp32, in two steps, with no new spend until the soak says it is needed.**

The 0.6% margin is a sum of two peaks measured separately, so treat it as a prediction. The speedup is the real prize. On the drained machine, which is the live site's normal state, embeds go from 9.96 s to 2.76 s. That is the difference between a demo that feels dead and one that feels instant.

Lowering the upload cap is the wrong lever. The weights are 2.06 GB of private memory, and activations are a small fraction of that, so cutting pages barely moves the peak.

1. Ship fp32 behind the existing flag, with three safeguards.
   - A single-slot semaphore on upload embedding, so a max-size upload can't overlap another.
   - An upload-refusal guard when RSS exceeds about 2.25 GB.
   - A health-log line for RSS.
2. Run window W2, a 30-minute soak with live-shaped concurrent asks plus back-to-back max-size uploads, at 4 GB.
   - Pass: measured peak is at most 2.28 GB (about 5% margin) and zero OOM kills.
   - Fail: ask the owner for the 8 GB class, about +$30/month (derived). It buys headroom for the cache and uploads.

Also try loading the weights as an mmap'd external-data file, which would make them reclaimable page cache instead of private memory. If it works, the +1.26 GB delta mostly disappears. It needs its own gates.

**Decision 2: reframe S2 as a measurement, not a pass/fail.**

- Record S2 at 1,000 users as "not attempted, R1 predicted fail", so a failure stays a failure.
- Pre-register S2b before any spend: a ramp-to-saturation run on the live class in drained state, and on performance-2x with fp32. Use a mock LLM, a $2.00 cap and CPU at most 70%. The headline is measured sustained asks/s per machine.
- Add a normalized-question embed cache and report capacity both salted and cache-hit. Real traffic repeats the example questions, and the salted benchmark deliberately ignores that.
- Price performance-4x as a quote only, no spend. By my arithmetic, 1.77 core-s/s over 4 cores is about 44%, so it would pass.

Order: flag and guards, W2 soak, S2 record, S2b registration, owner approvals.
