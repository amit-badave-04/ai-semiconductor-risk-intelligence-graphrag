# Fixes for the 2026-11-21 red-team findings

**Status: approved by the owner 2026-11-22** ("fix RT-001, RT-002, RT-005, RT-006, RT-007; fix RT-004; RT-003 in deploy C;
don't do history rewrite for RT-005"). Source: the red-team report of 2026-11-21 (commit `d00838e`). Path: full (input
handling, the request path, LLM answer checks). Each finding already has a failing test in `tests/security/`; each fix makes
its test pass without loosening another.

## Requirements

**F1 (RT-001, RT-002) Bound every request body in size and time, before routing.** A pure ASGI middleware in
`src/semigraph/serve/asgi_middleware.py`, wired in `main.create_app()` for every environment.
- JSON routes (every route except the document upload): a body over `JSON_BODY_MAX_BYTES` (64 KiB) is answered 413 without
  reaching the route, both when `Content-Length` declares it and when a chunked body grows past it.
- The document upload route (`POST /api/workspace/{id}/documents`) keeps its own cap (`upload_max_bytes`, 15 MiB) and the
  same middleware applies that cap at the transport level too.
- A body that is not complete within its read deadline is answered 408 and the connection closed: `JSON_BODY_DEADLINE_S`
  (10 s) for JSON routes; for uploads a deadline that allows the full upload at a slow but honest rate
  (`UPLOAD_BODY_DEADLINE_S`, sized and justified in the code).
- Requests with no body (GET, HEAD, `GET /healthz`) pass untouched; SSE responses are unaffected (only the request body is
  bounded).
- Limit: the deadline cannot be shown against Fly's proxy locally (RT-002's open question). The local test drives the ASGI app
  with a slow body; whether Fly buffers bodies is recorded as unverified.

**F2 (RT-006) The rate limiter forgets idle clients.** `src/semigraph/serve/guard.py` `RateLimiter`: keys whose newest
event is older than the window are removed by a sweep that runs once per window, and also every `MIN_SWEEP_INTERVAL_S`
(2 s) while the map is above `MAX_BUCKETS`, never on every call. If the map is still above `MAX_BUCKETS` after the idle keys
are gone, the keys idle longest are evicted, even if still inside their window: refusing new keys instead would let a
20,000-address flood lock out every new visitor, while the evicted key's owner already holds that many windows. A key
inside its window is never removed while the map is at or under `MAX_BUCKETS`. `allow()` stays O(1) amortised (eviction
uses `heapq.nsmallest`, never a full sort).

**F3 (RT-007) A verified user is never starved by the shared cache budget.** `src/semigraph/serve/routes.py`: when the
process-wide `cache_budget` is empty, the pre-Turnstile cache read is skipped (no 429); the ask then meets Turnstile as
usual; after Turnstile passes, the cache is read without taking a budget token, so a cached answer is still served free. An
unverified ask on an empty budget gets the Turnstile refusal (403), never the budget's 429. The free window and every other
gate are unchanged.

**F4 (RT-004) On the workspace path, a figure is grounded only by the source its own clause cites.** In
`src/semigraph/retrieval/workspace.py` (and only if necessary `verify.py`, without changing the public answer path's
results): SEC-cited figures and "computed:" lines are read from the SEC context only; doc-cited figures from their own
chunk. An upload-only amount or a "computed:" line inside an upload no longer grounds a sentence that cites an SEC id; the
same figures cited to the upload stay grounded. The unit is the clause, as the existing clause splitter cuts it (stricter
than a sentence: a citation in another clause of the same sentence does not ground the figure; marked `shortcut:` in
`verify.py`). The SEC-only context also feeds the removal-claim check and the uncited-statement exemption on the workspace
path, so upload text supports neither (the stricter direction). The public answer path's checks are unchanged: a plain
sources dict grounds amounts on the context alone, as before (`test_f4_public_path_grounds_amounts_on_the_context_only_as_before`). The `suspicious` flag stays unrendered: showing it needs new page wording (owner approval) and is out of scope.

**F5 (RT-005) No personal address in the repository's tracked files.** The four notebooks read `SEC_USER_AGENT` from the
environment with no personal default (and say so if it is missing); no notebook output keeps the address. Git history is
not rewritten (owner, 2026-11-22): recorded in `docs/security/accepted-risks.md`.

**Deferred.** RT-003 (a data-boundary rule in `answer.txt`) ships with deploy C, inside M2a I5 (it moves the template
fingerprint, R9). Recorded in `docs/security/accepted-risks.md` as deferred, with the M2a spec reference.

## Security requirements and abuse cases (checkable)
- A 2 MiB JSON body, declared or chunked, gets 413 and never reaches validation, the free window or the state backend
  (`tests/security/test_redteam_RT001.py`).
