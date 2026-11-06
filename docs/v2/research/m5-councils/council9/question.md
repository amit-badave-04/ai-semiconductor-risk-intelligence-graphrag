# Council 9: the upload ceiling after W1d (2026-11-06)

Framed question given to all five advisors (llm-council skill; Sonnet advisors and reviewers, Opus chairman):

Semigraph is a public GraphRAG demo (FastAPI on Fly.io, one shared-cpu-2x 4 GB machine in sin, live release v16; Neo4j on a second small machine). A visitor can upload a PDF (max 30 pages / 16,000 tokens) into a private workspace; the upload is parsed, chunked and embedded ON THE SERVING MACHINE with a local ONNX embedder (fp32 Qwen3-Embedding-0.6B, one thread), one upload at a time, at most 40 uploads a day; asks (paid LLM questions) share the same CPU but win it (the upload runs at nice 10). Uploads are rare; the owner is a solo developer who must approve any new recurring cost, wants honest public copy (strengths only on public surfaces, limits in internal docs) and is in the middle of M5b (a new React frontend).

THE PROBLEM. Fly's shared CPUs give a full core for a burst, then throttle to a pooled baseline (documented 12.5% for shared-cpu-2x). The embed timeout is 1,200 s live. A spec (owner-approved) raised it to 2,100 s, sized for a 12% sustained share with a 1.5x margin; it is built and pushed but NOT deployed. A pre-registered staging confirmation boot (W1d, 2026-11-06) on a FRESH shared-cpu-2x machine: after a 600 s burn the machine sustained only 10.1% (burn loop), and the embed ran at 5.54 tokens/s (an effective 5.5% of a performance core, half the burn's share). The 30-page fixture (60 chunks, 14,151 tokens) stopped at the 2,100 s ceiling with 50 of 60 chunks done. At that rate the fixture needs about 2,553 s and a max-size (16,000-token) upload about 2,887 s. Earlier boots (W1c) of the same image finished the fixture in 388 to 1,174 s (higher shares), so the outcome depends on the machine's throttle state, which nobody controls. A production validator requires ceiling x max_uploads_per_day <= 86,400 s (so the one upload slot cannot be held more than a day); at 40 a day that caps the ceiling at 2,160 s. Memory is fine (peak 2.28 GB of 2.4 GB limit).

OPTIONS ON THE TABLE (not exhaustive):
A. Raise the ceiling to about 3,600 s and lower max_uploads_per_day from 40 to 24 (3,600 x 24 = 86,400). $0. A max-size upload may take up to an hour on a throttled machine.
B. Keep 2,100 s and lower the upload cap (16,000 -> about 8,000 tokens, about 15 pages). $0. Halves the document size.
C. Move the serving machine to performance-2x 4 GB: +$51.53 per month (from $32.24 to $83.77). Uploads in about 3-4 minutes, asks faster too.
D. Embed off the serving machine: a second Fly worker machine (+$32 a month, an architecture change, a job hand-off) or an external embedding API (per-call cost, uploaded private documents leave the box; the retrieval index is schema-locked to this embedder so a different model needs a re-index).
E. Ship 2,100 s as is (strictly better than 1,200 s), state internally that max-size uploads can fail on a throttled machine, and read the new per-stage timing line from real uploads before deciding more.
F. Drop or loosen the day rule (ceiling 3,000 s at 40 a day).
G. Something else (for example a progress-based timeout with no fixed ceiling, splitting a big upload into resumable parts, or two ONNX threads, which the docs suggest will not help after the burst because the quota is pooled across vCPUs).

QUESTION: what should the owner do now, and in what order? What is at stake: a public demo feature that silently fails for the largest documents on a bad CPU draw, versus recurring cost, product cuts, or architecture work while M5b is in flight.
