# Outsider

**Recommendation: (b), the per-ask salt, with three guardrails.** An outsider's test is "is the thing being tested the thing that ships?" (b) changes only the input, so the server is the production server. (a) tests a server with two switches flipped, and those switches ship in the image.

**Hidden problem in (a):** the same 300 questions still hit Neo4j. Retrieval over 300 repeated queries runs against a warm page cache, which real traffic of diverse questions would not get. That makes the test easier than real traffic, which the constraints forbid. (b) avoids this.

**Hidden problem in (b):** the example salt is " (ref 48213)", which is 5 digits, so 100k values. Assume about 9.4k live asks per hour. By my birthday-collision estimate, roughly 4-5% of salts collide. Each collision is a cache hit, which pushes the paid rate toward the 95% void line. Use a per-generator sequential counter or 10+ digits. A bare number could also be read as a year or filing id, so test that.

**Safeguards (all fixed before the run):**
1. Normalization check: confirm the cache-key normalizer doesn't strip "(ref N)" and that validation, alias detection and length limits pass.
2. Retrieval-divergence pre-run: measure top-k overlap between salted and unsalted questions. Pre-register a threshold and a fallback now, not after seeing the number.
3. Server-side proof, not generator claims: log counters for answer-cache misses, embedding computations and LLM calls per live ask. Require embeddings computed to be at least 95% of live asks. Check that the cache-hit share matches 2.1 / (2.1 + 2.6), about 45%.
4. Cached asks: pre-cache the example set and send it unsalted.
5. Wording to the owner: "passed with a mocked LLM and unique salted questions." Also state that retrieval is slightly perturbed.

(c) costs money and effort and shifts the retrieval distribution, so don't use it.
