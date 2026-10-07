# Council 6: decision 14 (embedder fix after the failed parity gate) now that S2 depends on it

Run 2026-10-07 (workflow wf_80e56302-edc): five Sonnet advisors, five Sonnet peer reviewers (anonymized), Opus chairman.

## Framed question

CONTEXT. Semigraph is a public demo (FastAPI on one Fly.io machine) answering questions over SEC filings: every live question is embedded on the server's CPU (Qwen3-Embedding-0.6B, ONNX int8 "q8", onnxruntime 1.29, ONNX_THREADS=1, embed_slots=1 = one embedding at a time) and matched against about 3,150 stored chunk vectors in Neo4j, then a graph context is built and an LLM writes a cited answer. The owner pays all costs personally and has a binding rule: a failed pre-registered gate is never waived or relabelled by the builder; only the owner may grant an exception.

THE EMBEDDER FIX (decision 4, approved with "its three gates and one paid benchmark run <= $2"): rebuild the same q8 model with onnxruntime's accuracy_level=4 (int8 compute path for the MatMulNBits nodes). Same weights, same stored corpus vectors (the corpus is NOT re-embedded), only the query side changes. Gate results so far:
- Gate 1, corpus parity on the production query prompt over 60 questions (top-1 retrieved chunk identical to today's model): 93.3% (56/60) against the pre-registered >= 95% -> FAILED. All four changed top-1s are near-ties under today's model. Cosine gate (mean/min) and top-8 overlap gate PASS. Against an fp32 reference, the patched model is less faithful than the shipped one on top-1 (95.0% vs 98.3%), cosine and worst-case regret, and marginally closer on mean top-8 overlap (0.9688 vs 0.9646). (Note: the answer path uses the top 8 chunks plus graph blocks, not only the top-1.)
- Gate 2, timing: 3.96x faster on the owner's desktop (1.25 s -> 0.32 s median, 50 interleaved calls). NOT yet measured on Fly (a "S6" timing run on a temporary Fly machine costs < $0.10).
- Gate 3, paid answer benchmark (<= $2, live models, the 41-question deployed benchmark with 3 judge votes; baseline "v2e": 40/41 correct, citation validity 1.0, 17/23 on a stricter subset; flips diagnosed per question; judge-only flips go to the owner; never re-rolled): NOT run. Its approval was given before gate 1 failed; the builder's reading is that running it now needs the owner's go.
The fix is held: the builder default is accuracy level 0 and nothing ships.

WHY IT NOW MATTERS MORE (new, from a council on 2026-10-07 and verified against the docs): the pre-registered staging load test S2 (one performance-2x machine, 2 dedicated cores; 1,000 virtual users; about 2.6 live uncached asks/s; time-to-first-event p95 <= 1.5 s; CPU <= 70%; at least 60 min; "a failure stays a failure; a bigger size is a new run") needs about 2.6 x 1.22 s = 3.2 core-seconds of embedding per second on today's model (more if a Fly core is slower; the plan assumed 19% slower, unverified). A 2-core machine cannot do that even with embed_slots=2 (ceiling about 1.6 asks/s), and the 1.5 s TTFB budget leaves only about 0.27 s beyond a 1.23 s embed. So on today's embedder S2 is predicted to FAIL (or end void), and the "one machine serves the 1,000-user test" claim cannot be made in M5. With the level-4 model (about 0.32-0.38 s per embed) the embedding load is about 1.0 core-s/s and S2 is feasible. S2's outcome also decides whether a shared state store or a bigger live machine is needed later (recurring $9-52/month). The live site today runs about 0-few live asks per hour; a single slow embed (about 1.2 s) is acceptable for current real traffic.

OPTIONS:
(a) Hold the fix permanently; run S2 on today's embedder and report the expected failure honestly (or skip S2 and say the 1,000-user test was not attempted).
(b) Run the Fly timing (< $0.10) and the paid benchmark (<= $2) as evidence, then the owner decides on an exception with the results in front of them; pre-register now what benchmark outcome would justify an exception.
(c) The owner grants a documented exception to gate 1 now and ships level 4.
(d) Change S2's machine/plan instead (e.g. a pre-registered bigger class as a new run), keeping today's embedder.
(e) Something else (e.g. another speed path that does not change retrieval; a different gate measure that matches how retrieval is actually used).

QUESTION: What should the builder recommend to the owner, in what order, and what pre-registered decision rule should the paid benchmark carry so the outcome is decided before the numbers are seen?
