# Council 8: how to finish W1b after two INVALID boots (upload timeout under a saturating ask load)

Run 2026-11-06 (workflow wf_a478fd3c-9b4): five Sonnet advisors, five Sonnet peer reviewers (anonymized), Opus chairman.

## Framed question

CONTEXT. A public demo site (FastAPI on ONE Fly.io shared-cpu-2x 4 GB machine; the owner pays; a failed pre-registered gate is never waived by the builder, and choosing a measurement after seeing results is treated as gate-shopping unless justified and recorded). A faster query embedder ("fp32", 3.6x faster, retrieval identical, all parity and timing gates passed) costs +1.26 GB of RAM. Council 7 required one confirmation window, W1b, before adopting it, and the owner approved it. W1b as recorded: "the real app with the fp32 image on a fresh shared-cpu-2x 4 GB machine, staging Neo4j and mock LLM; M4's 30-page upload (about 15,849 tokens embedded as passages) plus mock-LLM asks at the LIVE concurrency caps; 3 fresh boots; PASS = on every boot max uvicorn VmHWM <= 2,400,000 kB, no OOM kill or restart, machine creation to healthy <= 180 s". The driver's own validity rules (written with it, before any run) mark a boot INVALID if the upload job does not reach ready, and say an INVALID boot is re-run unchanged.

WHAT HAPPENED. The driver interpreted "asks at the live concurrency caps" as keeping the in-flight cap (2 asks) SATURATED for the whole upload: about 118-154 asks per boot (each ask embeds a salted question with fp32 on the CPU, then streams from the mock). The machine is a shared-cpu-2x that throttles to about 15% after its burst (measured in window W0), and the upload's passage embedding shares the CPU with the query embeds.
- Boot 1: PASS. VmHWM 2,350,568 kB, no OOM, healthy 151.8 s, upload ready at 842 s, 118 asks all done.
- Boot 2, attempt 1: INVALID (upload job failed: code=timeout, the service's upload_embed_timeout_s = 1,200 s). VmHWM 2,364,488 kB, no OOM, healthy 147.3 s, 151 asks done.
- Boot 2, attempt 2 (re-run unchanged): INVALID again, same cause. VmHWM 2,364,660 kB, no OOM, healthy 124.7 s, 154 asks done.
- Boot 3: not run. The window is torn down (cost so far about $0.10 of the $0.25 cap; a re-run costs cents).
Context: M4's live gate G4 ran the same 30-page upload with 5 asks during it (q8 embedder, live machine): ready in 228 s, VmHWM 1,125,116 kB. Real live traffic is a few live asks per HOUR, so ~150 asks during one upload is far above anything the site sees, but "at the live caps" can be read as a stress test. With today's q8 embedder each query embed costs about 3.6x more CPU, so the upload starvation would be WORSE with q8: the timeout is a property of the throttled machine under saturating ask load, not of fp32. Memory in all four measured boots stayed under the limit by 35-49 MB (about 1.5-2%).

OPTIONS (not exhaustive):
(a) Keep re-running the unchanged workload until three boots are valid (may never happen; each attempt about 20 min, cents).
(b) Re-run boots 2 and 3 with a workload matching M4 G4 (5 asks during the upload, or a paced rate such as one ask every 30 s), recording the change and the reason; boot 1 stays as is.
(c) Count the two INVALID boots' memory data as valid for the memory criterion (the peak was observed over 1,200+ s of the harsher load, though the upload never completed so its last passages were not embedded), and run only boot 3.
(d) Change the product: raise the upload embed timeout, or make query embeds yield to upload embeds.
(e) Something else.
Also: is the upload timeout under saturating ask load a finding the owner must hear before adopting fp32 or any deploy, and what should be done about it?
QUESTION: How should W1b be finished so its result is trustworthy and not gate-shopping, and what (if anything) does the upload timeout change?
