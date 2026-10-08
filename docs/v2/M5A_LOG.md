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

## 2026-10-06 - I3 pepper, I4a state package, the state fixes, the anchor cap, the drain and the I4 wiring

**Where things stand.** I3 and I4a are committed (8be3790). Everything after them is an uncommitted working tree on `v2`
built by four workers (the state-package fixes, the anchor cap with true-bound estimates, the SIGTERM drain, the I3+I4
wiring), and the test, config, docs and CI items of this entry (J1). Nothing is deployed: the live image is the pre-M5 one,
and `semigraph-neo4j` has not been redeployed with its new setting. The J1 work made no network call and no paid call and
started no Neo4j server; M5a spend is still < $0.01 (the 2026-10-03 probe) and no staging machine exists. The last full-suite
run before J1 was 7,567 passed and 11 failed (the 11 are the tests J1 converted: see "J1" below).

**I3, the address hash (8be3790).** `guard.hash_request_ip` is the one helper (seven call sites): HMAC-SHA-256 under
`IP_HASH_PEPPER`, cut to 16 hex characters, IPv6 bucketed to its /64, IPv4-mapped IPv6 read as the IPv4, a zone id ignored,
hostile strings never raise. The pepper's version (`IP_HASH_VERSION`, 2 is the first) is stored on the row as `ip_hash_v`
and never mixed into the hash, so bumping it alone does not reset every window. A production boot with a missing or
under-32-byte pepper fails, and the refusal never carries a value (`hide_input_in_errors` on `Settings`).
`store.null_legacy_ip_hashes` and `scripts/null_legacy_ip_hashes.py` null the old unsalted hashes (`ip_hash = null`,
`ip_hash_v = 0`) in batches; the script counts and changes nothing without `--yes`, and the null is IRREVERSIBLE (owner
decision 12: run it only on the owner's go, after the pepper is staged and the image is live). A rollback to an image that
writes unsalted hashes makes new legacy rows: re-run the null. Procedure: RUNBOOK "IP-hash pepper cutover".

**I4a, the state package (8be3790).** `serve/state/`: the `StateBackend` protocol and shared core (`backend.py`), the
in-process backend (memory counters under one lock, a durable ledger row per ask in Neo4j), the neo4j backend (counters in
Neo4j in the row's transaction), `ledger.py` (all the Cypher), `maintenance.py`, and `serve/estimate.py`. Money is integer
micro-dollars. Contract tests run on both backends, including 200 concurrent reserves against a cap of 150 granting exactly
150 on a real Neo4j Community 2026.07.1 on a throwaway instance. The verifier's report is in the commit message (PASS WITH
CONDITIONS; full suite 7,317 passed twice; serve-shipped venv 4,056 passed; 8 mutants, 6 killed). The conditions it listed
(a stale kill-level refresh undoing an admin kill, the 1.1 s state bound missed against a real server, an estimate that was
not a true upper bound, settle retries sleeping inside a limiter slot, the spend-cap boundary untested, no caller for
`ensure_state_schema`) were fixed afterwards, before anything was wired: the next three paragraphs.

**The state fixes (working tree).** Measured on 2026-10-06 against a local Neo4j Community 2026.07.1 on Windows (neo4j
driver 6.2.0, a spot check on 6.3.0; 20 runs per cell; production settings: 1 s operation budget, 0.5 s configured
acquisition timeout). The "before" figures are the fix worker's; `serve/state/backend.py` records the same cases as 1.8-2.2 s
(a managed transaction on a dead port), about 14 s (a failed settle) and 1.99 s median / 2.01 s worst (a held lock).

| Case | Before | After |
|---|---|---|
| reserve against a closed or silent port (a managed transaction: two attempts) | 1.94-2.07 s | 1.00 s |
| reconcile (settle) against a dead database | 14 s (it slept 2 s three times holding a state-limiter slot) | 1.0 s, the first attempt only; a failed settle goes to a retry queue |
| operation blocked on a held lock | 2.0 s | 1.04 s median, worst 1.085 s: a margin of about 15 ms to 1.1 s, so a loaded server can exceed it |
| kill-level read (auto-commit, one attempt) | not measured | 0.47 s |

- **How.** `make_state_driver` derives one attempt's timeout from the budget (`state_attempt_timeout_s`: 0.47 s with the
  defaults), uses a transaction retry window of 0.2 s with a first retry after 0.05 s (the driver's default 1 s delay alone
  spent the budget), and sets `liveness_check_timeout=0`. `settle_queue.py` retries a failed settle on every maintenance tick
  (up to 10 attempts or 120 s, 500 entries); what never lands stays `reserved` and the next boot (or the neo4j backend's
  sweep) charges its estimate.
- **The Neo4j server setting.** A held lock is cut by the server at the transaction's 1 s timeout, but only when its
  transaction monitor next looks: every `db.transaction.monitor.check.interval`, default 2 s. `deploy/neo4j/fly.toml` now sets
  `NEO4J_db_transaction_monitor_check_interval = "100ms"` (single underscores: the setting name has no underscore). **It needs
  a restart, i.e. a redeploy of `semigraph-neo4j`, which has NOT been done.** Until then the live bound for a state
  operation blocked on a lock is about 2 s, not 1.1 s. Procedure, including the seed-hash check that stops the redeploy from
  reloading the dump: RUNBOOK "The Neo4j transaction-monitor setting". The CI `neo4j-state` job sets it on its service
  container.
- **A pooled connection whose server goes silent** (a frozen machine, a lost route; nothing closed): the driver reads with
  no deadline of ours, only the server's 120 s receive-timeout hint. Measured through a forwarder that stops moving bytes:
  without a liveness check the first operation on such a connection was still blocked after 20 s; with `liveness_check_timeout=0`
  each acquire proves the connection alive inside the attempt timeout and drops it (a read fails in 0.47 s, a managed
  transaction in 1.0 s). **Not covered:** an operation already in flight when the server goes silent (a reserve blocked on a
  held lock, the forwarder frozen 0.3 s into it, was still blocked after 25 s; the wait was not run longer). It holds a
  state-limiter slot (4 on live) for up to the 120 s hint.
