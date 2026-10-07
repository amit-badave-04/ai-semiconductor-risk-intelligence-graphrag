# Executor

**Use (b), the per-ask salt.** It's the only option that is a load-generator change only, and Monday morning it's a one-hour task.

**Why not (a).** It ships dead code paths in the production image. It also makes the test easier than real traffic. Skipping cache writes removes a write that production does on every live ask. Disabling the LRU removes its lock and memory behavior. A pass would not prove the production path holds. (c) costs LLM money and days, and shifts the retrieval distribution.

**Safeguards (all runnable before the soak):**
1. **Validation smoke test.** Push 300 salted pool questions through validation, company/alias detection and length limits. Require 0 rejections or behavior changes. Fix the salt format if any trip.
2. **Embedding cost calibration.** On the staging machine, time the ONNX embed for 300 questions salted and unsalted. The salt adds about 4-5 tokens to a roughly 15-token question, so it could inflate CPU cost. If the difference exceeds 5%, shorten the salt. Report the measured delta next to the CPU gate. This is the direction that could make a fail unfair (assumption: embed time scales with tokens).
3. **Retrieval drift check.** Compute top-k overlap and context-token-size distributions, salted against unsalted. Publish the numbers.
4. **Salt uniqueness.** Use run-id plus a global counter, never random. The cached example set carries no salt.
5. **Server-side evidence of misses.** Record answer-cache hit rate (expect about 45%, i.e. 2.1/(2.1+2.6)) and LRU hit rate (expect about 0%) from counters. The void rule's ledger check then confirms the paid rate independently.
6. **Cache growth.** Check that about 10k staging rows has no lookup-latency effect (assumption: it doesn't; production also grows).

**Citation wording:** "passed with per-ask-salted questions, mock LLM, salt delta X%."
