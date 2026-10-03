The question "which live fleet makes live == proven?" is the wrong one. Live == proven matters only if the live site is meant to serve 1,000 users. It is not: the per-IP window is 5 asks/10 min, the global ceiling is 150 paid asks/day, and real load is a handful of people. Admission control already refuses the load the gate describes. B and C would spend new recurring money (+$51.53/mo for B, which needs owner sign-off) to buy a property the live policy makes unreachable. The buyer is evaluating whether the architecture can scale, so what the owner needs is a true sentence saying exactly that.

(1) Adopt the embedder fix. It is an attribute flip on the same weights: 1.22 s to 0.31 s, query cosine 0.9988, top-1 identical for 96%. Gates: the paid benchmark (~$1.5-2) shows no answer regression, and the speedup is re-measured on Fly hardware, not only on the desktop. Fail either one and it does not ship.

(2) Live stays one shared-cpu-2x. It sustains 12.5% of one core, so the "CPU <= 70% of the class's sustained CPU" gate cannot be met on the shared class. The gate must name the performance class it is proven on.

(3) Wording: "On a temporary staging fleet of N performance-class machines, with a mock LLM, under the pre-registered load model, the app tier held 1,000 virtual users with p95 time-to-first-event <= 1.5 s, 0 drops and errors < 0.5%." State that provider capacity was not tested and that the live demo is one shared-cpu-2x with caps. Never write "the site supports 1,000 concurrent users."

(4) No hosted embedding. The fix removes the problem it would solve, and it adds a vendor and an unverified vector space.

(5) Biggest risk: the proof does not transfer. Non-embedding CPU per ask is unmeasured, and the 19% Fly slowdown comes from document embedding only. Detect it early by running baseline spike S2 on Fly hardware before building staging, and by executing the scale-up runbook once (scale live up, smoke test, scale back down) so it is tested rather than only written.

Recommendation: Option A, with the embedder gated on parity, the benchmark and a Fly timing check, the gate worded as a staging-fleet claim, and one rehearsed scale-up drill.
