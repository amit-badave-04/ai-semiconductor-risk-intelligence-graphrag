# Upload memory guard and memory logging (M5a, decision 21)

Status: APPROVED by the owner 2026-11-06; built and reviewed (see Status). Path: full (touches upload input handling).

## Why

The fp32 query embedder adds about 1.26 GB of private memory. Window W1c (2026-11-06, `docs/v2/research/m5-spikes/fly-2026-11-05/w1c`)
ran the real app with it on three fresh shared-cpu-2x 4 GB machines under M4's 30-page upload plus paced asks: peak uvicorn
VmHWM 2,322,556 kB against the 2,400,000 kB limit, lowest MemAvailable 1,471,596 kB, no OOM. Council 7
(`docs/v2/research/m5-councils/council7/verdict.md`) asked for two free safeguards to ship with fp32: refuse a NEW upload
when the machine is short of memory, and log the process's memory so the live margin is visible.

## Requirements

1. Before an upload is accepted (after every gate that refuses on its own account: kill level, drain, Turnstile, workspace
   token, per-address window, size/type checks, quota, the upload slot; immediately before the daily upload charge and the
   job, so a refusal charges nothing; see Implementation notes), the service reads `MemAvailable` from `/proc/meminfo`. If it is below
   `upload_min_mem_available_kb` (default 1,250,000 kB: the upload-parse child's 1 GiB RLIMIT_AS plus a margin; W1c's lowest
   reading under the heaviest measured load was 1,471,596 kB), the upload is refused: 503, `Retry-After: 120`, and a new
   message `MSG_UPLOAD_BUSY` ("Uploads are busy right now. Please try again in a few minutes."). No job is created, nothing is
   stored, the daily upload count is not charged.
2. Asks, cached answers and every read route are unaffected by the guard.
3. A background log line every 60 s (on the existing maintenance thread or loop-lag monitor) at INFO:
   `memory vmrss_kb=... vmhwm_kb=... mem_available_kb=... swap_used_kb=...` for the serving process; and one WARNING line
   per refusal (`upload refused: memory mem_available_kb=... threshold_kb=...`).
4. Where `/proc/meminfo` does not exist (Windows, tests, a non-Linux host) the guard is OFF and logs one WARNING at boot;
   on Linux a read error counts as OFF for that request with a WARNING (the guard is a safeguard, not a security control:
   it must never block uploads because of its own fault).
5. Setting `upload_min_mem_available_kb` (env `UPLOAD_MIN_MEM_AVAILABLE_KB`): production validator requires
   500,000 <= value <= 2,000,000; 0 is allowed outside production only (off).

## Security requirements and abuse cases (checkable)

- S1. The guard reads only `/proc/meminfo` and `/proc/self/status`; no request data reaches a file path. (test: the reader
  takes no request argument; path constants only)
- S2. The refusal body reveals no memory figures or thresholds to the client (no information leak). (test: response body
  equals the message; no digits beyond the status code)
- S3. Log lines carry no IP, IP hash, workspace id, file name or upload content. (test: captured log lines match an
  allow-listed format)
- S4. A visitor cannot trigger the guard for others except through genuine memory use, and genuine memory use is already
  bounded by the existing upload caps (one upload job at a time, size/page/token caps, the daily cap). The guard adds no new
  way to refuse service. (review item)
- S5. The guard runs after the existing gates, so it cannot be used as an oracle before Turnstile or the workspace token
  check. (test: order pinned by the new S5 order tests; there was no existing upload gate-order test)
- S6. A failure of the guard never accepts an upload that an earlier gate refused, and never refuses one for its own
  read error (fail-open only on read error, per requirement 4). (tests)

## Acceptance criteria (tests, written first)

- Fake `/proc/meminfo` below / equal to / above the threshold: 503 + Retry-After + `MSG_UPLOAD_BUSY` / accepted / accepted.
- No job, no stored file, no upload-count charge on a refusal.
- An ask during low memory is admitted as before.
- No `/proc/meminfo`: guard off, one boot WARNING, uploads accepted.
- Read error on Linux: accepted with a WARNING.
- The 60 s log line: fields present, format allow-listed (S3).
- Production validator bounds (S5 setting), value never echoed.
- Gate order: the guard runs after Turnstile and the workspace token (S5).

