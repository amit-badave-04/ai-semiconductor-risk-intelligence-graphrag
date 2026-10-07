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
