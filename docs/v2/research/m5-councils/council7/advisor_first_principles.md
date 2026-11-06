# First Principles Thinker

**First principles: what are we solving?** We aren't choosing between q8 and fp32. We want a public demo that answers quickly, never visibly dies, and costs nothing new unless the owner approves it. The question also splits into two parts: whether the memory margin is real, and whether a 1,000-user claim is worth proving.

**Decision 1: adopt fp32, but replace the prediction with a measurement first.**
- The 0.6% margin comes from adding two peaks measured in different processes. That is arithmetic, not a measurement of the live process. Run the real uvicorn app with fp32 on a W1-style machine under the worst case: a max-size upload plus 20 concurrent asks.
- Sanity check: the 2.4 GB limit is our own budget, and the physical RAM is 4.01 GB. The 0.6% margin is therefore less dangerous than it looks, but only the live-process run will confirm it.
- Safeguards, all at no recurring cost:
  - Allow one upload parse at a time (a semaphore).
  - Add a guard that returns a 503 with a retry-after to new uploads when RSS exceeds 2.25 GB.
  - Cut the cap to 20 pages / about 10,500 tokens only if the measured peak exceeds 2.30 GB.
- Do not buy 8 GB for +$30/month. An upload cap solves the same problem for free.
- fp32 is worth adopting. The user-visible win is that the drained-throttle state improves from 9.96 s to 2.76 s per embed.

**Decision 2: skip S2 as written and rewrite the claim.**
- R1 was pre-registered as the rule for whether to run S2. Following it is not a waiver. Record S2 as "not attempted, R1 predicted failure on measured Fly numbers", with the numbers.
- Don't spend $1.25 to confirm a predicted failure.
- Replace "1,000 virtual users" with the measured per-machine capacity: 0.68 core-s per salted ask, which gives roughly 2 live asks/s on a performance-2x. Also state the drained shared-cpu-2x rate.
- A bigger-class run is a new pre-registration. The owner decides whether to spend on it.

**Order:**
1. Measure the live process.
2. Add the upload semaphore and RSS guard, with tests.
3. Adjust the cap only if the measurement requires it.
4. Report to the owner and get approval before deploying.
5. Record S2 as not attempted.
6. Rewrite the M5 claim.