## Plan

One Sonnet worker (TDD) owns: `src/semigraph/serve/memguard.py` (new: readers + decision), `src/semigraph/serve/workspace_routes.py`
(the call site), `src/semigraph/config.py` (setting + validator), the maintenance thread or monitor (the log line),
`src/semigraph/serve/routes.py` (message constant only if the messages live there), tests. Then `ecc:code-reviewer` (sonnet),
`ecc:security-reviewer` (opus), the full suite, the Opus verifier. Ships in the decision-21 deploy.

## Implementation notes (stage 2)

- **Gate placement.** Requirement 1 lists "daily upload cap" among the gates the guard follows and also says a refusal must not
  charge the daily count. The only daily-cap gate is `store.reserve_daily_upload`: it increments the counter and has no
  refund, so both cannot hold. The guard therefore follows every gate that refuses on its own account, the upload slot
  included, and runs immediately BEFORE the daily charge: uploads available -> draining -> read-rate window -> workspace
  token -> kill level -> Turnstile -> per-address upload window -> size cap -> byte gate -> quota (and unchanged re-upload)
  -> slot -> **memory guard** -> daily budget -> job. A refusal raises inside the slot's existing `try/except`, which gives
  the slot back (tested, with the drain count).
- **There was no existing upload gate-order test**, so the order is pinned by new tests (S5): thirteen earlier gates each
  answer for themselves under memory pressure without the guard being consulted, and one test records the call order
  (workspace token, Turnstile, slot, memory guard, daily budget, job).
- **Log line host:** the maintenance thread (it already ticks in production, off the event loop, with an injectable clock).
  `MaintenanceThread(..., memory_log=memguard.log_memory)`; first line at the first tick, then every 60 s. The boot WARNING
  is `memguard.log_boot_posture()` called next to `log_bot_check_posture` in the lifespan.
- **For the S4 reviewer:** a memory refusal still spends one try of the visitor's per-address upload window (10 per hour),
  exactly as a busy-slot refusal does today, because that window is an earlier gate. It charges nothing global (no daily
  count, no slot, no job).
- **Real `/proc`:** every unit test reads a fake file. One test (`test_the_real_proc_files_parse_in_a_process_marked_non_dumpable`)
  reads the kernel's files after `hardening.make_process_non_dumpable()`, the production order; it is skipped where `/proc`
  does not exist (this Windows machine) and runs in the ubuntu serve-shipped CI job.
