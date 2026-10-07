# Peer review 2

**1. Strongest: D.** It does the CPU arithmetic first. 2.6 asks/s × ~1.1 core-s is about 2.9 cores, against a 1.4-core gate on 2 vCPUs. So an honest S2 likely fails, and D says to write that prediction down before the run. D also catches the failure the void rule can't see: embedder invocations per live ask, which would expose a silent LRU hit. It correctly notes that Neo4j stays warm under both (a) and (b), and it bars gate edits after seeing data. A also has the arithmetic, but D is more complete.

**2. Biggest blind spot: C.** It misses the CPU arithmetic and the salt-collision risk, and it uses a fixed seed with no counter. It proposes rotating "filing-year" and "company-context" qualifiers, which would change company detection and the retrieval distribution (the thing option c is rejected for). It also relies on the "ledger" for cache and LRU hit rates, but the ledger only records paid asks. B and E are also weaker than D. E wrongly says (b) avoids the warm page cache. B never checks the CPU arithmetic.

**3. All five missed:**
- Running calibration and smoke checks on the staging box warms the ONNX model, the page cache and the LRU. It also fills the answer cache and ledger, which breaks the pre-registered cold start. Run them elsewhere, or wipe and restart before the soak.
- None proposes a short pilot to confirm the paid rate stays above the 95% void line before the 60-minute run.
- E's 5-digit salt collision (about 4-5% of salts) is a real risk, but only E and B mention uniqueness.
- Nobody pre-registers fallback decisions for each pre-run check, such as the overlap threshold, before seeing data. Only E gestures at this.
