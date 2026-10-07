# First Principles Thinker

The question isn't "level 4 or not." S2 is a means to a claim, "one machine serves 1,000 users," and the site gets a few asks per hour. Today's 1.2 s embed costs real users nothing. The only thing the fix buys is the ability to make that claim. Don't ship a retrieval change to enable a test of a claim nobody needs yet.

Gate 1 also measures sameness to the shipped model, not correctness. Against fp32, level 4 is worse on top-1 (95.0% vs 98.3%), and its 93.3% parity is mostly near-ties. So no gate-1 waiver, now or later.

**Order I'd recommend:**
1. **Fly timing run (< $0.10), now.** It needs no approval beyond the existing budget. If level 4 on a Fly core isn't clearly under about 0.5 s, the whole question dies.
2. **Free first, before the paid run:** check whether the 1.22 s includes padding or a long instruction prefix. Cutting wasted tokens changes no weights and no retrieval parity, which is option (e), and it may close most of the gap.
3. **Ask the owner for the benchmark "go"** with the rule below. Don't run on inferred approval.
4. **If the fix stays held:** don't run S2 on a predicted failure. Record "S2 not attempted on this embedder," and pre-register a smaller claim.

**Benchmark rule, written before the run:**
- The run only qualifies an exception request. It never converts the gate-1 failure into a pass.
- Request an exception only if all hold: correct ≥ 40/41; citation validity = 1.0; strict subset ≥ 17/23; and zero flips diagnosed as retrieval-caused regressions.
- Any retrieval-caused regression means the fix stays held, with no exception request.
- Judge-only flips go to the owner. No re-rolls.
- Be explicit that 41 questions can't detect small differences, so a pass is "no evidence of harm," not "equivalent."

A usage-matched gate (top-8 vs fp32) can be registered only for future changes, never to rescue this result.
