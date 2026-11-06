# Peer review 3

**1. Strongest: A.**
- It measures the real uvicorn process before shipping, and it pre-registers a gate (peak at or below 2.3 GB).
- It sees that the 2.4 GB limit is a budget and that a real OOM is about 1.5 GB further out.
- It tests free levers in the same run (arena off, batch cap) and adds a 503 guard.
- It puts fp32 behind an env flag so rollback is one `fly secrets set`.
- It rejects the +$30 machine and records S2 honestly without new spend.

D's point that cutting the upload cap barely helps, since weights are 2.06 of 2.13 GB, is also right. A never lowers the cap either.

**2. Biggest blind spot: E.**
- It ships fp32 to the live site before the soak.
- It pre-registers a $2.00 S2b run and a cache feature without owner approval.
- It treats "performance-4x would pass" as arithmetic and quotes no price.

**3. What all five missed:**
- None checks what enforces the 2.4 GB limit. If it is only a budget, crossing it is not an OOM. If it is a guard, crossing it means refusals.
- Every new threshold (2.20, 2.25, 2.28, 2.30 GB, 5% margin) is set after seeing the data. Each should be labeled a new pre-registration, and the original gate recorded as PASS at 0.6%.
- The owner owns the claim change away from 1,000 users, and only C says so.
- Nobody tests micro-batching concurrent embeds. A call reads about 2.4 GB of weights, so batching may be the cheapest lever on core-s per ask.
- Nobody confirms there is a single uvicorn worker, or checks OOM restart behavior.
