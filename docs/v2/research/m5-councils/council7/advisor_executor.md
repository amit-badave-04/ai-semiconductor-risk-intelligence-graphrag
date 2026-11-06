# Executor

**Decision 1: adopt fp32, but not on the paper prediction.** The 14,872 kB margin sums two separate runs, and nobody has measured the real uvicorn process under this load. The 2.4 GB limit is a budget. Real OOM is about 1.5 GB further away (4.01 GB total, about 2.43 GB used including the 41 MB parse child). Even so, measure the real process before shipping. Monday:

1. Put fp32 on the staging machine (W1 harness, about $0.13). Run the real app with a max-size upload (15,849 tokens) while 20 asks stream. Read VmHWM. Pre-register the gate before the run: peak <= 2,300,000 kB. A fail stays a fail.
2. In the same run, test the free levers. Set ORT `enable_cpu_mem_arena=False` and cap passage batches at about 2,048 tokens per `session.run`. Keep the 30-page cap.
3. Add a guard that costs nothing. Serialize uploads, one embed at a time. Return 503 with Retry-After when MemAvailable < 700 MB or RSS > 2.2 GB.
4. Ship behind `EMBEDDER=fp32|q8`. Rollback is then one `fly secrets set`, not a rebuild.
5. Do not go to 8 GB (+$30/month). Ask the owner only if step 1 still fails after step 2.

Holding on q8 is the worst option. A drained machine makes every question wait about 10 s instead of 2.8 s.

**Decision 2: skip S2.** Record "not attempted: R1 failed on measured Fly numbers (1.39-1.77 vs 1.1 core-s/s)". R1 is the pre-registered rule, so spending $1.25 to confirm it buys nothing. Do not pre-register a performance-4x or two-machine run. It implies recurring cost for a 1,000-user claim a demo does not need.

Change the M5 claim to what was measured: live asks per second per machine, embed-bound, for both the fresh and drained states, taken from the W1 numbers. State "single machine by design; shared state store not built, not needed at this rate". Run a pre-registered measurement (mock LLM, cap $0.30) only if the owner wants a published asks/s number.
