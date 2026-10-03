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
