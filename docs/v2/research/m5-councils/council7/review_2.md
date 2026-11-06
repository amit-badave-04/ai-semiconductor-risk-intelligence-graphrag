# Peer review 2

**1. Strongest: E.** It is the only response that goes after the root cause, the 2.06 GB of private weights, by testing mmap'd external-data weights. It also adds an embed cache and handles spend in the right order: record S2 as not attempted, pre-register S2b, then ask the owner. D is close behind, because it shows that cutting the upload cap saves tens of MB at most. E should not ship before its soak.

**2. Biggest blind spot: B.** Its main lever, cutting the cap to 20 pages, barely moves the peak. It would also publish about 2 asks/s from performance-2x, which the site does not run. Drained shared-cpu-2x manages about 0.4 asks/s (1 / 2.76 s).

**3. What all five missed:**
- Every proposed gate (2.20 to 2.30 GB) sits below the 2.385 GB prediction, and the weights are fixed. The gates therefore fail by construction. The responses should say plainly that 8 GB or smaller weights is the real alternative.
- Upload guards on absolute RSS near 2.2 GB may sit below idle fp32 RSS and refuse every upload. Use MemAvailable instead, calibrated on idle RSS.
- There is an untested middle path. The +1.26 GB is the 196 linear layers (28 × 7). Test a native int8 matmul export, or dequantizing only some layers, against the same parity gates.
- After an OOM kill, reloading 2.4 GB on a throttled machine could loop. The image also grows about 4x.
- The owner's strengths-only public-surface rule means capacity limits belong in the internal report, not on the page.
