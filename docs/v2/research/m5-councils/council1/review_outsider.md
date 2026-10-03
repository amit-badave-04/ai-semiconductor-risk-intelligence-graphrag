**1. Strongest: A.** It rejects the premise that live must equal proven, because the admission caps (5 asks per 10 min, 150 paid asks/day) already make 1,000 users unreachable on the live site. That removes the case for B, C and E. It also gives exact gates, a safe claim sentence and a rehearsed scale-up drill. C is a close second: it has the cleanest sequencing, and its 2.6x figure (0.33 / 0.125) is correct.

**2. Biggest blind spot: E.** It ignores the admission caps and the owner's rule that new recurring spend needs sign-off. It asserts "a performance-class machine can" pass with no S2 data. Its sales line, "the machine serving you right now was load-tested at 1,000 virtual users", overclaims given the mock LLM and the live caps. D has a related flaw: its own wording analysis shows the claim is misread whatever the machine, yet it recommends B.

**Arithmetic check.** B's 0.09 core-s/s, D's "about a minute" (150 / 2.6 = 58 s), D's 17 % and the +$51.53 figure all hold. B calls A "fatally flawed" and then recommends A. B's "soak drains the burst balance" is an inference, because the refill rate is unpublished.

**3. All five missed:**
- Deploy and rolling-restart behavior against the "0 drops" gate (`kill_timeout`, volume downtime), and whether Option C's redundancy matters.
- That moving the proof to a performance class amends a pre-registered gate, so the owner must approve that change explicitly.
- Where the load generator runs, and whether it can itself drive 1,000 VUs.
