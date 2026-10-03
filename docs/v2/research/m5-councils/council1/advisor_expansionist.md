**The Expansionist**

The embedder fix is the biggest lever in the brief, and it is undervalued. A one-attribute change takes a query embed from 1.22 s to 0.31 s (4x), and retrieval parity is already strong (cosine 0.9988, top-1 identical for 96 %). That is a latency win for every buyer click, not just for the load test. Today's ~1.5 s embed would consume the whole 1.5 s time-to-first-event budget. Adopt it. A $1.5-2 benchmark re-run is trivial next to a 4x gain.

The bigger prize is making live equal proven (Option B). Arithmetic from the brief: shared-cpu-2x sustains 12.5 % of one core, while warm embedding alone is ~0.33 core-s/s (derived, unverified on Fly). So the current class cannot be the proven class. A performance-class machine can. Paying +$51.53/month turns the claim into a sales line: "the machine serving you right now was load-tested at 1,000 virtual users." Option A, "the live fleet is not the proven fleet", throws that line away to save the price of a modest dinner.

Wording that wins: name the exact machine class, the pre-registered load model, the mock-LLM scope, and any failed run reported as a failure. That pre-registered honesty is itself a credibility asset for a buyer.

Adjacent upside: the harness, the mock-LLM rig and the parity method are reusable as a "capacity proof" artifact. The accuracy_level finding (ORT's int8 path unused) is worth a short engineering write-up. Local embedding stays a differentiator (assumption: buyers value that document text and queries are not sent to a third party). Reject D: a new vendor, with an unverified template, and the fix removes the reason for it.

Risk to watch: the Fly core is assumed 19 % slower than the desktop and per-ask CPU outside embedding is unmeasured. Spike S2 on a performance-class machine detects it early. If CPU headroom is large, drop to a cheaper performance class (for example performance-1x 4 GB, $57.12) and prove that one instead.

Recommendation: Option B, ship the embedder fix gated on a re-run of the answer benchmark, and run live on the performance-class machine that S2 and the gate actually prove.
