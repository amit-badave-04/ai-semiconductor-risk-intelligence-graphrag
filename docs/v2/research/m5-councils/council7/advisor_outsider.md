# Outsider

**Outsider view: two numbers are carrying more weight than they can bear.**

**Decision 1: adopt fp32, but not on a 0.6% margin.**
- 14,872 kB of margin is the sum of two numbers from two different runs. No run-to-run spread is given, so I can't tell that margin from noise. The speedup is real: a visitor waits about 10 s just for the question embed on the throttled live machine, and fp32 cuts that to about 2.8 s. So holding at q8 is not free either.
- Order:
  1. Run the real uvicorn process with fp32 on a one-off machine, with a max-size upload plus 60 questions, three times. Report the max and the spread. Require at least 5% (120 MB) of margin under 2.4 GB.
  2. If that fails, lower the upload cap to about 20 pages / 10,000 tokens, re-measure, and publish the new cap.
  3. Add a free memory guard that returns "busy, retry in a minute" for new uploads when RSS is above about 2.2 GB.
  4. Ship fp32.
- Don't ask the owner for the 8 GB machine (+$30/month) unless steps 1–3 still leave under 5% margin. Spending money to widen a margin that free settings can widen is the wrong first move.

**Decision 2: don't run S2, and don't buy a bigger class.**
- Your own rule R1 fails on your own measurements. Spending $1.25 to confirm the arithmetic buys little. Record S2 as "not attempted, R1 failed (1.39–1.77 vs 1.1 core-s/s)". Don't reword that failure.
- "1,000 virtual users" means nothing to a visitor. Replace it with a measured sentence such as "one machine sustains N questions per second". Measure N with a small, newly pre-registered ramp (cap $0.30) on shared-cpu-2x in its drained state, since that is the state visitors actually get. Don't quote the 0.33 s fresh-burst figure publicly. It lasts about 2 minutes.
- Changing the claim is the owner's call, and the page must say the original claim was dropped.
- Order: record the R1 failure, do the memory re-measure, ship fp32, run the ramp, then take the claim change to the owner.
