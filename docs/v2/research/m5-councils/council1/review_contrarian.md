**1. Strongest: B.** Its arithmetic holds (70% of 0.125 is about 0.09 core-s/s; 0.33 warm load drains the 500 s burst in about 40 min). Only B flags the unscaled 1 GB Neo4j machine and the single event loop, and it keeps the $1.5-2 benchmark non-waivable. Flaw: it calls A "fatal", then recommends A. C is runner-up (clean pass/fail order, correct 2.6x ratio).

**2. Biggest blind spot: E.** It treats derived, Fly-unverified numbers as settled and pays +$51.53 (correct) for "the machine serving you was load-tested". The live site still caps 5 asks/10 min and 150/day with a mock LLM, so that line stays misleading. It never gates the fix on Fly timing, ignores single-machine deploy drops, and "4x on every buyer click" overstates it when cached embeds dominate. D also picks B, but words the claim carefully.

**3. All five missed:**
- Option C (deploy redundancy against the 0-drop gate and kill_timeout) was never weighed. Nor was the cheapest dedicated step, performance-1x 2 GB at $41.89 (+$9.65).
- Scale-out fragments per-process state (per-IP window, 150/day ceiling, embedding cache, slots limiter). The test must also relax Turnstile and per-IP caps, so admission and degrade are not proven as live runs them.
- Load-generator capacity and location for 1,000 VUs against `sin` is unaddressed.
