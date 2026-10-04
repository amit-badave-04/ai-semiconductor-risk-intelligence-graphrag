# M5a build log (append-only; internal)

One entry per step, newest last. Each entry says what was done, what was measured, what is still open. Paid and
staging spend is listed so the total can be reconciled against the ledger and the Fly invoice.

## 2026-10-03 — Step 0-pre

**Embedder fix: gate failed, fix held.** Commit 2ed0a40 adds the tools (builder `--verify-patched`, corpus-parity script,
timing script, 122 tests) and the evidence. On the production prompt over 60 questions the pre-registered top-1 gate
fails: 0.9333 against >= 0.95 (cosine and top-8 gates pass). The builder's `--accuracy-level` default is 0 and the
Dockerfile is unchanged, so the image build still produces today's model. Owner decision 14 is open
(`M5_DECISIONS.md` section 7). My first parity numbers (96% over 53 questions) were wrong (wrong query prompt, subset of
questions) and are withdrawn (section 1.4 item 12). Opus verification of the tooling: DONE WITH WARNINGS, four
warnings fixed in the same commit.

**litellm async streaming probe (live, `openai/gpt-6-luna`, litellm 1.100.0 in the serve venv `C:\temp\sg_sv`).**
Cost: two small calls, well under $0.01. Results:
- `acompletion(stream=True, stream_options={"include_usage": True})` returns a `CustomStreamWrapper` (async iterator
  with `aclose`); usage arrived (22 prompt, 98 completion tokens) on the stream's last chunks, 82 chunks in total,
  time to first chunk 2.2 s, total 2.7 s.
- Closing a half-read stream: `aclose()` returned in 0.04 s and raised nothing.
- **Finding:** this model rejects `max_tokens` ("use `max_completion_tokens`"). The sync path avoids it through
  `completion_params(model, max_tokens)` in `answerer.py`; the async twin must use the same helper, not a bare
  `max_tokens`.
- **Not verified:** that the upstream HTTP connection is actually closed by `aclose()` (not observable from the client
  side here); the async path's disconnect test uses a stub that records `aclose()` instead, and the real-connection
  claim stays unmade.

**Delegated and running (Sonnet workers, disjoint files):** test classification for promotion gate 1
(`docs/v2/M5B_TEST_CLASSIFICATION.md`); pre-M5 replay fixtures from the unmodified sync code
(`tests/data/{agent,workspace}_events_pre_m5.json`, recorder, replay test).

**Open owner items (none blocks the async-path increment I2):** decision 14 (embedder), D (drain shape; recommended
option b), W (workspace token read before Turnstile), P (how the S2 load test reaches the live-ask rate when the question
pool becomes cache hits), the paired benchmark arm (default no).

Spend so far in M5a: < $0.01 (the probe). Staging machines: none created.

## 2026-10-03/04 - I2 async answer path: seams, twins, review findings

**Committed:** shared seams (471aa92: LimitedEmbedder with a 2,000-vector cache, named limiters, loop-lag monitor,
query_vec on the retrievers) and the three async twins (ec083e7: SEC, workspace, agent; local until the wiring lands).
Each twin was built test-first by a Sonnet worker and reviewed adversarially by Opus (four passes in all); parity with the
sync writers is tested event-for-event (parametrised sync-vs-async scenarios, the recorded answer fixture, the pre-M5
agent and workspace fixtures).

**Findings that changed the design (all fixed or tracked):**
- A cancelled anyio scope is not delivered when a SHIELDED thread hop returns (no checkpoint), so a twin could start a paid
  call for a client that had already left. Fix: a checkpoint after every thread hop and before every paid call (an AST pin
  keeps bare run_sync out). A gated-thread probe over all three twins confirms no paid call starts after a cancel, except one
  remaining case, below.
- Open at the time of writing: a disconnect during the agent's PREFETCH still buys planner call 1 (the sync stream has the
  same gap); being closed in the wiring workflow by a stop check before every planner call.
- CPU-bound regex/Python work holds the GIL, so a worker thread does NOT protect the event loop from it. strip_links_images
  (workspace.py) is quadratic: 222 ms per 10,000 characters, 886 ms per 20,000. The deterministic checks cost 7 to 21 ms per
  ask on the dev machine. Consequence: the regex is being made linear (differentially tested against the old one, with
  security property tests), and the twin docstrings are being corrected to say what a thread hop does and does not buy.
- Native task.cancel() (server shutdown) is best effort for cleanup by design; the route cancels through an anyio scope on a
  client disconnect, which is fully covered.

