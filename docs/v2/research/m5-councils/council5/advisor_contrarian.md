# Contrarian

**Check the arithmetic before choosing anything.** 2.6 live asks/s at about 1.1 core-s per embedding is about 2.9 cores of demand. A performance-2x has 2 vCPUs, so the 70% gate is about 1.4 cores. I'm assuming the 1.1 s figure was measured on that machine class. If so, an honest S2 fails the CPU gate by roughly 2x on embedding alone, before retrieval, SSE and the 2.1 cached asks/s. Then the only realization that passes is one where embedding is accidentally cheap. Run a 10-minute single-ask CPU calibration on the staging box first, and write the predicted outcome down. To pass, all-in CPU must be at most about 0.5 core-s per live ask.

**Use (b), the salt.**
- The tested artifact is the shipped artifact. Option (a) puts dormant staging switches in the production image, and "passed" would then describe a configuration nobody runs.
- Cache writes and embedding behave as in production.
- The salt adds about 6 tokens, so CPU is biased slightly up, which is the conservative direction.
- Option (c) costs money and shifts the retrieval distribution.

**Both (a) and (b) leave a blind spot.** 300 distinct questions keep the same Neo4j neighbourhoods hot. State the clone size against the page cache. If the graph fits in RAM, say so in the result.

**Safeguards, pre-registered as an addendum before the run:**
1. Salted keys must miss the cache 100% of the time. Normalization stripping parentheses or digits is the likeliest silent failure. Validation, alias detection and length checks must also behave identically across all pool questions times N salts. Report the salted vs unsalted retrieval top-k overlap.
2. Each salt is random, unique, fixed-width and logged.
3. The void rule only sees the ledger rate, so embedding-LRU hits would go unnoticed. Add a check on embedder invocations (or CPU-seconds) per live ask against the calibration. A result below it is invalid, not a pass.
4. No gate edits after seeing data.
5. Cite the result as "salted 300-question pool, mock LLM, warm retrieval". A CPU failure is a valid answer, because it points to a bigger machine.
