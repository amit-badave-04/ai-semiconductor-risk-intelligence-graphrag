# Peer review 3

**1. Strongest: D.** It is the only response that pairs the CPU arithmetic (about 2.9 cores of demand against about 1.4 allowed) with a validity rule: a CPU pass below the calibrated embedder cost is invalid, not a pass. It also notes that both (a) and (b) leave the same 300 Neo4j neighbourhoods warm. It writes the safeguards as a pre-registered addendum with no gate edits after seeing data. E is runner-up for catching 5-digit salt collisions (about 4-5%, close to the 95% void line). E wrongly claims (b) avoids the warm page cache, since salted queries still hit the same neighbourhoods.

**2. Biggest blind spot: C.** Rotating filing-year or company-context qualifiers changes retrieval and alias detection. That breaks the "not easier" and near-identical-retrieval goals and adds unrequested scope (M5b reuse). It never does the CPU arithmetic. It also says the ledger can show cache and LRU hit rates, which a paid-ask ledger likely does not hold. B and E also skip the arithmetic.

**3. Missed by all five:**
- **Pilot contamination.** The pre-run calibration warms ONNX, the Neo4j page cache, the answer cache and the LRU, which breaks "cold start". Restart the machine, purge salted rows, and exclude the pilot from S2 data.
- **Counters may not exist.** Embedder-call and LRU counters may not exist in production, and adding them is a production change, the same objection raised against (a). Use independent oracles: the mock-LLM request count, per-process CPU-seconds and Neo4j query counts.
- **The load generator itself.** Locust CPU, region and co-location, and Fly proxy concurrency limits can create or hide TTFE degradation and stream drops.
- **Void versus fail rules.** No one pre-registers what a void or a fail means for the $9-52 spend decision.