- A JSON body dripped slower than its deadline gets 408 within the deadline plus a small margin; an honest upload at the
  slowest allowed rate completes (new tests beside RT-001).
- After every window expires, the limiter holds at most `MAX_BUCKETS` keys, and within 2 s of exceeding it on any window
  length; a live key is never dropped while the map is at or under the bound
  (`tests/security/test_redteam_RT006.py`, `test_redteam_F2_limiter.py`).
- With the cache budget drained by anonymous asks, a verified ask from a fresh address is not 429, and an unverified one gets
  403 (`tests/security/test_redteam_RT007.py`).
- An upload-only amount and an upload "computed:" line do not ground an SEC-cited sentence; the same figures cited to the
  upload stay grounded (`tests/security/test_redteam_RT004.py`).
- No tracked text file holds a personal webmail address (`tests/security/test_redteam_RT005.py`, file paths only in its
  output).
- No fix weakens an existing control: the full suite stays green.

## Status
| Stage | Evidence |
|---|---|
| 1 Spec | Approved by the owner 2026-11-22 (above) |
| 2 Dev (TDD) | Workflow wf_16acb79b-44d (three serial Sonnet implementers): every PoC test in tests/security/ seen RED, then GREEN (RT001 3 of 4 RED, RT004 2 of 3, RT006 1 of 2, RT007 2 of 3, RT005 RED by its scan); new tests/test_serve_body_limit.py. Round 2, workflow wf_037f7e17-53c: per-sentence grounding on the workspace paths (sync and async), `connection: close` on any response sent before the body is complete, the limiter sweep also triggered every 2 s while over MAX_BUCKETS (longest-idle eviction kept on purpose; refusing new keys would lock out every new visitor under a flood), RT-007 ordering pins; tests/security/test_redteam_F4_sentence.py, test_redteam_F2_limiter.py, test_redteam_RT007_cached.py, each RED first except the RT-007 pins (behaviour already right). Round 3 (main session): BodyLimit decides by framing, not method (a GET or OPTIONS that declares a body is bounded), two tests RED then GREEN; the smoke-test notebook no longer prints secret values. **2026-11-22 RT-007 residual (M2a track A):** the workspace token check took the shared cache budget before Turnstile and answered 429 when it was empty; with the budget empty it is now deferred to after Turnstile, runs unbudgeted and before the paid window (never skipped). New tests/security/test_redteam_RT007_workspace.py: 3 of 3 seen RED, then GREEN (drained + verified + good token is not 429; drained + unverified is 403; drained + verified + bad token is 404 with no reserve). |
| 3 Regression | Round 2 targeted run 5,708 passed, 0 failed (serve, security, verify, answer, workspace suites; the one host-hanging drain test deselected). Full non-integration suite: 11,264 passed, 4 failed, 223 skipped (the 4 are environmental and outside these fixes: three need the built web dist, run in the off-OneDrive mirror; tools/probe/w1d.py is the CRLF working-copy hazard). After the verifier's last fix (public amounts back to context-only): tests/security, verify grounding and the workspace suites 172 passed; routes and state wiring 114 passed. Integration on T not run: no query changed. |
| 4 Security | Review lenses on Opus with two Opus refuters per serious finding, twice. Round 1 upheld: F4 pooled grounding, the early-response connection gap, five test gaps (all fixed in round 2). Round 2 upheld one MEDIUM by one of two refuters (a GET with a declared body skipped the limits: fixed in round 3) and two LOWs (the notebook print: fixed; workspace asks still take the shared cache budget before Turnstile, as spec F3 kept them: open, reported to the owner). The test-strength lens found no surviving mutation in round 2. |
| 5 Fix findings | The owner approved by ID 2026-11-22. Deferred and declined parts recorded in docs/security/accepted-risks.md (RT-003 to deploy C; RT-005 history not rewritten). |
| 6 Re-test and verifier | Opus verifier: NOT READY on two items (the public path grounded amounts on cited sources; a leftover placeholder), both fixed test-first and recorded here; its wording warnings applied to F2, F4 and the report. |
| 7 Push, CI, deploy | Pushed c07b897 (gitleaks no leaks; pre-push /security-review: no findings). The owner ran `check_env_fly` (PASS), `flyctl deploy` (image `deployment-01M7ZX8TAZ5SZ38QBH4SNCA08E`) and the deployed-mode image check (all PASS, 4 skipped) on 2026-11-22. Live: an oversize POST /api/ask (100 KB) answered 413; /healthz 200; /api/freshness still serves since_data (31 filings, 2 rules). Rollback target: deploy B `deployment-01M7ZP9CH6MG9RK5E62JWG50P9`. |
