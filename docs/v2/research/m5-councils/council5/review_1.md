# Peer review 1

**1. Strongest: D.** It does the feasibility arithmetic (2.6 asks/s × 1.1 core-s is about 2.9 cores against a 1.4-core gate) and turns it into a usable pass threshold of about 0.5 core-s per live ask. It also:
- Registers the safeguards as an addendum before the run.
- Forbids gate edits after seeing data.
- Calls a CPU failure a valid result.
- Correctly says the warm Neo4j neighbourhood problem hits both (a) and (b).

A is close, with the same arithmetic plus server-side counters, but it lacks the Neo4j point.

**2. Biggest blind spot: C.**
- Its rotating "natural qualifiers" (filing-year or company clauses) change retrieval and company/alias detection. That is an unplanned drift toward (c).
- It has no CPU feasibility check.
- Its overlap threshold is vague ("if high").

E also errs on two points:
- It says (b) avoids the warm page cache. It doesn't, because salted questions hit the same graph regions.
- Its 4-5% salt-collision estimate ignores that a collision needs the same question and the same salt. Over about 30M combinations that is roughly 1-2 collisions per hour. A counter is still the right fix.

**3. All five missed:**
- **Cold start.** Every calibration, overlap or validation run on the staging box warms ONNX, the Neo4j page cache, the LRU and the answer-cache rows. Run these elsewhere, or restart and purge before S2.
- **Negative control.** No response shows that the counter checks and gates can fail, for example with a short unsalted run that should trip the checks.
- **Pre-agreed consequences.** No response says in advance what happens if a calibration check fails (rerun, void or redesign), or what the owner is told about the changed realization.