- **The kill-level race.** A refresh that read `off` just before an admin set `on` could write `off` back, reopening paid
  asks against a database that said `on` (HIGH in the verifier's list). `set_kill_level` now takes a generation number in the
  same critical section that decides whether it tightens; `refresh_kill_level` applies what it read only if no set began or
  finished meanwhile; database writes are serialised and a superseded set skips its write; a tightening whose write failed
  holds in memory and is written first by the next refresh (the thread retries).

**The anchor cap and the true-bound estimates.** One 500-character question can name all 26 detectable companies, and every
per-company block of the context scales with the anchor count, so the prompt, and what an ask could cost, had no ceiling.
`retriever.MAX_ANCHORS = 4`, twice the observed maximum: no benchmark, example or agent-benchmark question names more than 2.
`serve/estimate.py` reads the constant from the retriever, so the two cannot drift.
- The first 4 companies in **detection order** are kept: the order of `canonical_entities.json` (chip designers and
  foundries, Samsung, the hyperscalers, then the companies that file nothing), not the order the question names them, and
  deterministic whatever alias matched.
- The writer is told. `hybrid_retrieve` adds `anchors_dropped` (the key exists only then) and a `Note for this question: it
  names more companies than this answer covers; not covered: <names>.` at the end of the temporal block, where the answer
  prompt already tells the writer to state a note plainly. It carries no citation id.
- Cost against anchors (live models, 2.5 characters per token): one company's blocks at their caps are 39,686 characters
  measured (allowed at 42,000), about 16,800 tokens and $0.069 per anchor. A hybrid prompt is 129,485 tokens at 4 anchors and
  196,685 at 8; the 200,000-token tier the estimate assumes is crossed after 8 anchors for a hybrid ask and 7 for a
  workspace ask, so the cap of 4 keeps every plain ask under it. The hybrid estimate moved from $0.52 to $0.580089 and the
  break-even from 6.36 to 6.32 cents.
- Which daily cap binds first (`tests/test_serve_estimate.py`): a settled ask counts at its actual cost and only a lease in
  flight at its estimate, so the **150-ask count binds first while the average ask costs less than 6.32 cents, and the $10
  cap binds above it**. At the recorded mean (1.5 cents) all 150 are granted with $2.19 spent; at the dearest straight-to-Sonnet
  ask (6.06 cents) still all 150; at the dearest recorded ask (6.6 cents) the $10 cap binds at the 145th. Agent asks are
  estimated at $1.565661 (120 granted at the dearest recorded agent cost).
- **Not capped:** the agent. Its tools resolve companies themselves with no cap on how many the merged context carries, so
  `MAX_ANCHORS` does not bound it: the estimate prices all 13 SEC filers, about $1.57 and a prompt past the 200,000-token
  tier; a cap in the agent's merge would bring it to about $0.83 (`estimate.agent_company_blocks` is the one place).

**The drain.** Stock uvicorn and sse-starlette on SIGTERM (observed: uvicorn 0.52.4, sse-starlette 3.4.11; Python 3.13 on
Windows, 3.12 on Linux): uvicorn closes its listener at once and waits for connections with no timeout by default, and
sse-starlette replaces `Server.handle_exit` so that every `EventSourceResponse` is cancelled at once (after
`shutdown_grace_period`, default 0). So every paid stream was cut and a "draining, reads still served" route gate could not
exist. `serve/drain.py` implements option (b) of `M5A_BUILD_PLAN.md` item D, the plan's recommendation (the owner's answer to
item D is not recorded in this log or in `M5_DECISIONS.md`): `python -m semigraph.serve.drain` (the image CMD) runs a
`DrainingServer` whose `handle_exit` only begins the drain and never calls the parent, so the listener stays open and
nothing is cut. It ends on a 100 ms tick when nothing is counted active or at `DRAIN_TIMEOUT_S` (240 s), then ends the
streams, ledgers them and lets uvicorn shut down; a second signal exits without waiting. The budget fits Fly's 300 s: after
a 240 s drain 50 s remain for shutdown. Exit code 0 when drained empty, 1 when streams were cut or forced.
- Platform facts: Fly's `kill_timeout` maximum is 300 s and the docs conflict on the default signal (research note
  `docs/v2/research/m5-facts-fly-2026-10-03.md`: `flyctl machine stop` defaults to SIGINT), so `fly.toml` sets
  `kill_signal = "SIGTERM"` and `kill_timeout = 300`; the drain treats SIGINT and SIGTERM alike.
- **Unverified:** how Fly's proxy routes requests that arrive during a drain (the rolling-deploy drill measures it), and
  whether a second signal can be delivered to a machine that is already stopping. Nothing has run on Fly.
- `scripts/ops.ps1` was not changed: `stop` still runs `kill_switch on` and `flyctl scale count 0`.

**The wiring (I4).** `routes.ask` gate order: validate (and hash the address) -> draining (a workspace ask is refused here,
before any window) -> the free window -> a workspace ask's token (under the cache read budget and the 1 s bound) or the answer
cache for a public ask (a hit is logged and served; Neo4j slow or down is 503, never a paid call) -> `state.kill_level` ->
Turnstile -> the short per-address paid window -> `DRAIN.try_enter()` and `state.reserve` (a lease with the ask type's
estimate) -> the event stream. `PaidStream` takes the lease, calls `mark_started` on the first event, reconciles it BEFORE
the terminal event and gives the drain count back last in `finalize`. Refusal texts (`routes.py`):
- 429 `MSG_BUDGET` (daily count or daily spend): "The daily budget of live questions is used up — try an example, or come
  back tomorrow."
- 429 `MSG_IP_BUDGET` (20 per address per day): "You have used today's live questions for your address — the example
  questions still work, or come back tomorrow."
- 429 `MSG_BUSY` (in-flight cap, now a pre-stream refusal with a JSON `detail` and no event stream): "The service is busy
  answering other questions — try again in a moment."
- 429 `MSG_READ_RATE` (cache read budget, 10 reads/s for the process): "Too many requests from your address — please slow down."
- 503 `MSG_PAUSED` (kill level `on`; kill level or cache unreadable; reserve unavailable): "Live questions are paused right
  now — the example questions still work."
- 503 `MSG_RETRIEVAL_ONLY` (kill level `retrieval_only`): "Live questions are limited to cached answers right now — the
  example questions still work."
- 503 `MSG_DRAINING` with `Retry-After: 30`: "The service is restarting — please try again in a minute."
Also: `/api/admin/policy` takes `on`, `retrieval_only`, `off` (or a bool); `/api/admin/state` (admin token) returns the
snapshot, the limiter counts, the drain and the maintenance thread; `/api/stats` `paused` is now true for any level but
`off`, and it gains `spend_today_usd`, `limits.max_spend_usd_per_day` and `limits.per_ip_per_day`; uploads are refused unless the kill level is `off`. `Settings`
gains the I4 fields and production validators that refuse to boot without `TURNSTILE_REQUIRED=true`, a Turnstile secret,
`CLIENT_IP_HEADER=fly-client-ip` and a pepper, or with a cap raised past 150 asks, $10, 20 per address or 4 in flight (or a
cap of 0). The lifespan computes the estimates, rebuilds the counters from the ledger (retrying for 90 s), reseeds the paid
windows, starts the maintenance thread (which reads the kill level once before serving) and, at shutdown, waits for the
drain before it stops the thread and closes the drivers. `Dockerfile` CMD is the drain entry point; `fly.toml` has
`kill_signal`, `kill_timeout` and `STATE_BACKEND = "inprocess"`. Procedures: RUNBOOK chapter "M5a".

