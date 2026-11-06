# Contrarian

**Decision 1: adopt fp32, but the 0.6% margin is not a pass I would defend.**

The 14,872 kB margin is the sum of two numbers measured in different processes under different workloads. M4's 1,125,116 kB peak came from a q8 uvicorn. The +1.26 GB delta came from a single-model harness. Nobody has measured the live uvicorn running fp32. At 0.6%, glibc fragmentation, an ORT arena regrowth or one concurrent question during an upload decides whether it passes. A passing prediction is not a measurement.

Lowering the upload cap is the weakest option. Weights are 2.06 GB of the 2.13 GB peak, so cutting tokens saves tens of MB at best. The upload-parse child is capped at 1 GiB by RLIMIT_AS. 2.385 GB plus 1.0 GB is 3.39 GB of 3.9 GB, so the real worst case leaves almost nothing for the kernel and Fly's agent.

Order of actions:
1. Build fp32 behind a flag, with the ORT CPU arena and mem-pattern disabled and a batch cap of 4 passages.
2. On a staging machine, measure the real uvicorn with a max-size upload plus 4 concurrent questions, 3 runs. Pre-register the gate as peak ≤ 2.20 GB, a margin of at least 8%.
3. If it passes, ship with a one-upload-embed semaphore and a refusal when MemAvailable < 600 MB.
4. If it fails, hold q8. Do not waive the gate. Ask the owner about 8 GB (+$30/month) only then.

**Decision 2: skip S2.**

R1 fails on both estimates (1.39 and 1.77 core-s/s against 1.1), so R1 already says not to run it. Spending $1.25 to confirm a known failure buys nothing. Record it as "not attempted, R1 failed on measured Fly numbers". Do not pre-register a performance-4x run, because that argues for recurring spend without any demand evidence.

Instead, rewrite the M5 claim as a measured per-machine live-ask capacity on shared-cpu-2x, throttled state. The 1,000-user claim is dropped, not reworded. The per-address spend meter is the real limiter on the public site anyway.
