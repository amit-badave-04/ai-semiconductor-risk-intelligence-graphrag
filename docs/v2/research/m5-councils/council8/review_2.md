# Peer review 2

**1. Strongest: B.** It keeps the registered gate open. W1b is reported as not passed, and boot 3 runs as registered. Any lighter workload becomes a separately named W1c, and the owner decides whether it substitutes, which is what the never-waive rule requires. B also does the arithmetic: G4's 1,125,116 kB plus 1.26 GB is about 2,385,000 kB. A light load will therefore pass easily, and the thin margin is the real finding. It adds a q8 control and the abuse case as well.

**2. Biggest blind spot: A.** The builder pre-declares and runs W1c with no owner decision on replacing the gate. A also frames the DoS as a "differentiator" and proposes a permanent harness, which is scope creep. D is the runner-up, because it keeps boot 1 (saturated) alongside paced boots 2 and 3, and that mixed set looks selected.

**3. What all five missed:**
- **Exploitability.** Nobody checked in code or tests whether one address can actually hold both ask slots. Per-address limits, the meter, and staging-only switches may make saturation unreachable in production.
- **Memory peak.** The peak sat within 35-49 MB of the limit on every boot regardless of ask load. That suggests it is set by the model load and upload batch, not the asks, so the paced run mostly re-measures the same number.
- **No stop rule.** Nobody proposed a pre-registered stop rule for repeated INVALIDs, or a threshold for what margin counts as safe.
- **Not adopting fp32.** At a few asks per hour, 3.6x faster query embeds buy little against a 1.5% margin. "Keep q8, or buy a bigger machine" is a legitimate default until the owner chooses.
- **Timeout cause.** Nobody ran a lone upload on a throttled machine to test the throttle explanation.