**J1 (this entry's own work).** The 11 failing tests were stale against the wiring; none needed a product-code change. The seven
in `tests/test_serve_guard_pepper.py`: four built a production `Settings` with only a pepper, which the production
validators now refuse for another reason (no Turnstile, no `fly-client-ip`); they now build on a valid production base, and a
new test proves that base boots and that each refusal in the file names only its own setting (pydantic stops at the first
failing validator, so the base-boots control is what makes the pepper the only reason a pepper case fails). Three built a
paid or cached ask without `state`, a lease or `build_route_app`; they now use `serve_state_fakes` (the paid-row pair runs a
`PaidStream` on a lease of the real in-process backend over an in-memory ledger). The four in
`tests/test_serve_ask_workspace_seam.py` use the new gate order, the backend's kill level and denials, and `fakes.settled`
for paid rows; none lost an assertion: the token-before-kill-level test now runs at `on` and `retrieval_only` and asserts that
a bad token never makes the kill level be read, and the daily-ceiling test runs for both `DAILY_COUNT` and `DAILY_SPEND`.
Added: the new settings in `.env.example` and `STATE_BACKEND` in `fly.toml`, the `drain.py` docstring rewritten to what the
wiring does, the RUNBOOK chapter, `tests/test_state_*.py` in the CI `serve-shipped` selection and a `neo4j-state` job.
Results after J1 (2026-10-06, this working tree): `uv run pytest -q` 7,612 passed, 294 skipped, 2 xfailed, 0 failed (the
skips were not itemised); the serve-shipped selection of the CI job (with
`tests/test_state_*.py`, plus `tests/test_retrieval_*.py` and `tests/test_static_ui.py`) in the shipped-dependency venv:
5,002 passed, 88 skipped, 0 failed. The opt-in Neo4j suites were not run by J1 (no server allowed).

**Known gaps and findings.**
- A refusal by `state.reserve` (daily count or spend, per-address daily, in flight) comes after Turnstile and the short
  paid window, so it uses one Turnstile verification and one window slot; before M5 the daily ceiling refused before
  Turnstile. The order is the plan's; the cost is one outbound check per refused ask once the day's budget is spent.
- An ask abandoned (client left, stream cut at the drain timeout) is charged its whole estimate against the $10 and the count.
- `TURNSTILE_REQUIRED` is in neither `fly.toml [env]` nor `push_fly_secrets.FLY_KEYS`; the new image boots only if it is
  already a Fly secret. `scripts/check_env_fly.py` of the plan was never written (the RUNBOOK has a one-liner instead).
- The held-lock margin to 1.1 s is about 15 ms; an in-flight operation on a silent server is bounded only by the 120 s hint.
- Estimates assume 2.5 characters per token (an assumption, not a bound), count no provider-level retry, and assume the
  200,000-token price tier is not crossed (unverified for Sonnet 5); the agent estimate is past that tier.
- I1 (the embedder fix) is still held (owner decision 14). I5 (S7, S2, the fresh-machine probe) and I6 are not started.
- The CI `neo4j-state` job could not be run locally; the first run on GitHub will show whether the service container, its
  health check and the 100 ms setting behave as written.

## 2026-10-08 - The panel's fixes, the verifier's conditions, and what stays open

**Where things stand.** Still an uncommitted working tree on `v2` (HEAD bfd7b71), now with the fixes for the three-reviewer
panel on the 2026-10-06 wiring (W1 the state, W2 the config, drain and ops, W3 the page, the regex test and the merge cap),
the Opus verifier's report on them (PASS WITH CONDITIONS), and the fixes for its conditions (W1b, this entry). Nothing is
deployed, nothing was committed, and no call to Fly or to a model provider was made. W1 started a throwaway local Neo4j on
port 7898 for its contract run and stopped it (the verifier confirmed ports 7898, 7698, 7474 and 7687 closed); W1b started
none. M5a spend is still < $0.01.

**Panel findings fixed**
- **H1, a kill set during an outage was lost.** `set_kill_level` ranked the new level against the effective level, which
  reads `on` for a stale or unread cache, so a set of `on` looked like "already on", nothing was queued, and the refresh after
  the recovery read the stored `off` back and reopened paid asks. It now ranks against the cached level as last read or set
  (see W1b item 2 for where that stops being enough).
- **The wait for a state slot was unbounded.** `stream_runtime.slot_call` takes the limiter token itself under a deadline of
  `state_op_timeout_s` and runs the work on a private one-token limiter, so only the WAIT is bounded (a granted call is never
  abandoned); a timeout is `NoStateSlot` (a `StateUnavailable`) and the call never ran. The settle has no bound by design (a
  reconcile that gave up would leave its lease registered and renewed for ever). `kill_level` and `mark_started` are called
  directly, memory only. The admin routes got a limiter of their own (one token).
- **A slow stream was reclaimed by the sweep.** `PaidStream.events()` now calls `mark_started` at its top, not at the first
  event, so a lease whose twin is slow to its first event is renewed, keeps its in-flight slot and settles `done` once.
- **The production validators** (`config.py`, `guard.py`): `ENVIRONMENT` and `CLIENT_IP_HEADER` are stripped and lower-cased
  once; `FLY_APP_NAME` is tied to the environment (an unknown app name refuses to boot, not only a mismatch); zero and raised
  values are refused for the three rate limits; `CACHE_READ_BUDGET_PER_S` is capped at 10 and `KILL_SWITCH_STALE_S` at 30;
  `WORKSPACE_CREATE_PER_DAY`, `UPLOADS_PER_HOUR` and `RATE_LIMIT_WINDOW_SECONDS` are pinned; a set admin token must be 32+
  characters; refusals name settings only. `scripts/check_env_fly.py` exists now (below).
- **An upload could start during the drain** (`DrainCountedSlot.acquire` now uses `try_enter` and answers 503).
- **The shutdown budget overran Fly's `kill_timeout`.** 240 (drain) + 15 (post-timeout window) + 35 (lifespan shutdown) + 10
  = 300 s; a test derives the lifespan worst case from the code's own constants. `DRAIN_TIMEOUT_CEILING_S` is 254.
- **LOWs:** logs carry the class and the server code, never a message; a failed boot unwinds (`abort_boot`); the page no
  longer sticks on "generating…" after a cut stream; `MSG_STATE_UNAVAILABLE` is the text for an unreachable store (five
  paths) and `MSG_PAUSED` is the kill switch's alone; the RUNBOOK `-C` commands, `ops.ps1 stop` (waits for the machines to
  be gone), the second-signal text, `TURNSTILE_REQUIRED` in `fly.toml [env]`, the dead `drain_timeout_s`, the CI skip guard;
  the agent merge holds at most 5 temporal pairs per company, which makes that part of the estimate a bound the code enforces.
- **The red CI regex test, honestly.** `test_a_nest_deeper_than_the_cap_before_a_flood_costs_under_the_budget` measured a
  20.0 ms wall-clock budget and failed on GitHub at 20.7 to 23 ms; it ran at 11 to 12 ms p95 on the dev desktop and could not
  be reproduced locally. The budget is now `loop_lag_warn_ms / 2` = 50 ms: the budget protects the event loop, the answer cap
  gives a 20k-character input 2x headroom, 50 ms is 2x the slowest CI p95 seen, and the old quadratic implementation took 886
  ms on that input. A new test runs the old implementation verbatim and asserts it is still over the budget. **This loosened
  a wall-clock threshold; it did not change the code, and the protection is the scaling tests and the legacy test.** Whether
  CI is green is not confirmed: nothing has been pushed.

**The verifier's evidence** (before W1b's edits): each of 17 fixes was reverted on its own in a scratch copy and its tests
were run; all 17 reverts were caught. Dev suite `uv run pytest -q -p no:cacheprovider`: 7,782 passed, 312 skipped, 2 xfailed,
0 failed (446 s). Shipped-dependency venv (`C:\temp\sg_sv`, the CI serve-shipped list): 5,142 passed, 106 skipped, 0 failed
(335 s). UI (`node --test tests/ui/*.test.mjs`): 188 passed. `scripts/check_env_fly.py` against the real `.env.fly` and
`fly.toml`: 22 PASS, 1 FAIL (the pepper). **These counts predate W1b's edits and the full suites were not re-run after them**:
W1b ran the targeted files below.

**W1b, the verifier's conditions.**
1. **An emergency kill behind a stuck admin call (a regression against bfd7b71).** The admin limiter has one token, and an
   admin call stuck on a half-open connection holds it for up to ~120 s, during which a kill `on` was refused with 503 and not
   applied. The route now calls `state.hold_kill_level(level)` first, on the loop: memory only, it takes the memory lock and
   never the lock that serialises the database writes. A level that holds is in force at once and its write is queued for the
   maintenance thread; only the write (and a relaxation) waits for the admin token. If the token does not free in
   `state_op_timeout_s`, or the write fails, a held level answers 200 `stored: false` and a level that is not held answers
   503 with nothing changed. A relaxation still needs a successful write. The failed write raises `KillNotStored` (a
   `StateUnavailable`) whose `held` attribute is the backend's own answer, so the route no longer infers it from the
   effective level. Tests: `tests/test_serve_state_wiring.py::test_a_kill_behind_a_stuck_admin_call_is_in_force_at_once_and_is_stored_when_the_token_frees`
   (the POST runs on a thread while the test sees the level go `on` inside the wait, a direct `reserve` is `KILL`, and the
   next refresh stores it) and `::test_a_relaxation_behind_a_stuck_admin_call_is_503_and_changes_and_queues_nothing`.
2. **Kill-level corners.** The rule is now one predicate (`StateCore._holds_locked`): a level holds when it is not `off` and
   at least as tight as the level the gates apply now (the cached level, or `on` when stale or unread; the `KILL_SWITCH` env
   override is a separate layer). (a) A set EQUAL to the cached level now queues its write when the database write fails (it
   returned 200 `stored: false` with nothing queued, so the promised retry did not exist); the contract test makes the
   database differ during the outage, because otherwise the refresh reads the same level back and the test passes without the
   fix. (b) With `KILL_SWITCH` on, a tightening that was applied and queued was reported as 503 "not applied" because the
   effective level reads `on`; the answer now comes from the backend. (c) `retrieval_only` set on a cache that reads `on` only
   by age, or was never read, used to become the cache, lowering the level the gates apply from `on` and queuing
   `retrieval_only` over a stored `on`; it is now a relaxation (503 during an outage, nothing queued). `off` is never held or
   queued, not even equal to a cache that reads `off`: a queued `off` would be written over a level another machine stored,
   with nobody having confirmed it. For the same reason an EQUAL `retrieval_only` on a cache that is stale is not queued
   either (the gates apply `on`, so it relaxes): 503. Both go beyond the literal "equal included" wording on purpose. A property
   test covers cache {fresh, stale, unread} x cached level x requested level. **Behaviour change to know about:** the earlier
   H1 tests expected `retrieval_only` to hold on a stale or unread cache; they now expect it not to.
