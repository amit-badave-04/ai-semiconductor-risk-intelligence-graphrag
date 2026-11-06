# Council 9: anonymized peer reviews

Letter mapping (hidden from the reviewers): Response A = The Outsider; Response B = The Contrarian; Response C = The Executor; Response D = The First Principles Thinker; Response E = The Expansionist

## Review 1

**1. Strongest: B.** It is the only response that treats W1d as a failed pre-registered gate and records it as one. It also sees that n=1 on the throttle state is no floor, so any new ceiling is another guess. Its admission control (project the finish time after 3 chunks, abort in under a minute) fixes the actual harm, which is a doomed upload burning 35 to 60 minutes before it says anything. The 10k cap is arithmetic from the measured 5.54 tok/s. D is close, but its 8k cap and its "5.5 as the floor" are tighter than the evidence supports. A's point that the 50 finished chunks are thrown away is also valuable.

**2. Biggest blind spot: E.** It leaves the known max-size failure in production through all of M5b. It prices the ephemeral performance worker at about $0.01 per upload and ignores the real cost, which is a new trust boundary. That means cold start, secrets, and writing vectors back across machines for private PDFs. It also wrongly says an hour-long upload "fails silently". C is second: a 1.25x margin on one sample, plus an hour of contention with paid asks.

**3. All five missed:**
- The embedder uses one thread, so performance-1x with 4 GB was never priced. C was costed on 2x.
- Nobody tried making the work cheaper, for example length-sorted batching, less padding, or int8 for upload embeds.
- Nobody tested whether the throttle depends on machine age or burst-balance state. One repeat boot would show it.
- Nobody noted that the owner must re-approve the amended spec.

## Review 2

**1. Strongest: B.**
- It is the only response that records W1d as a failed pre-registered gate instead of waiving it.
- It sees that A, C and E all fail at the end, after burning up to an hour of throttled CPU.
- Its early projection abort (measure tok/s over the first 3 chunks) fixes that failure mode.
- Its cap arithmetic is checkable: 10,000 tokens against about 11,600 tokens of capacity at the worst observed rate.

A's checkpoint-and-resume idea is good, but A calls it "a small change" without evidence.

**2. Biggest blind spot: C.**
- It treats a single 5.54 tok/s sample as a floor and claims a 1.25x margin.
- It ignores that an hour of throttled upload drains the pooled quota the paid asks need. Nice 10 doesn't help against the hypervisor throttle.
- It adds no early abort, and it deletes the owner-approved 40 uploads a day without asking.
- E is a close second. It calls an hour-long wait a "silent" failure, then defers its own worker design.

**3. Missed by all five:**
- **Representativeness of the test.** W1d's 600 s burn deliberately drained the burst balance before the embed. A production machine that sits idle between rare uploads may start with credit, which would explain W1c's 388 to 1,174 s. Nobody asked whether 5.5% is the production worst case or a test artifact.
- **Cutting compute per token.** The embed got half the burn's share. Nobody tried a q8 ONNX model, shorter padding or batching. Production previously used a q8 embedder, and this is the only $0 fix that attacks the cause. It needs a retrieval-quality check, because the index is schema-locked.
- **Reading the throttle state.** The machine could read its own cgroup CPU throttle counters at upload start and defer or warn.

## Review 3

**1. Strongest: B.** It is the only response that counts W1d as a failed pre-registered gate instead of waiving it. It also sees that a doomed upload fails at the end, after burning 35 to 60 minutes of throttled CPU. And it notes that nice 10 does not protect asks from the pooled hypervisor quota. Its plan is deterministic: early rate-projection abort, a cap of about 10k tokens, and the 2,100 s ceiling kept. The cap arithmetic is explicit (5.54 × 2,100 ≈ 11.6k tokens). Its one weakness is that the early rate may be optimistic if the burst balance has not drained yet. D is a close second.

**2. Biggest blind spot: C.** It treats one 5.54 tok/s draw as a floor and sets a 1.25x margin on it, so the next draw can still fail, now after an hour. It also ignores that an hour-long upload drains the quota the paid asks share. It calls A "no new code" and "loud failure" without admission control, and it adds a 60-minute silent wait. E is also weak: it ships a known failure and defers the real fix.

**3. All five missed:** the embed got half the burn's share (5.5% vs 10.1%). Nobody asked why. Padding, batching, ONNX threading or a q8 vs fp32 difference may hide a free speedup. Nobody weighed checkpointing finished chunks except A, and A did not link it to this gap. Nobody noted that W1d deliberately drained the balance, so it measures the floor, not a typical production draw.

## Review 4

**1. Strongest: B.** It is the only response that treats W1d as a failed pre-registered gate and not something to route around. It adds admission control: measure tokens/s over the first 3 chunks and abort within a minute, so a doomed upload doesn't burn 35 to 60 minutes. It sizes the cap from the measured floor (5.54 × 2,100 ≈ 11,600 tokens, so a 10,000 cap), keeps the day rule, and defers C and D. D is close, but its 7,700-token cap stacks a 1.5x margin on an already-worst-observed rate. A's chunk checkpointing is a good idea but is not "small."

**2. Biggest blind spot: C.**
- A ceiling of 3,600 s is only a 1.25x margin on an n=1 rate.
- It never asks what an hour of throttled embedding does to paid asks. The nice 10 setting wins the scheduler but not the hypervisor quota.
- It holds the single upload slot for up to an hour.
- It amends an approved spec to fit the failing number and calls W1d "evidence" instead of a failed gate.
- "No new code" contradicts its own loud-failure and progress-feed work.

E also leans on an unverified $0.01-per-upload figure.

**3. All five missed:**
- Nobody reduces CPU work per token (length-sorted batching to cut padding, chunk overlap, or int8 after an index-compatibility check). That is a cheaper lever than any ceiling.
- Nobody measured ask latency under a throttled upload.
- Nobody noticed that throttle state depends on prior idle and burst balance, so a warm-up or timing policy might be controllable.
- Nobody said whether a timed-out upload consumes the visitor's daily slot.
- One slot means a slow upload blocks every other visitor.

## Review 5

**1. Strongest: B.** It is the only response that records W1d as a failed gate and sees that a ceiling sized from one throttle sample will fail again. Its admission control (project the finish time from the first chunks, abort within a minute) targets the real harm, which is a 35-minute failure the visitor never sees coming. It also notes that an hour-long throttled upload drains the pooled quota the paid asks need. Its weakness is that early-chunk rates can mislead if throttling starts mid-run. A's checkpointing of finished chunks is the best idea B lacks.

**2. Biggest blind spot: C.** It treats n=1 as a floor and accepts a 1.25x margin on it. It ignores an hour of degraded asks. It equates "no new code" with safe. It also amends the spec to pass the gate instead of treating the failure as informative. E is second: its per-upload worker ignores cold start, the job hand-off and the owner's recurring-cost approval rule.

**3. What all five missed:**
- **Whether W1d is representative.** A fresh machine after a 600 s burn has drained its burst balance. An idle production machine may behave differently. Measure the live machine's real idle-then-upload pattern before sizing anything.
- **Estimating before starting.** Token count is known before embedding begins, so the system can show an ETA or reject the upload up front.
- **Async UX.** Run the upload in the background and tell the visitor to come back later, so no session has to stay open.
