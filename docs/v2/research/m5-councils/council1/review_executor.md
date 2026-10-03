**1. Strongest: B.** Its arithmetic checks out (70% of 0.125 core-s/s is about 0.09; a 60-minute soak at 0.33 core-s/s exceeds the 500 s burst cap). It is the only response that flags the single uvicorn event loop and the unscaled 1 GB Neo4j machine. It gates the embedder fix on benchmark and parity, and it leaves Option B as an owner decision conditional on spike S2. Its claim that Neo4j is the "likelier" bottleneck is not supported by the brief. C is a close second (correct 2.6x figure, concrete pass/fail steps), but "only path needing no owner spend decision" is wrong: staging also costs money.

**2. Biggest blind spot: D.** It recommends Option B without asking which machine class or fleet size actually passes. It never raises the unmeasured Fly CPU or the non-embedding per-ask CPU. Its only detection method is a human paraphrase test. Its arithmetic is correct (150 / 2.6 is about 58 s; 17% parity misses). E has a similar flaw: it treats +$51.53/month as trivial despite the owner's sign-off rule, and its "serving you right now was load-tested" line risks the overclaim the rules forbid.

**3. All five missed:**
- Staging must bypass or spoof Turnstile and the per-IP 5/10-min window, so "admission control proven" is overstated.
- A restart or deploy empties the embedding cache, causing a cold-embed stampede (about 1.0 core-s/s, not 0.33).
- Live deploys drop SSE streams on a single machine, which is Option C's real argument and goes unexamined.
- The gate amendment (performance class replacing "that class's sustained CPU") must be pre-registered before the run, not after.
- The answer benchmark needs a pre-set pass threshold.
