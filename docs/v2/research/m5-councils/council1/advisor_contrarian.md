The fatal flaw is that the gate is unpassable on today's machine, so Option A's proof describes a machine the buyer never touches. Shared-cpu-2x sustains 12.5% of one core, so the gate's "70% of sustained" is about 0.09 core-s/s. Warm embedding alone is ~0.33 core-s/s after the fix (~1.0 cold). That already assumes the Fly core is 19% slower, which is unverified. The burst balance caps at 500 s, so a 60-minute soak drains it. Under A, a buyer who asks "is this what you tested?" hears "no".

Second, the derived table rests on unmeasured inputs. CPU per ask outside embedding (SSE, Neo4j client, verification) is unmeasured, and so is everything on Fly hardware. One uvicorn process (one event loop) is untested at ~4.76 asks/s plus 40-60 open streams. The brief also never scales the 1 GB shared Neo4j machine, which is the likelier bottleneck. "Database reads proven" may only mean Neo4j was not the weak link on staging.

Third, the fix has strong evidence (cosine 0.9988, top-1 identical for 96%). It still has no answer benchmark, and ORT issue #29849 is open. Adopt it, but do not waive the $1.5-2 benchmark.

Reject D: it adds a vendor with unknown template and precision, overrides the owner's local-embedding choice, and solves a problem the fix removes. Reject E for the same reason A is weak: it proves a fleet nobody runs.

Biggest risk: wording drift. The live site admits 150 paid asks/day and the load test uses a mock LLM, so a bare "1,000 concurrent users" is not honest. The wording must name the machine class, the mock LLM and the admission settings, and say the live fleet differs. Detect it early: run baseline spike S2 on a Fly machine first. If non-embedding CPU per ask rivals embed cost, the whole table is wrong.

Recommendation: adopt the fix behind parity, benchmark and on-Fly timing gates, run S2 first, and take A with a configuration-scoped claim, leaving B for the owner to decide only if S2 shows the live class fails.
