# Peer review 4

**1. Strongest: D.**
- D is the only response that combines three checks. It runs the CPU arithmetic (2.6 asks/s × ~1.1 core-s ≈ 2.9 cores against a 1.4-core gate; A also did this). It flags the warm Neo4j page cache (E also did). And it notes that the void rule's ledger can't see LRU contamination, so it requires an embedder-invocation check where a result below calibration is invalid, not a pass.
- D also pre-registers an addendum and forbids gate edits after seeing data.
- E's salt-collision figure is wrong. A collision only matters per (question, salt) pair, which is about 300 × 100k keys. That gives roughly 1-2 collisions, not 4-5%. E's counter fix is still harmless.

**2. Biggest blind spot: C.**
- C has no CPU feasibility check and no numeric thresholds ("if the median overlap is high").
- It expects the ledger to show LRU hit rates, which a paid-ask ledger can't.
- Its rotating qualifiers (filing year, company context) bring back option (c)'s retrieval shift and could trip alias detection. That is uncontrolled scope creep, not a safeguard.

**3. All five missed:**
- **No negative control.** Run a short salt-off canary to prove the miss counters flag contamination, and a salt-on run to prove they stay clean.
- **Overload handling.** If calibrated demand exceeds capacity, the run is a diverging queue, not a steady state. Pre-register reporting the maximum sustainable live asks/s, since that number drives the $9-52 decision.
- **Per-IP limits.** One load-generator IP will hit the ask route's per-IP limits and measure 429s, so source IPs must be spread without adding another staging code path.
