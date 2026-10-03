The biggest upside is hiding in S2, and Valkey is the thing that would bury it.

The async rewrite is the real unlock: it turns a machine that holds 2 LLM streams into one that holds dozens (the brief puts the gate at ~40 concurrent streams). If ONE performance-class machine passes the 1,000-virtual-user gate, the claim is stronger than a fleet story: "1,000 virtual users on one machine, with a hard $10/day spend cap." That is a cost-efficiency headline a buyer can repeat. A Valkey fleet would turn it into a "we needed a distributed system" story and hide that the code got fast. Do not give up that framing for an $8.65/month store nobody has shown we need.

Second upside: S7 is a free capability result. About 12 reads/s and 9 writes/s of service state is currently an unmeasured assumption. If Neo4j Community on a 1 GB machine carries 5, 10 or 20 asks/s, we get a published capacity curve and can say "no extra infrastructure." If it does not, we have the exact number that justifies Valkey, which is the only evidence the owner should be asked to approve a new recurring cost and vendor against.

Third: the `StateBackend` protocol with in-process and neo4j backends is the seam that makes later Valkey, a second region or a multi-machine fleet a drop-in. The in-process backend makes the daily count, spend ceiling and in-flight cap atomic by construction, with no Lua. That frees the worker who would have owned three backends and Lua parity tests for buyer features and frontend, which is where the product value is. (Assumption: that capacity is redeployable.)

Policy: accept the $10 cap with reserve/reconcile, the three-level kill switch, the HMAC pepper and /64 bucketing. Fail closed on paid asks if state is unreachable.

Order: async rewrite, embedder fix, protocol and two backends, S2 on one machine, then S7 at 5/10/20 asks/s.

Recommendation: S2, with the evidence trigger written down now: add Valkey only if S2/S7 show one machine cannot pass, or the owner asks for a multi-machine live fleet.
