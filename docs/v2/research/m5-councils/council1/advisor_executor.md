**Executor: Monday-morning order, each step with a pass/fail check**

1. **Embedder fix.** Add `accuracy_level=4` to the builder script and rebuild the image. Commit the 53-question parity check as a CI test (pass: query-vector cosine mean >= 0.998, top-1 identical >= 95 %; measured today: 0.9988 and 96 %). Run the ~$1.5-2 answer benchmark and compare to the current baseline. Embed time drops from 1.22 s to 0.31 s with no vendor and no recurring cost. ORT issue #29849 is settled by our own parity test, not by argument.
2. **Day 1 on Fly.** Start one temporary performance-1x and run embedding timing plus baseline spike S2. The unmeasured items (Fly timings, CPU per ask outside embedding) decide fleet size, so size nothing before this.
3. **Staging run.** Size the staging fleet from S2, run steady plus the 60-min soak, destroy the fleet, archive the report.
4. **Runbook rehearsal.** Execute the scale-up runbook once. An untested runbook is a claim, not proof.

**Live fleet:** Option A, one shared-cpu-2x. It is the only path that needs no owner spend decision. State the arithmetic plainly: the derived warm embed load (0.33 core-s/s) is about 2.6x what a shared-cpu-2x sustains (0.125 core-s/s), so the live machine cannot carry the gate load. Option B becomes a one-line owner ask after step 3, not a Monday blocker.

**Gate wording:** "On a staging fleet of <exact class x count>, mock LLM, 1,000 VUs per the pre-registered model: TTFE p95 <= 1.5 s, >= 40 streams with 0 drops, errors < 0.5 %, CPU <= 70 %. The live demo runs one shared-cpu-2x behind admission caps; scaling to the proven fleet is a rehearsed runbook. Provider capacity is not tested."

**Hosted embedding (D):** no. It needs a new vendor, plus template, dimension and cosine checks, to fix something a free attribute already fixes.

**Biggest risk:** per-ask CPU on Fly is above the derived figure. The 19 % slowdown is an assumption and non-embedding CPU is unmeasured. Detect it in step 2. If measured warm core-s/s exceeds the derived figure, resize the staging fleet before the gate run. Never relabel the result.

**Recommendation:** Option A, with the day-1 Fly measurement (step 2) as the first hard gate.