**Wiring (in progress):** PaidStream (events + idempotent shielded finalize as the response's background task), the
anyio.CapacityLimiter replacing answer_slots, ledger row written BEFORE the terminal event as in the sync code, conversion of
the sync-path tests, then real-uvicorn concurrency and disconnect tests and a review panel. Nothing is deployed.

## 2026-10-04 - I2 hardening round and the Opus panel

**Hardening (three workers) and panel (verifier, security reviewer, FastAPI reviewer), then fixes and a re-verification.**
- A quadratic regex family in the deterministic answer checks (verify.py percent patterns, 156 ms at 5,000 characters of
  comma-digits) and, worse, an EXPONENTIAL one in removal_claims (_ECHO_RE: about 150 characters of repeated "none" took
  16 s; every further repeat doubled it) were replaced by linear code. The _echo rewrite is a hand-written matcher; it was
  checked equal to the old regex by the worker (about 550,000 strings) and again independently by the re-verifier (300,000
  fresh-seed strings, 44,312 matches, 0 differences, a planted change was caught). strip_links_images is linear and
  idempotent (a fixed point reached in at most MAX_STRIP_ROUNDS rounds, then a defang fallback).
- HIGH (security review): a client that stops reading could hold an answer slot indefinitely, because sse-starlette's
  closing empty-body send has no timeout. Fixed in two parts: finalize runs at the end of events(), and PaidResponse bounds
  the closing send by send_timeout_s. Reproduced on a real uvicorn before the fix (slot held at 25 s, a second ask got the
  busy event) and confirmed fixed after (slot free at 1 s, closing send cut at 2.0 s).
- Ledger edge cases: no second row once the terminal row exists (a failing cache write, a twin raising after done); when
  the client never received its terminal event it still gets the generic error (the page waits for done or error);
  _started is set before the twin is called; the abandoned row is written before the twin/agent join (up to 12 s).
- Privacy: a workspace ask logs only exception class names; the sse_starlette logger is pinned to INFO (its DEBUG chunk
  lines carry the question and the workspace answer); workspace questions bypass the embedding cache (a cache hit is a
  timing oracle and would outlive the 24 h workspace); the agent twin's exception log is redacted.
- Config: send_timeout_s, embed_slots, db_thread_limit and loop_lag_warn_ms have a minimum of 1 (send_timeout_s=0 would drop
  every paid stream after retrieval); .env.example lists them. Cached answers no longer take a thread-pool hop.
- The real-uvicorn tests run with --loop asyncio (production's loop: deploy/requirements-serve.txt was compiled on Windows
  without uvloop, so the image uses asyncio's default loop; the unit CI job has uvloop). Whether production should get
  uvloop is a separate deploy decision.
- Gate order has a test now: a failed Turnstile leaves the paid per-IP window untouched.
- CI on e918563: unit and serve-shipped (Linux, including the real-uvicorn tests) passed; gitleaks flagged a fake workspace
  id in a test (marked gitleaks:allow, plus a .gitleaksignore fingerprint for the history finding).

**Known and accepted, tracked:**
- Crafted removal sentences ('no risk factor was removed ' repeated) still cost about 1.1 s of pure-Python CPU per check call
  at 5,000 characters and about 4 s at the 9,600-character cap (removal_claims._survives and _clause_claims). On a worker
  thread this does not stall the event loop (measured lag 23-27 ms; pure Python hands the GIL back every 5 ms; inline it
  would stall for the whole 1.2-4.3 s), but it holds one limiters.db thread per call (an escalated ask makes up to three).
  Bounded by the per-IP window, the daily cap and max_concurrent_answers; a restructure of _survives belongs after I2.
- litellm 1.100.0 runs stream_chunk_builder on the event loop at the end of every async stream (about 5-16 ms measured on
  1.90.2); not changeable without patching litellm.
- uvicorn logs one ERROR line ('ASGI callable returned without completing response', no traceback) for each client dropped
  by the send timeout; accepted, no log filter (a filter would also hide real bugs).
- Two send timeouts in the same loop iteration arrive as an exception group and still log a traceback (rare).
- A pure-Python (GIL-holding) embedder makes the plan's 'loop-lag monitor logs nothing' criterion unreachable at 40
  concurrent streams; the real ONNX embedder releases the GIL (measured 16 ms, 0 warnings). The live I2 check reads the
  monitor.
- For I4: uvicorn's --timeout-graceful-shutdown cancels request tasks and runs lifespan shutdown without waiting for them,
  so a finalize ledger write could race driver.close(); the drain work must wait for in-flight streams.
- Nothing has run with the h11 implementation or with uvloop.
