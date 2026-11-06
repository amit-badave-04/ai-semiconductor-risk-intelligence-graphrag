# Peer review 1

**1. Strongest: D.** It is the only response that makes owner approval of the changed workload a precondition, which the "builder never waives a gate" rule requires. It also fixes the amended workload from the M4 G4 anchor before any new run. It labels the saturated runs as supplementary and shows the owner the 1.5% margin, not just PASS. It rejects (a), (c) and (d) for sound reasons. Its one flaw is carrying boot 1 into a mixed-workload set. Drop that, as C and E do.

**2. Biggest blind spot: A.** It treats the builder's W1c as a replacement gate without owner sign-off. It also adds scope (a q8 arm, a permanent harness) and frames the denial-of-service finding as a "differentiator". B has a smaller flaw: it says to run boot 3 "as registered", which would likely time out again, and it calls (a) futile.

**3. What all five missed:**
- **The failure is not deterministic.** Boot 1 passed the same saturated workload (842 s), so only 1 of 3 boots finished. C and D call it deterministic. Unchanged re-runs with a pre-stated attempt cap are the only route that finishes the gate as written without an amendment, and they cost cents.
- **The denial-of-service claim is unchecked.** Nobody compared it against the live per-address rate limits and the paid-call meter. The mock harness probably bypasses both.
- **Not adopting fp32 is the null option.** At a few asks per hour the 3.6x speedup is not user-visible, and it uses most of a 1.5% margin. Staying on q8, or using a bigger machine, were never weighed.