3. **A regression in the slot wait would have hung CI** (a reverted run sat for 240 s). The slot-wait tests of
   `test_serve_stream_runtime.py` run under a 5 s deadline of their own (`run_bounded`); the HTTP-level ones run the request
   on a thread with a deadline (`bounded`: `anyio.fail_after` cannot wrap a synchronous `TestClient`) and give the held slots
   back before they fail; the `_admit` test has `anyio.fail_after(5)`. Red check in a scratch copy with the wait made
   unbounded: both groups fail in seconds instead of hanging.
4. **The page (`index.html`).** An exception inside `handle()` re-enabled Ask but left the status on "generating…"; so did a
   response with no body, and a throw in `finish()` left "done" over an answer whose checks were never shown. Every exit of
   `ask()` now leaves a true status: a page failure says "the page could not show this answer completely…", the original
   error still surfaces, a `done` or `error` the server sent and the page showed is never replaced. The page does NOT close
   the connection on such a failure (a first draft did, and it was taken out: a disconnect settles the ask as `abandoned` at
   its whole estimate, $0.58 to $0.83 against the daily cap, where an answer that is let finish settles `done` at its cost;
   that is the owner's trade-off, not a side effect of a status fix). Six node tests (`tests/ui/stream_cut.test.mjs`); four
   fail on the previous page (measured in a scratch copy), the other two are controls that already passed (the error still
   surfaces; a server error is not replaced). Node suite: 194 passed.
5. **Stale text fixed:** RUNBOOK (the refusal table, "Failing closed", the troubleshooting entries, the kill-level section,
   the BOM lines) and the `serve/state/__init__.py` docstring. The corrections below cover this log.
6. **`push_fly_secrets.parse_env` reads `utf-8-sig`:** a file that starts with a BOM no longer loses its first key to a name
   that begins with `\ufeff` (it was reported absent and never pushed). `tests/test_push_fly_secrets.py`: three of five fail
   on the old code. Its first fixture put a comment on line 1 and so proved nothing; the key is on line 1 now.

**W1b evidence.** Red-then-green where a test could be written first (items 3, 4 and 6); items 1 and 2 were written against
the new design and then checked by mutation in a scratch copy: eight mutants (no hold before the token, effective-level
inference only, `KillNotStored.held` ignored, `off` may be held, ranking against the raw cached level, equal not queued, the
hold waits for the writer lock, a refused hold bumps the generation) were all caught. Targeted runs on the final tree
(`uv run pytest -q -p no:cacheprovider`): the files W1b owns plus `test_serve_api`, `test_serve_drain` and
`test_state_inprocess` 587 passed, 143 skipped (the skips are the opt-in Neo4j kinds); `tests/test_serve_*.py`,
`tests/test_state_*.py`, `test_check_env_fly.py` and `test_push_fly_secrets.py` 1,957 passed, 148 skipped, 0 failed (188 s);
`node --test tests/ui/*.test.mjs` 194 passed; `tests/test_static_ui.py` and `tests/test_serve_agent_ui.py` 40 passed.

**What W1b did NOT do, and the limits.**
- The Neo4j kinds (`inprocess-db`, `neo4j`) of the new and changed contract tests never ran: no throwaway server was allowed,
  so those parameters skip. Only the in-memory kind ran. W1's earlier run against a throwaway Neo4j (165 passed, 0 skipped)
  predates these tests.
- `FakeStateBackend` has no `hold_kill_level`; the route falls back to judging a double by its effective level, and the
  route's real-backend tests use `make_backend`. `scripts/kill_switch.py` prints the same banner for `stored: false` as for a
  stored flip; the operator confirms with `kill_switch get` (the RUNBOOK says so).
- A queued tightening is an unconditional write. If this machine holds `retrieval_only` against a cache that read `off` while
  another machine stored `on`, the flush overwrites that `on` with `retrieval_only` when the database returns. Both refuse paid
  asks, so it costs nothing today; it would need a conditional write (read first, write only if tighter).
- The admin limiter has one token for the flip and for `GET /api/admin/state`; a held tightening no longer waits for it, a
  relaxation and the report still do (503 after `state_op_timeout_s`).
- **A residual the hold introduces.** `hold_kill_level` runs outside the admin token and bumps the generation, so a held
  tightening can supersede a set that holds the token and is waiting on the writer lock (for example behind a maintenance
  flush stuck on a silent connection). That set then skips its write and returns, and its route answers 200 `stored: true`
  for a level it never wrote. The error is on the safe side (the machine is tighter than reported, and the superseding
  level is queued), but the `stored` flag can be wrong in that window. Before this wave the case could not arise from the
  route: the single token serialised every set. Not fixed.
- Full suites were not re-run (see above).

**Still OPEN (these gate live traffic or a rollback)**
- **H2 and the cost accounting.** One address can still pause the day (the verifier's H2/M3 policy), a paid ask's cost is not
  accounted across its retry attempts (V2 M1), and the strong stream still runs with `num_retries=2` (so the estimate is not a
  strict bound; `CHARS_PER_TOKEN = 2.5` is an assumption, not a bound). These go to the next wave, per council 4
  (`docs/v2/research/m5-councils/council4/`).
- **The `neo4j-state` CI job has never run green on GitHub** with the new rule that any `SKIPPED` line fails it. Against a
  database that was already used locally it fails on 6 skips (311 leftover legacy ledger rows make the legacy-row tests skip);
  a fresh service container has none, which is a reading, not a measurement. Until it is green once, a rollback to
  `STATE_BACKEND=neo4j` rests on W1's local run.
- **The pepper is not in `.env.fly`** (`IP_HASH_PEPPER` and `IP_HASH_VERSION` are absent), so the next deploy refuses to boot
  unless Fly already holds one; `check_env_fly` cannot see Fly's own secrets. Generate 32+ bytes, put both in `.env.fly`, push
  them together, re-run the check.
- **`ESCALATION_MODEL` is set both in `.env.fly` and in `fly.toml [env]`.** Which one a Fly machine reads is not confirmed;
  the owner decides which value is live.
- Smaller: the agent planner is shown pre-merge pair counts (`agent/tools.py`, `_risk_changes`), the writer sees the cap note;
  an unknown `FLY_APP_NAME` refuses to boot, so I5 must register any helper app that builds `Settings` (mockllm); an empty
  `ADMIN_TOKEN` is allowed in production (the owner's call); shutdown leaves the two driver closes and the monitor's lease
  release unbounded (about 2.5 s of slack).

**Corrections to the entries above (this log is append-only; the text above stays as written).**
- 2026-10-06 "The wiring (I4)": the 503 `MSG_PAUSED` line also listed "kill level or cache unreadable; reserve unavailable".
  Only part of that moved. An unreadable, unread or stale KILL LEVEL is still `MSG_PAUSED` (`_kill_level` answers `on` on any
  failure). The cache read, the workspace token, the cached row and the reserve being unavailable are now
  `MSG_STATE_UNAVAILABLE` ("Live questions are temporarily unavailable — please try again in a few minutes."), which promises
  no examples. Because the cache read comes before the kill gate, a visitor sees "unavailable", not "paused", while Neo4j is down.
- 2026-10-06 "The anchor cap": the agent estimate of about $1.57 and the "not capped" bullet are stale. The agent's companies
  are capped at the anchor cap and its temporal pairs at 5 per company; the estimate is about $0.83 (827,661 micro-dollars),
  with break-evens of 6.32 and 6.16 cents (`tests/test_serve_estimate.py`).
- 2026-10-06 "The drain": "after a 240 s drain 50 s remain" is stale. The post-timeout window is 15 s (240 + 15 + 35 for
  the lifespan shutdown + 10 = 300); `DRAIN_TIMEOUT_CEILING_S` is 254, not 289. "`scripts/ops.ps1` was not changed" is stale: `stop` waits for
  the machines to be gone.
- 2026-10-06 "Known gaps": `TURNSTILE_REQUIRED` is now in `fly.toml [env]`, and `scripts/check_env_fly.py` was written (22
  PASS, 1 FAIL on the real files today).

## 2026-10-08 (wave 2) - The paid-call meter, the per-address spend share, the fp32 embedder path and the staging harness; the verifier's FAIL and the blockers fixed

**Where things stand.** One uncommitted working tree on `v2` (HEAD 1e52772). Nothing is deployed and nothing was committed. No
worker called Fly or a model provider. The verifier started a throwaway Neo4j on port 7898 for the state tests and stopped it
(ports 7898, 7698, 7474 and 7687 closed, no java process left); the repository was byte-identical before and after its run
(same `git status`, same sha256 for all 132 modified and untracked files). M5a spend is still < $0.01.

**What was built.**
1. **The paid-call meter** (`serve/meter.py`, `PaidMeter`; pure and thread-safe, no litellm and no state import). Every provider
   call of an ask is recorded when it starts (role draft / strong / planner, model, prompt characters, maximum output tokens,
   attempts) and completed with the provider's reported usage. `PaidStream` creates one meter per ask and hands it to a twin
   that declares a keyword named exactly `meter` (all four real twins do; a guard test pins the name, because a rename would
   silently un-meter a twin). The decisions taken (council 4, `docs/v2/research/m5-councils/council4/`):
   - an abandoned ask is charged `min(estimate, metered charge)`, but never below what the provider reported (the B1 rule
     below); it is charged 0 when a metered ask started no paid call; a twin that takes no meter, or a stream that never
     opened its twin, still settles at the estimate;
   - it fails closed to the estimate: a bad request, an unreadable price, an inconsistent record, or a meter that cannot be read
     charges the full estimate (a meter that is merely empty is the 0 case, and the two are kept apart);
   - it settles after the twin has stopped: close the twin, the tracer, the meter, then the abandoned settle, then the drain
     count, the last two in nested `finally` clauses inside the shielded scope. A process killed during the close leaves the row
     reserved and the next boot charges the estimate;
   - a call that starts after the ask was settled is still counted, flagged late and logged at ERROR;
   - the strong stream (the escalation, a question routed straight to the strong model, and the sole answer model of the
     rollback configuration) makes NO provider retries (`num_retries=0`, as the draft never did): the stream's own attempts are
     the retries and each is one metered call. A LiteLLM retry would be a billed call nobody could bound. The planner is
     metered inside the stop guard. `done` and `error` events keep the sync writer's `usage` and `cost_usd`; the meter is a
     separate record.
2. **The per-address spend share** (`PAID_SPEND_SHARE_PER_IP_USD`, default 1.25, production 0 < x <= 1.25, 0 = off). One address
   cannot spend more than the share of the day: `ip_spend + estimate <= share` is decided atomically after `IP_DAILY` and before
   `INFLIGHT` (in process under one lock; in Neo4j in the same WHERE of the reserve statement, under the day counter and then
   the address node). The settle adjusts the address spend; the boot rebuild restores it from the ledger rows. A denial is
   `ip_spend` (429, `MSG_IP_SPEND`: settled spend plus the estimate passes the share) or `ip_spend_inflight` (429, `MSG_BUSY`:
   only the address's running asks push it over). Eight addresses are needed to pause a $10 day. On the first boot after a
   deploy the address spend is rebuilt from the ledger rows (no manual step); an image from before this ignores the field and
   an address node written before it reads as zero, so a rollback is safe.
3. **The fp32 embedder build path** (`scripts/predequantize_embedder.py`, Dockerfile `EMBEDDER_VARIANT=q8|fp32`). The default is
   unchanged (q8; `ONNX_MODEL_PATH` resolves to the same string; `fly.toml` untouched). The fp32 gates: every new weight equals
   ONNX Runtime's own dequantization (196 of 196 nodes bit-exact, maximum difference 0.0), the graph around the replaced nodes
   is unchanged with no 8-bit node left, and the 53-question cosine gate against the q8 model passes. Parity on the local pair
   (`artifacts/embedder_parity_fp32.json`): 60 of 60 top-1, top-8 overlap 1.0, all four gates. `/healthz` now carries
   `embedder_variant` and `embedder_fidelity` (a few fidelity numbers and a 16-character prefix of the source model's hash; no
   version, path or secret). This gives decision 14 a path that passes its pre-registered gate; nothing is switched.
4. **The staging harness and the staging-only switches.** `deploy/staging/` (five apps, `windows.json` with the quotes),
   `scripts/staging.py` (quote, create, seed, deploy, preflight, reset-ledger, snapshot, destroy; spending commands need
   `--approve-quote` equal to the printed quote), `tools/mockllm`, `tools/s7` (the S7 replay and its report), `tools/probe`,
   `tools/loadtest` and `scripts/loadtest_report.py` (VOID / FAIL / INCOMPLETE / PASS computed from the raw files). The switches:
   `TURNSTILE_STUB`, `OPENAI_API_BASE` (every `openai/` call carries it through `llm_shape.provider_kwargs`), `ORIGIN_AUTH_SECRET`
   (`OriginAuth` is wired in `create_app` only when it is set; the secret is wrapped so no printout shows it), and a staging
   validator set that is an allowlist (the staging database host, no provider key, a `mock-` key, the mock on a `.internal`
   host, `openai/mock-*` models, a secret of 32 bytes or more, no freshness monitor, a vouched client-address header). Production
   refuses every switch, whatever its value, naming the setting and never the value.

**The verifier's verdict: FAIL as the tree stood** (Opus, against this tree). Dev suite `uv run pytest -q --ignore=tests/test_loadtest_local_smoke.py`:
4 failed, 9031 passed, 411 skipped, 2 xfailed (506 s). The serve-shipped selection, run in a clean venv of
`deploy/requirements-serve.txt` (LiteLLM 1.100.0): 2 failed, 5642 passed, 200 skipped. UI `node --test`: 194 of 194. State tests
against its throwaway Neo4j: 561 passed, 6 skipped (the six are in `test_null_legacy_ip_hashes_neo4j.py`: the instance held 311
old ledger rows; a fresh CI service has none, which is a reading, not a measurement). Per area: money safety FAIL, per-address
atomicity PASS, boot rebuild PASS, finalize order and shield PASS, rollback compatibility PASS (read, not run), staging switches
PASS, secrets PASS, embedder PASS WITH CONDITIONS, harness PASS WITH CONDITIONS, test leaks FAIL, dev suite FAIL, serve-shipped
FAIL. Of the 17 mutants it ran, the seven it reports for the money path were killed (strong retries restored, settle before the
close, an unused lease settled at the estimate, an empty meter always 0, a fault not fail-closed, a late call not flagged, `done`
ignoring the meter); the removal of the address spend from the Neo4j settle survived the race tests alone and was caught by the
contract tests.

**The four blockers, and how this pass closed them.**
- **B1 (money).** A finished ask whose reported cost was above its estimate was recorded at the estimate (probe: reported 70,000
  micro-dollars, estimate 60,000, ledger 60,000; HEAD and an unmetered twin record 70,000). That broke council 4's test (d), "at
  least the provider-reported cost", and under-recorded the day and per-address counters. It was reachable through the Sonnet
  ratio below. Fixed by the code workers of this pass; as read in the tree when this entry was written,
  `PaidMeter.charge_micro` returns `max(reported, min(estimate, total))` (with a fault, `max(estimate, reported)`): the charge
  is never below the sum of what the provider reported, the estimate caps only what is a bound, and a report above the estimate
  is logged (`meter_over_estimate`). Pinned in `tests/test_serve_meter.py` (the 70,000-against-60,000 probe, at and around the
  report) and in `tests/test_serve_stream_runtime.py` (a done and an abandoned ask both recorded at 70,000). The old test that
  pinned the cap was replaced; the commit has the final names.
- **B2a (money).** When reading the meter raised, `_abandoned_cost_micro` settled at 0 while the log said the estimate stays
  charged. Fixed by the same workers: "unreadable" is kept apart from "empty" (the meter's reading has a state for each), and
  an unreadable meter charges the estimate; `tests/test_serve_stream_runtime.py` has the unreadable-meter cases for the done,
  error and abandoned settles.
- **B3 (two tests leaked process state).** `LiveMock` builds `uvicorn.Config(log_level="error")`, which sets `uvicorn.error` to
  ERROR (and installs handlers on `uvicorn` and `uvicorn.access`) for the whole process, so `test_serve_drain` failed after the
  mock tests; and the `live` fixture left a cached `get_settings()` holding the first mock's `OPENAI_API_BASE`, so
  `test_the_real_litellm_planner_gets_a_tool_call_from_the_mock` failed on its own. `tests/test_tools_mockllm.py` now has an
  autouse guard that restores the four uvicorn loggers (level, handlers, propagation, disabled flag, filters) and empties the
  settings cache before and after each test; `tests/test_latency_smoke.py` already had its own copy. Red then green:
  `pytest tests/test_tools_mockllm.py tests/test_serve_drain.py` 2 failed, 120 passed -> 125 passed; `tests/test_tools_mockllm.py`
  alone 1 failed -> 52 passed on each of two runs. The one place that would serve every user of the mock is `LiveMock` itself
  (`tests/mockllm_fixtures.py`); the two copies are tidied there when it is next edited.
- **B4 (four red tests).** All four came from the new staging validators, and each was the test's assumption that was out of
  date, so each was made correct and not only green: the pepper test and the "caps may be raised or zeroed" test built a bare
  `environment="staging"` and now use `"development"` (staging has its own allowlist, tested in
  `tests/test_serve_config_staging.py`); `test_staging_tomls.py` reads the toml back with the two secrets a window generates
  (a mock key, an origin secret) and first proves that the toml alone is refused naming both settings; the S7 test expected the
  tools app to be an unknown app, but it is registered as staging now, so it proves that a `Settings` built from the tools
  machine's own environment is refused, and that the staging database app (left out of `FLY_APP_ENVIRONMENTS` on purpose) is
  refused by name.

**Also changed in this pass.**
- **`OPENAI_BASE_URL`** (verifier area 6, adjacent). LiteLLM reads it from the process environment BEFORE `OPENAI_API_BASE`
  (`litellm/main.py`), and live calls pass no `api_base`, so a production process holding it would have sent every `openai/`
  call (the cheap drafts) elsewhere with no validator noticing. It is a `Settings` field now (read from the environment, hidden
  from the repr, since a base URL may carry credentials), refused in production by name only, and in the pre-deploy check
  (`CHECKED_SETTINGS`). An exported-but-empty variable is not a base. **The same class is not covered:** LiteLLM also reads
  `ANTHROPIC_API_BASE` and `ANTHROPIC_BASE_URL` for the Sonnet calls (`litellm/main.py`, twice each). Nothing sets them; not
  refused yet.
- **Quote labels** (verifier area 9). The L5-only row listed the whole window's phases (control, L10, L20 included) while it
  prices 3.6 h: it now states its own hours (or an option's own `duration` text if the file has one). The four-worker option
  printed "3 x loadgen" while its arithmetic and quote already paid for five: its machines column now merges the extra machines.
  Both are pinned in `tests/test_staging_script.py`.
- **CI.** The serve-shipped selection gains `tests/test_llm_provider_kwargs.py` (34), `tests/test_agent_seam.py` (8) and
  `tests/test_embeddings_onnx_variant.py` (18 and 5 skipped for the developer-only `onnx`), each run in a clean venv of
  `deploy/requirements-serve.txt` before it was added. `test_state_core_share.py`, `test_serve_meter.py`,
  `test_serve_routes_share.py`, `test_serve_config_staging.py`, `test_serve_healthz_embedder.py` and `test_serve_origin_auth.py`
  were already matched by `test_state_*` and `test_serve_*`.
- Two stale docstrings (`asgi_middleware.py`: it IS wired in; `tools/mockllm/metrics.py`: the strong call runs with
  `num_retries=0`).
- Two wiring tests (`tests/test_serve_state_wiring.py`) followed the code and are not blockers: a lease whose twin was never
  called now settles at 0 (as `routes._abandon` does) and the bound of a started Sonnet call is priced at the per-model ratio
  (`estimate.tokens_for_chars`, so the test no longer hard-codes 1,000 tokens).

**Characters per token (an owner condition, measured).** `scripts/check_chars_per_token.py` (artifact
`artifacts/chars_per_token_check.json`) joins the recorded prompts to the billed prompt tokens. Sonnet's lowest ratio is 2.10
characters per token and 7 of 20 hybrid prompts fall below the 2.5 the estimate assumed (vector prompts are fine, lowest 2.83);
Luna holds at 3.02 or above over 20 rows. A started Sonnet call's bound therefore under-counts its input by up to 19% (about
$0.01 at the largest recorded prompt, 64k characters; about $0.05 at the estimate's ceiling), and a draft plus an empty strong
attempt plus a full strong attempt reaches about $0.66 against the $0.58 hybrid estimate. The 3.03 and 3.95 figures quoted in
`estimate.py` could not be reproduced with the template the runs used. Decision: a per-model ratio, 2.0 for Sonnet (under the
measured minimum). The estimates, the share arithmetic and their pins move with it: the table and the new numbers are in the
commit (`tests/test_serve_estimate.py`), not repeated here. Not measured: the planner's, the workspace's and the deployed
path's own prompts (no run saved them) and anything near the ceiling; `PaidMeter` logs `meter_ratio` at INFO for every
completed call so the first preview run measures them. Sonnet's tokenizer is not available offline, so its rows rest on the
baseline and the bake-off having used the same prompts.

**Unverified.** No Docker image was built, so the first real deploy is the first run of the new `RUN` step of the Dockerfile
(pin resolution on Linux, the builder's peak memory for the gate's 2.3 GB session, CRLF in the multi-line `RUN`). Linux RSS and
Fly timing of either embedder are unmeasured; the timing window holds both models in one process (about 1.1 GB plus 1.8 GB).
Every `flyctl` flag in `scripts/staging.py` is unverified against this org: `apps create --network`, `ips allocate-v4 --shared`,
`secrets import --stage`, `deploy --ha=false --vm-size --vm-memory --no-public-ips --env --build-arg`, `machine list/stop/start
--json`, `ssh console -C`, `proxy`, `config show`; `deploy --build-arg` takes an allowlist (`EMBEDDER_VARIANT=q8|fp32`,
`KEEP_UNPATCHED=0|1`). The S7 Cypher, `dbms.queryJmx` on 2026.07.1 Community and the state statements of the replay have never
run against a server (the opt-in `test_a_level_against_a_throwaway_neo4j` does that); `tools/s7/data/vectors.json` is not built.
The local Locust smoke was not reproduced by the verifier (gevent's DLLs fail to load from its long scratch path), so its PASS
rests on the report script alone. The rollback compatibility of the new ledger field was checked by reading the code, plus one
forward-compatibility test. The mock-CPU > 50% VOID rule in `scripts/loadtest_report.py` is tagged `[pre-registered]`; the
verifier found that it comes from the harness plan, not from council 5 (none of `M5_PLAN.md`, `M5_DECISIONS.md` and
`M5A_BUILD_PLAN.md` states a 50% threshold, and the harness plan is not in the repository), so the tag is to be corrected
there; doing so changes the count of pre-registered clauses and what `void_without_a_pre_registered_clause` means.

**Still OPEN.**
- **Owner:** the VOID / FAIL split of the harness verdict (and the clauses the harness added on top) needs sign-off before W4;
  decision 14 (the embedder) now has a gate-passing fp32 path but needs the owner's choice and a timing / RSS window first
  (switching means `ONNX_MODEL_PATH` in `fly.toml` and `fly.stg.toml`; an fp32 image built without `KEEP_UNPATCHED=1` has no
  8-bit file and will not boot with today's `fly.toml`); the staging quotes (`--approve-quote`, W0 $0.05, W1 $0.08, W3 $0.45
  or $0.25 for L5 only, W4 $1.25 or $1.60 with five generators); the go for the first deploy; the go for nulling the legacy
  IP hashes.
- **Engineering:** the fp32 Docker build has never run; Linux RSS and Fly timing are unmeasured; `KEEP_UNPATCHED` in the
  Dockerfile means "keep the 8-bit model and ship the timing script", which differs from the I1 plan's meaning (pass
  `--keep-unpatched` to the builder), so un-holding I1 collides with it; the live `Dockerfile` and `fly.toml` do not set
  `LITELLM_LOCAL_MODEL_COST_MAP`, so LiteLLM tries to fetch its price table when the live API starts; `ANTHROPIC_API_BASE` and
  `ANTHROPIC_BASE_URL` are not refused; on the Neo4j backend the boot rebuild is skipped while another machine holds leases, so
  the address counter then starts that day at 0 (fails open, bounded by the $10 day cap); a stream that never ran settles at
  the estimate in `PaidStream` but at 0 in `routes._abandon` (both safe); the `neo4j-state` CI job has still never run green
  on GitHub.

**Corrections to the entries above (append-only).** The 2026-10-08 entry's "Still OPEN" line saying the strong stream "still runs
with `num_retries=2` (so the estimate is not a strict bound)" is stale: it runs with none now, and the bound of a started call is
the open question of the ratio above. The same line's "a paid ask's cost is not accounted across its retry attempts" is closed
by the meter, and "one address can still pause the day" by the per-address share, for asks the meter covers.

## 2026-10-08 (wave 2b) - The share set to $1.40, the Anthropic base URLs refused, the mock restoring its own process state, the harness labels

**Estimates, and why they moved.** Each model sizes the prompt at its own characters per token (Claude Sonnet 5 at 2.0, every other
model at 2.5; "Characters per token" above), which raised every live estimate: hybrid $0.580089 -> **$0.709573**, vector $0.223389 ->
**$0.265873**, agent $0.827661 -> **$1.015669**, workspace $0.600236 -> **$0.734632** (`tests/test_serve_estimate.py`, pinned in
`tests/test_state_contract.py`). The ceiling prompts are 161,856 tokens on Sonnet and 129,485 on Luna for a hybrid ask, 167,998 for
a workspace ask and 235,012 for the agent's Sonnet call, which is past the 200,000-token tier the estimate was first written to
stay under. The 200,000-token concern was checked by the main session against Anthropic's pricing page (this pass did not fetch
it): Sonnet 5 has no long-context premium.

**The per-address share is $1.40** (`config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD`: the default, the production ceiling and the
validator are the one constant). It applies council 4's own two requirements to the estimates above, and the arithmetic is in the
comment beside the constant:
- (i) pausing the day must need at least eight addresses: `7 x share < $10`, so `share < 10/7 = $1.4286`;
- (ii) council test (b), 20 asks from one address with 3 escalations and 2 agent asks all admitted (the second agent ask arrives
  with 15 x $0.002 + 3 x $0.04 + $0.15 = $0.30 settled): `share >= 0.30 + 1.015669 = $1.3157`.
$1.25 (`$10 / 8`) fails (ii); $1.40 meets both. What it means for one address (a running ask counts at its estimate): a second
concurrent hybrid ask is refused with the busy text while the first is in flight (2 x 0.709573 > 1.40); an agent ask is admitted
while the address's settled spend is at most about $0.38, a hybrid ask while it is at most about $0.69; a vector ask fits beside a
hybrid or an agent ask, an agent or a workspace ask beside a hybrid one does not. Pinned on the in-process backend in
`tests/test_state_contract.py` (the same cases are written for the database-backed kinds, which run only with `RUN_NEO4J_TESTS=1`
against the throwaway server on 7898 and were not run here: no Neo4j call was made), in `tests/test_serve_config_production.py` ((i)
as `7 x 1.40 < 10`, the ceiling, the environment variable) and in the share-conversion tests (1.40 is stored as 1,400,000).
**Noted for the owner, not changed:** seven full shares are $9.80 and leave $0.20 of the day, which is less than the cheapest
estimate (the vector ask, $0.265873). So requirement (i) holds as written (seven addresses cannot reach $10) but the estimate-based
day cap refuses every live ask a little before that: seven addresses that each used the whole of their share stop live asks for the
day (cached answers still work). A share that kept room for a hybrid ask after seven full shares (`7 x share + 0.709573 <= 10`)
would be at most $1.3272, for a vector ask at most $1.3906, and (ii) needs at least $1.3157. To leave less than a vector ask's
estimate, seven addresses must average at least $1.3906 each, within about a cent of the share, which takes asks whose charges
are tuned to land there (an ask is admitted only while settled spend plus its estimate fits the share); whether the gap matters
is the owner's call. The B1 consequence stays: an address ends the day above its share by the excess of a provider
report over its estimate.

**`ANTHROPIC_API_BASE` and `ANTHROPIC_BASE_URL` are refused in production** (the same class as `OPENAI_BASE_URL`). LiteLLM 1.100.0
reads both from the process environment for the Anthropic route (`litellm/main.py`) and a live Sonnet call passes no `api_base`, so
a held value would have sent every escalation elsewhere. Both are `Settings` fields (read from the environment, hidden from the
repr), refused by name only and never by value, listed in `CHECKED_SETTINGS` (so the pre-deploy check reports them), and
documented in the RUNBOOK's refusal table and `.env.example`. An exported-but-empty variable is not a base; development may set
them (a local gateway). Effect on a developer shell that exports one of them (a proxy or gateway setup does): a test that builds a
production `Settings` without clearing the environment now fails locally; the tests of this pass clear every `Settings` field's
variable, and the others are listed in the report of this pass.

**`LiveMock` restores the process state itself.** `uvicorn.Config(log_level="error")` rewrites four loggers for the whole process;
`LiveMock` now snapshots them before it builds its `Config` (in `__enter__`, so building one changes nothing) and puts them back on
exit or on a failed start, and the `live` fixture empties `get_settings()` once the mock's variables are set and again when the
test ends. The test-local autouse guard in `tests/test_tools_mockllm.py` is gone. Red with the guard removed and `LiveMock`
unchanged: `pytest tests/test_tools_mockllm.py tests/test_serve_drain.py` 7 failed, 120 passed (6 failed alone); green after: 127
passed together and 54 passed alone on each of two runs. `tests/test_latency_smoke.py` still has a copy of the old helper; it is
harmless and was not touched.

**Harness labels.** The L5-only option of W3 now carries its own `duration` ("seed 20 + baseline 15 + drain soak <=120 + L5 60
min": 215 minutes, inside the 3.6 h it is priced at); the four-worker option of W4 is named "4 load workers + the master (5
generator machines; ...)" because the plan's Locust master plus 4 workers is five machines, which the row, the arithmetic and the
$1.60 quote already priced (the row prints "5 x"); both pinned in `tests/test_staging_script.py`. The mock-CPU > 50% VOID rule is
kept but relabelled "harness rule (not pre-registered)": it is tagged `[harness-added]` in `scripts/loadtest_report.py`, a VOID
that rests on it alone now reports `void_without_a_pre_registered_clause`, and the registered clauses are four (the offered rate,
the generator's 70% CPU, the offline checks, the CPU-seconds per live ask); `tools/loadtest/model.py` has the label as a constant
(`HARNESS_RULE_LABEL`), `tools/loadtest/README.md` and `tests/test_loadtest_report.py` agree.

**Corrections to the entries above (append-only; the text above stays as written).**
- 2026-10-06 "The anchor cap" (the bullets on cost against anchors and on which daily cap binds first) and the correction of
  2026-10-08 after it: the estimates $0.580089, $1.565661 and $0.83, and the break-evens 6.32 and 6.16 cents, are all superseded by
  the table above. The break-evens are now 6.24 cents (hybrid, 62,351 micro-dollars) and 6.03 cents (agent, 60,297); at the dearest
  recorded ask (6.6 cents) the $10 cap refuses the 143rd hybrid ask, and at the dearest recorded agent ask (7.07 cents) the 129th.
  The "129,485 tokens at 4 anchors" is Luna's prompt; Sonnet's is 161,856.
- 2026-10-08 "The panel's fixes" item 4 (the page): "a disconnect settles the ask as `abandoned` at its whole estimate, $0.58 to
  $0.83" is stale. An abandoned ask is charged what its meter says (0 before any paid call started, the bounds of the calls that
  started, never less than the provider reported); only a stream that cannot meter, an unreadable meter and a restart charge the whole
  estimate, now $0.71 to $1.02.
- Wave 2 item 2 (the per-address share): "default 1.25, production 0 < x <= 1.25" and "Eight addresses are needed to pause a $10
  day" are superseded: the default and the ceiling are 1.40 and the arithmetic is above.
- Wave 2 blocker B3: the guard is no longer in `tests/test_tools_mockllm.py`; `LiveMock` and the `live` fixture restore the state
  themselves (above), which is the tidy-up that entry promised.
- Wave 2 "`OPENAI_BASE_URL`" and "Still OPEN" ("`ANTHROPIC_API_BASE` and `ANTHROPIC_BASE_URL` are not refused"): they are refused now.
- Wave 2 "Unverified" (the mock-CPU rule tagged `[pre-registered]`): relabelled, above.
- Wave 2 "Still OPEN", Engineering: "a stream that never ran settles at the estimate in `PaidStream` but at 0 in `routes._abandon`" is
  stale: both settle at 0 now (a twin that was never called made no paid call). An unreadable meter still keeps the estimate.
- Wave 2 "Quote labels": the four-worker option's name now says "+ the master (5 generator machines)", and the L5-only option has its own
  `duration` text.

**Still OPEN for the owner.**
- The VOID / FAIL clarification of the harness verdict (and the clauses the harness added on top, the mock-CPU rule among them).
- Decision 14 (the embedder): the fp32 path passes every parity gate; Fly timing and Linux RSS are not measured, and the fp32 Docker
  build has never run.
- The staging window quotes (`--approve-quote`: W0 $0.05, W1 $0.08, W3 $0.45 or $0.25 for L5 only, W4 $1.25 or $1.60 with five generators).
- The go for the first deploy, and the go for nulling the legacy IP hashes.
- Whether the $0.20 left by seven full shares at $1.40 matters (above).

## 2026-10-08 (wave 2c) - The share set to $1.32, tests that no longer depend on the developer's shell, the pre-deploy check

**The per-address share is $1.32** (`config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD`, the default and the production ceiling), set by
two rules and no longer by one. The verifier showed that $1.40, which met only `7 x share < $10`, left $0.20 after seven full
shares: less than every live estimate (the vector ask is $0.265873), so seven addresses could stop live asks for the day, and the
eight-address test only passed because it probed the remainder with a synthetic 200,000 estimate. The rules, applied to the
estimates of "wave 2b" (hybrid $0.709573, vector $0.265873, agent $1.015669, workspace $0.734632):
- (i') pausing live asks needs at least eight addresses: `max_spend_usd_per_day - 7 x share >= hybrid estimate`
  (`10 - 9.24 = 0.76 >= 0.709573`), so `share <= (10 - 0.709573) / 7 = $1.3272`;
- (ii) council test (b), unchanged: `share >= 0.30 + 1.015669 = $1.3157`.
The window is $1.3157 to $1.3272 and $1.32 is inside it (4,331 micro-dollars above the lower edge, 7,203 below the upper). What it
means for one address: two concurrent hybrid asks do not fit (2 x 0.709573 > 1.32), so the second gets the busy text while the
first runs; an agent ask is admitted while the address's settled spend is at most about $0.30, a hybrid ask while it is at most
about $0.61, a workspace ask about $0.59, a vector ask about $1.05. Seven full shares are $9.24 and leave $0.76, which still holds a
hybrid, a workspace and a vector ask (not an agent ask); the eighth address pauses live asks. The RUNBOOK (the section, the table,
the demo arithmetic: about 305 asks at $0.002, 10 admitted and the 11th cut off at the dearest recorded $0.06578) and `.env.example`
carry the new figures. This closes the open owner item "whether the $0.20 left by seven full shares at $1.40 matters": the share is
lower and nothing is left to decide there.

How it is held: `tests/test_serve_config_production.py` computes (i') and (ii) in micro-dollars from `serve.estimate` (the live
configuration) and the production caps, not from the number, and checks that the share sits in the window and that the window is not
empty; `tests/test_state_contract.py` runs the same rules through the state backends (its eight-address test fills seven shares
synthetically and then probes with the service's own hybrid, vector, workspace and agent estimates). Mutations in this pass: a
share of 1.33 fails (i') and the window, 1.31 fails (ii) and the window, and the contract tests at 1.40 fail on `200000 >= 709573`.
Run in this pass: the in-process kind of the contract tests and every other state test. **Not re-run at $1.32:** the database-backed
kinds (`inprocess-db`, `neo4j`) and `tests/integration/test_state_neo4j.py`, because this pass makes no Neo4j call; the verifier's
runs against the throwaway server were at $1.40.

**Tests no longer depend on the machine's environment.** `tests/conftest.py` clears `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
`ANTHROPIC_API_BASE` and `ANTHROPIC_BASE_URL` before every test (a test of the refusal sets the one it needs itself). Claude Code's own
environment exports `ANTHROPIC_BASE_URL`, which made 8 tests in `tests/test_serve_guard_pepper.py` fail locally while CI was green; with
`--noconftest` those 8 fail again, with the fixture none does. `scripts/check_env_fly.py` already built its `Settings` from the files only
(`LiveSettings` has no environment source), and now also takes the names the production validators cover out of the process
environment while it builds and puts them back (also on an error). It also judges the four base variables wherever `.env.fly` holds a
non-empty value, although the default push does not send them (`push_fly_secrets --only` can send any key of the file, and a green
check before that push would boot a machine that refuses to start); every other unpushed key is still only listed by name.

**Corrections to the entries above (append-only; the text above stays as written).**
- "Still OPEN" of 2026-10-08, first entry: "`CHARS_PER_TOKEN = 2.5` is an assumption, not a bound" is stale. The estimate sizes each
  model's prompt at its own ratio (Sonnet 5 at 2.0, every other model at 2.5, `serve/estimate.py`), and `scripts/check_chars_per_token.py`
  checks the ratios against the recorded prompts.
- Wave 2b, "Pinned on the in-process backend ... and were not run here": the database-backed share tests have now been run, on every
  backend, and passed (verifier, 2026-10-08, against the throwaway Neo4j on 7898; that was at $1.40, see above for $1.32).
- Wave 2b, "The per-address share is $1.40", its `7 x 1.40 < 10`, "$0.38" and "$0.69", the stored 1,400,000 and the "Noted for the
  owner" paragraph: superseded by the section above ($1.32, (i'), $0.30 and $0.61, 1,320,000).
- Wave 2b, "this pass did not fetch it" (Sonnet 5 has no long-context premium): recorded now in `src/semigraph/llm_shape.py`. Anthropic's
  pricing page (https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-08) gives Claude 4.6 and later models, Sonnet 5
  included, the full 1M-token window at standard pricing. Luna's threshold is still unrecorded (not in LiteLLM's map).
- `tools/loadtest/cpu_watch.py` no longer describes the mock-CPU rule as part of the pre-registered VOID rule, and
  `deploy/staging/fly.stg.toml` no longer says the production-settings sub-run sets the share "back to 1.25".
- The fake key `sk-ant-api03-not-a-real-key` in `tests/test_serve_config_staging.py` carries a `# gitleaks:allow` marker.
