# Peer review 5

1. **Strongest: B.**
   - It states that W1b has not passed as registered.
   - It runs boot 3 as registered, which costs cents.
   - It lets the owner, not the builder, decide whether a committed W1c substitutes. This is the only answer that keeps "the builder never waives a gate" intact.
   - It spots that boot 1 reached ready at 842 s against the 1,200 s timeout, so even the passing boot had thin margin.
   - It projects the G4 baseline plus 1.26 GB to about 2.385 GB, so a lighter load is no free pass.
   - D is a close second, since the owner approves the new workload before it runs. But D mixes a saturated boot 1 with paced boots 2 and 3.

2. **Biggest blind spot: C.**
   - The builder writes and runs its own gate amendment, and picks the rate itself, with no owner approval.
   - It also asserts "q8 would be worse, so not a reason to reject fp32" without measuring it.

3. **What all five missed:**
   - Boot 1 finished the identical workload at 842 s, so "deterministic starvation" is wrong. The outcome is near a cliff and probably depends on host throttle state between boots. Nobody asked for CPU steal or throttle telemetry. Re-running until valid also selects the faster boots, which could bias peak memory.
   - Memory peaks were nearly identical (within about 14 MB) across all four boots, whatever the load. A paced rerun therefore says little about the margin.
   - The real decision is whether a 1.5-2% margin on a larger or max-size upload is worth fp32 at a few asks per hour. Keeping q8 or using a bigger machine is never weighed.
