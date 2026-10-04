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