- **Not done in this change:** `fly.toml` is untouched (the code default, 1,250,000, is inside the production bounds), and
  `.env.example` was not edited (see the hand-off note for the line to add).

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves | **Approved by the owner 2026-11-06** (answer "Approve the spec (Recommended)"), within the owner's "approve 21, deploy it". Wording amended after review to match the built gate order (requirement 1, S5); no behaviour change. |
| 2 Dev (TDD) | tests red then green; reviews clean | **Tests RED then GREEN (2026-11-06).** RED, run against an all-off skeleton of `memguard` (and a signature-only `memory_log` kwarg): `uv run pytest tests/test_serve_memguard.py tests/test_serve_config_production.py tests/test_check_env_fly.py tests/test_serve_lifespan.py` = 60 failed, 206 passed, 1 error (the drain-count check in the teardown of the no-job test). By file: memguard 43 failed + 1 error (18 passed: the off-path tests, green by design: equal, above, zero, missing file, silent accept, asks and reads untouched), config_production 13 failed, check_env_fly 1 failed, lifespan 3 failed. The S5 gate tests went RED on the missing `MSG_UPLOAD_BUSY` only, so their real RED is a mutation: the guard placed first in `upload_document` = 19 failed (13 S5 gate cases, the S5 order test, the S1 route test, 4 S6), the guard placed after `reserve_daily_upload` = 2 failed (the no-charge test and the order test); both reverted. GREEN: `tests/test_serve_memguard.py` 61 passed + 1 skipped (the real-`/proc` test, Linux only), `test_serve_workspace_routes.py` 62, `test_serve_config_production.py` 107, `test_check_env_fly.py` 38, `test_serve_lifespan.py` 60, `test_state_maintenance.py` 42: together 370 passed, 1 skipped. Acceptance criteria and S1-S6 map to tests in `tests/test_serve_memguard.py` (S1 `test_s1_*`, S2 `test_s2_*`, S3 `test_s3_*`, S4 `test_s4_*` + review item, S5 `test_s5_*` (14 cases), S6 `test_s6_*` (6 cases)). **Reviews (2026-11-06):** `ecc:code-reviewer` (sonnet) APPROVE, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 3 LOW (spec wording, now amended; S4 window try accepted as the busy-slot precedent; two brittle structure tests, optional). `ecc:security-reviewer` (opus) 0 CRITICAL / 0 HIGH / 0 MEDIUM, 2 LOW (a refusal spends one per-address try, documented; MemoryError caught as the guard's own fault, fail-open per requirement 4), 5 INFO (pre-existing multipart buffering before any gate; one-bit signal; `maintenance.py` error line safe by review; real-`/proc` test first runs in CI; bounds hold, value never echoed). S1-S6 PASS. |
| 3 Regression | full suite green | Full suite: `uv run pytest -q -p no:cacheprovider --ignore=tests/test_loadtest_local_smoke.py` = **9563 passed, 422 skipped, 2 xfailed, 0 failed** (571 s). (A first run, with the serve-shipped run going at the same time, had 1 failure, the timing test `tests/test_retrieval_workspace_regex.py::test_cost_grows_linearly_with_length[valid-citations]`; it passes alone, 444 passed, and in the quiet rerun.) Serve-shipped selection in `C:\temp\sg_sv` (`PYTHONPATH=src`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, the file list of `.github/workflows/tests.yml`) = **5862 passed, 216 skipped, 0 failed** (343 s). Skips are the opt-in Neo4j tests, the dev-only pandas/pyarrow/onnx packages, and the real-`/proc` test (no `/proc` on Windows): that test has not run anywhere yet and runs in the ubuntu CI job. |
| 4 Security | /security-review, gitleaks before push | **2026-11-06:** `/security-review` (Opus) over `6c77042..2afdd6e`: no finding at confidence >= 8. gitleaks over the 2 commits: no leaks. No lockfile changed. `/red-team` skipped for this deploy at the owner's choice (still owed before a release tag). `differential-review`, semgrep and `spec-to-code-compliance` are merge-time checks: v2 is not being merged now. |
| 6 Verifier | Opus | **PASS WITH CONDITIONS (2026-11-06).** 30 mutations in a scratch copy (threshold, placement, fail-open, response leaks, ask/read isolation, validator, 60 s line): every one failed at least one test; control 370 passed, 1 skipped. Full suite 9563 passed / 422 skipped / 2 xfailed / 0 failed; serve-shipped 5862 passed / 216 skipped / 0 failed. Real-`/proc` equivalent run under WSL Ubuntu-24.04 (non-root, after `make_process_non_dumpable()`): parses, line allow-listed, accepts at 1,250,000. gitleaks clean on the 14 files. Condition: the ubuntu CI log shows `test_the_real_proc_files_parse_in_a_process_marked_non_dumpable` run, not skipped. Open before merge (not deploy): the `.env.example` line (that file is read-denied in the agent session). |
| 7 Push, CI | green | **Pushed `03429a2` (guard) and `2afdd6e` (fp32) 2026-11-06; CI run 42164683354 green on all five jobs.** The serve-shipped job collects this spec's tests (`tests/test_serve_*.py`) and its `-rs` list skips none of them, so the real-`/proc` test ran on ubuntu (5865 passed / 213 skipped, 3 more passes than on Windows: this test and the 2 Linux-only hardening tests): the verifier's condition is met. **Live since release v15** (`docs/v2/M5_DECISIONS.md` 10.9): the memory line logs at boot and every 60 s (MemAvailable about 1.64 GB at idle against the 1.25 GB threshold). |
