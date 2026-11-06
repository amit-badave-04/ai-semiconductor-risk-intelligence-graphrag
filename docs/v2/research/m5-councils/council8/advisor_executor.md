# Executor

**Executor: finish W1b in three runs, not one.**

(a) is dead. The driver's own rule says an INVALID boot is re-run unchanged, and that was done twice. The cause is deterministic: a throttled CPU under saturating asks can't embed 15.8k tokens in 1,200 s. A third try buys nothing.

(c) is dead. Those boots are INVALID under the rules you wrote before any run. Counting their memory data is the textbook case of gate-shopping.

(d) cannot go inside W1b. Changing the timeout or adding embed priority changes the thing under test.

**Monday morning:**
1. **Record the amendment before any run.** Quote the W1b text: "asks at the LIVE concurrency caps." The saturating reading was the driver's interpretation, not the quote. Name the evidence: two identical INVALIDs. Pick the replacement workload from an existing measured anchor, M4 G4's 5 asks during the upload, with in-flight never above 2. Choose the rate now, with no new results in hand. Leave the 2,400,000 kB / no-OOM / 180 s thresholds untouched.
2. **Run three fresh boots on the amended workload.** The extra boots cost cents, so don't mix boot 1 (saturating) with two paced boots. A mixed set looks selected.
3. **Report boot 1 and the two INVALIDs as supplementary stress data, not counted.** Their peaks (2,350,568–2,364,660 kB) show the memory margin is only 35–49 MB (1.5–2%) even under the harshest load. Say so plainly.
4. **A memory FAIL on the amended runs is a FAIL.** Pre-state this in the amendment.

**The timeout is a finding, and the owner hears it first.** Under saturating ask load the upload can time out, and q8 would be worse, so it is not a reason to reject fp32. Open a spec for it as its own item. The cheap options are a bounded timeout with a clear "retry" error, or a priority lane for upload embeds. Either needs a decision from the owner, and neither should be deployed before it.
