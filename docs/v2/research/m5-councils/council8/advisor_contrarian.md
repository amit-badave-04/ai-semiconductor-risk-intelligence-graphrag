# Contrarian

Don't finish W1b by choosing a lighter workload now. Whoever picks the workload after seeing the results is gate-shopping, and here the likely result of the new workload is already known.

**Why (b) and (c) look like shopping**
- The pre-written rule says INVALID boots are re-run unchanged, and that was done twice. Swapping to a lighter load after two failures is the textbook move.
- A 5-ask load will almost surely pass memory. G4's q8 peak was 1,125,116 kB. Adding the 1.26 GB cost of fp32 gives about 2,385,000 kB, which is already within 15 MB of the 2,400,000 kB limit before any other overhead. Four boots clear the limit by only 35-49 MB, so that margin is the real finding.
- (c) fails because the upload never finished. Memory during the final passages was never observed, and the registered rule says those boots are INVALID.
- (a) is futile. Two attempts failed for the same reason, and a third will too.

**What I would do**
1. State plainly that W1b as registered is not passed. The result is: boot 1 PASS, boot 2 INVALID twice, boot 3 unrun.
2. Run boot 3 as registered, since it costs cents.
3. Add a separately named W1c with a paced load, committed before it runs, and keep the saturated result alongside it. Let the owner decide whether W1c substitutes, because the builder cannot waive a gate.
4. Run the saturated workload on q8 as a control. "Worse with q8" is an inference, not a measurement, and it is the builder's best argument for shipping.

**What the timeout changes**
- It is a finding the owner must hear before any deploy. Boot 1 became ready at 842 s against a 1,200 s timeout, so boot 1's pass was only a modest-margin one. The other two boots missed the timeout by a wide margin.
- It is also an abuse case. The cap is 2 in-flight asks, and the harness runs them from separate addresses. If a single address can hold a slot, one scripted client can starve every upload on this machine. Real traffic of a few asks per hour does not matter for a public demo.
- I would not touch the product (d) inside W1b, because that changes the thing being measured. Fix it afterwards with its own spec, for example by making query embeds yield to upload embeds.
