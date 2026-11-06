# Peer review 3

**1. Strongest: C.** It rules out (a), (c) and (d) using the rules written before any run. It requires the amendment to be recorded before the next run, with the rate taken from an existing anchor (M4 G4). It runs three fresh boots instead of mixing in boot 1, leaves the thresholds alone, and pre-states that a memory FAIL stays a FAIL. The saturated boots are kept as labelled supplementary data. It would be stronger with D's step of getting the owner to approve the changed workload.

**2. Biggest blind spot: D.** D keeps saturated boot 1 and adds two paced boots, so the verdict mixes workloads and looks selected. E also wrongly says the margin was never measured on a completed saturated upload, but boot 1 completed at 842 s. A, C and E also let the builder amend a gate without the owner.

**3. What all five missed:**
- **Workload anchor.** "Live caps" is defined by production code: the per-address rate limit, the $1.32 daily share and the in-flight cap of 2. Deriving the maximum rate those limits permit is a principled, non-post-hoc anchor, better than G4's 5 asks.
- **DoS claim unverified.** "One visitor starves every upload" was asserted without checking the per-address limits and the spend meter. Check the code before telling the owner.
- **Cause not established.** Boot 1 took 842 s and two identical fresh machines timed out past 1,200 s. Nobody asked whether boot 1 would repeat.
- **Owner is waiting.** The owner is asking for status now. Report first and let them choose the amendment.
