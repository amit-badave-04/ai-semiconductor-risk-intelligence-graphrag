# Peer review 4

**1. Strongest: D.** Its rule is the most complete and the most honest.
- It makes the benchmark unlock an owner decision and never ship the fix.
- It gives a concrete retrieval test: each correct-to-wrong flip must still have its cited chunk in the top 8 under both models.
- It requires the Fly timing to pass first.
- It states that 41 questions can show only "no harm detected", not "equivalent".
- It frames S2 as a claim the site doesn't need yet.

C deserves credit for seeing that S2 on today's embedder goes void rather than failing.

**2. Biggest blind spot: A.** It says its speed paths "skip gate 1 entirely". Micro-batching, padding changes and truncation all alter the query vector, so they need the same parity gates. The LRU cache is useless for S2, which uses uncached asks. A also never says the benchmark can't detect small differences.

**3. What all five missed:**
- **Free pre-check.** Before paying, run retrieval only on the 41 questions and diff the top-8 and graph context under both models. Questions with identical context can't flip except by LLM or judge noise. Rerun only the changed ones. This is cheaper and removes noise.
- **Baseline noise floor.** The "never re-rolled" rule needs a pre-registered definition of a flip given LLM and judge nondeterminism.
- **S2's CPU budget.** The 70% cap on 2 cores is 1.4 cores. Embedding at about 1.0 core-s/s leaves about 0.4 core for Neo4j I/O, graph building and streaming, so "S2 feasible" is unverified. The Fly run should measure p95 TTFB under concurrency, not just median embed time.
- **Owner ratifies the rule first.** The owner should approve the exception criteria before the run, so the builder isn't writing its own waiver terms.
