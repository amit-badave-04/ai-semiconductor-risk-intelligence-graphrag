Read cold, the core problem is the word "proven." A buyer hearing "proven for 1,000 concurrent users" pictures 1,000 people on the live site. What is measured: 1,000 virtual users each asking once every 2-5 minutes (~40 simultaneous live answers), a mock LLM, on machines destroyed afterwards. The live site also caps 5 asks per IP per 10 minutes and 150 paid asks per day, which the test's 2.6 live asks/s would exhaust in about a minute (my arithmetic, assuming live asks are the paid ones). Each gap is obvious to an expert and reads as sleight of hand to a newcomer. Option A is worst: the proof sits on a fleet that does not exist at the URL being demoed.

The live machine sustains 12.5% of a core, while warm embedding alone is derived at 0.33 core-s/s, so today's class cannot meet "CPU <= 70% of sustained" on its face.

"Retrieval parity" is expert shorthand. Plainly: 17% of test questions get a slightly different top-8 set, and nobody has measured whether answers got worse (the $1.5-2 benchmark). Adopt the fix (1.22 s to 0.31 s, about 4x) only after that benchmark passes. Hosted embedding (D) adds a new vendor and unverified vector compatibility to fix what the local fix already shrinks; reject.

Gate wording: never bare "1,000 concurrent users." Say "1,000 simulated users, each asking about once every 3.5 minutes (~40 simultaneous live answers), mock LLM, app tier on [named machine class]; the public site is additionally rate-limited."

Biggest risk: the claim being quoted without its footnotes. Detect early by having someone uninvolved paraphrase the claim back and checking they say "1,000 people at once."

Recommendation: Option B (fix gated on the answer benchmark, live runs the proven machine class, claim worded as above).
