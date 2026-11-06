# Peer review 4

**1. Strongest: B.** It keeps the registered verdict separate: boot 1 PASS, boot 2 INVALID twice, boot 3 unrun. It runs boot 3 as registered. The paced test goes under a new name, committed before it runs, and the owner decides whether it substitutes. That is the only framing that fits "the builder never waives a gate." Its arithmetic also helps: q8's 1,125,116 kB plus 1.26 GB is about 2.385 GB, so a lighter load won't widen the margin.

**2. Biggest blind spot: C, shared by A.** C swaps the workload on the builder's own authority and never asks the owner to approve it. Pre-registering a changed gate still rewrites it. A also drifts into scope creep ("differentiator", a permanent harness). E has a factual error: boot 1 did complete its upload under saturating load.

**3. What all five missed:**
- They called option (a) futile and "deterministic", but boot 1 passed under the identical load (842 s against the 1,200 s timeout). That is a cliff edge, with roughly 1 in 3 boots valid. The pre-registered rule says re-run unchanged. The remaining $0.15 allows that with a pre-declared attempt cap and every attempt reported. The 2-in-3 failure rate is itself a finding.
- Nobody proposed a zero-ask baseline on a throttled machine. G4's 228 s may reflect burst credit, so the timeout may not need saturation at all.
- Peaks were nearly identical (2.350–2.365 GB) regardless of ask count, so memory barely depends on the workload. The real owner question is whether a 1.5% margin is acceptable, versus keeping q8 or a bigger machine.
- Nobody checked whether the in-flight cap is per-address, or what the user sees when an upload times out.
