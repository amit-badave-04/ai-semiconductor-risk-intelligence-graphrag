**Adversarial review of the M5 plan (2026-09-30, read-only; no code changed, no servers started, no paid calls)**

Evidence tags: **code** means a file:line I read on branch v2. **web** means a page I fetched or searched. **inferred** means my own reasoning, marked "verify" where it needs a spike.

**CRITICAL**

1. **CRITICAL: the 1,000-user load-test gate cannot pass as designed, and it proves nothing about the production configuration.**
   - Evidence (inferred arithmetic from the plan's §6):
     - 1,000 VUs with a mean think time of about 210 s give about 4.6 iterations/s.
     - 17% of iterations are live asks, so about 0.8 live asks/s.
     - The mock stream lasts about 10 s (800 ms TTFT plus 300 tokens at 40 tok/s, plus retrieval), so about 8 streams are concurrent at any time.
     - "≥150 concurrent streams held with 0 drops" is therefore unreachable.
     - It is also capped by the plan's own settings: `answer_slots` stays at 2 per machine (code: config.py:68, main.py:236), so 2 machines give 4 streams, and `max_inflight_answers` defaults to 6.
     - PLAN.md:33 got 50–140 streams and 3–7 q/s by assuming every cycle is a question. The plan pairs a traffic mix 4–9× lighter with PLAN.md's criteria, and sizes the fleet (§0.3, decision 3) from the heavier model.
     - "errors < 0.5% excluding 429 busy/shed (< 2%)" is a carve-out that is not in the recorded PLAN.md:103 gate.
   - Change:
     - Pre-register one traffic model and derive the stream criterion, the fleet size and the production caps from it. Any departure from PLAN.md:33 is an owner decision.
     - Split admission into two limits: an embedding/CPU limiter (the scarce resource) and an in-flight LLM stream limit (I/O-bound, sized from measurement, for example tens per machine).
     - Run the gate with the caps that will go live.
     - Put the shed carve-out to the owner, or drop it.

**HIGH**

2. **HIGH: the reserve can drain the daily budget without a Turnstile token.**
   - Evidence (code): today the kill switch and daily ceiling are read-only checks (routes.py:268-271), placed before Turnstile (:272) and the paid window (:275). The plan keeps "gate order unchanged" but merges kill, ceiling, spend and in-flight into one `reserve.lua` that runs `INCR`/`INCRBY`/`ZADD`. Placed at the kill-switch position, every cache-miss request with no token increments the day's counters. At the free window of 30 per 10 min per IP, 5 IPs exhaust 150 a day. A per-machine "busy" answer (routes.py:358) after a reserve leaks the lease. At an in-flight cap of 6, such zombie leases lock out the whole fleet for the lease TTL.
   - Change:
     - Keep a read-only kill/ceiling pre-check where it is today.
     - Make `reserve` the last gate: after Turnstile and the per-IP paid window, and after the local slot is taken, or release the lease on "busy".
     - Add tests: a 403 or 429 or busy answer leaves `paid`/`spend`/`inflight` unchanged.

3. **HIGH: the rollback backend is weaker than production today.**
   - Evidence: the plan names `STATE_BACKEND=memory` as the rollback and retires the `SvcAnswer`/`SvcQuery` writers. With memory, the daily paid ceiling and the ledger reset on every restart or deploy. Today they persist in Neo4j (code: store.py:85-101).
   - Change:
     - Wrap today's `store.py` as a `neo4j` `StateBackend` and make it the production rollback.
     - Keep `memory` for tests and local runs only.
     - Keep the parity tests across all backends.

4. **HIGH: benchmark answers would expire and silently become paid calls.**
   - Evidence (code): store.py:141 exempts `source = 'benchmark'` from the TTL. The plan stores `ans:<key>` with `EX answer_cache_ttl_hours` and seeds with `SET NX` only at boot. `/api/examples` keeps listing the boot-time ids (routes.py:137-148). On an always-on machine, after 24 h an "instant, cached" click becomes a paid live call. That is exactly the behaviour main.py:151-172 guards against.
   - Change:
     - Give examples no TTL, or serve them from process memory (they are static per build and snapshot).
     - Add a test: an example is still cached after the TTL has passed.

5. **HIGH: the new UI goes public before the owner signs it off.**
   - Evidence: §9 M5b flips `UI_V2=true` in production, then runs B6 (the owner click-through). The owner rule is that anything newly public-facing needs an explicit decision, and the plan promises the live site is never worse.
   - Change:
     - Run B6 on staging or a gated preview path (for example `/v2`, not linked) before flipping `/`.
     - Make each M5c flag enable (MCP, OpenAPI, API keys, map) in production an owner decision too.

6. **HIGH: staging could become an open paid LLM proxy, and could leak private data.**
   - Evidence (inferred from §6): the staging app is on a public `*.fly.dev` edge, with always-pass Turnstile test keys, `MAX_QUERIES_PER_DAY=0`, `MAX_SPEND_USD_PER_DAY=0`, and a spoofable `X-Test-Client-IP`. One copied provider secret turns it into a free, unlimited proxy. "the current dump" is ambiguous: a dump of production would copy `User*` workspaces and ledger IP hashes.
   - Change:
     - No provider API keys may exist as staging secrets. Add a boot validator: `ENVIRONMENT=staging` requires the model `api_base` to be the mock.
     - Put an origin-auth header on staging (Locust sends it).
     - Seed staging from `deploy/seed` only, never from a dump of production.
     - Destroy staging after the test days (already planned).

7. **HIGH: decision 3 leaves out an option the code already has, hosted query embedding.**
   - Evidence (code): config.py:45-51 and embeddings_remote.py implement `EMBEDDING_BACKEND=remote`, which calls an OpenAI-compatible `/embeddings` endpoint serving the same model. embeddings.py:144 says uploads refuse to start on it.
   - Change:
     - Add option (d): remote embedding for public-corpus query embeddings only, with local ONNX kept for uploads and token counting.
     - Gate it on a vector-parity check against the shipped q8 ONNX model (top-k overlap on the benchmark).
     - It is an owner decision (new US vendor), and likely far cheaper than the +$200–500/30 days performance fleet. It would also help the TTFB gate.

8. **HIGH: the async rewrite has gaps the planned tests cannot catch.**
   - Evidence and changes:
     - **The ONNX embed can block the event loop.** `encode_query` (about 1.2 s of CPU) is not named among the await-points. Run it through `anyio.to_thread.run_sync` with its own small limiter (1–2). The slow-callback test uses scripted streams and a fake embedder, so add a test with a CPU-bound fake. (inferred)
     - **Cleanup on disconnect may not run.** anyio cancellation is level-triggered, so the lease reconcile and ledger write in a cancelled async generator's `finally` fail unless wrapped in `CancelScope(shield=True)`. Also verify that sse-starlette closes the generator promptly. Verify in S5 and add a test.
     - **Reconcile is billed at the estimate.** When a client disconnects, the provider's usage figure (stream_options include_usage) only arrives on the final chunk, so the reconcile falls back to the estimate. (inferred)
     - **The lease TTL is too short.** It is `llm_request_timeout_s × 2 = 180 s`, below the worst case: 25 s agent budget, 30 s draft (answerer.py:549-556), and the strong model at `attempts=2` × (`num_retries=2` + 1) × 90 s, plus a 5–15 s backoff (answerer.py:361-426). Renew the lease while streaming. (code)
     - **The estimate under-prices escalation.** It must price draft, escalation and agent planner together, not one model.
     - **`time.sleep` in the retry backoff** must become `anyio.sleep` in the async twin.
     - **Agent path equivalence.** Add a recorded agent-path fixture to A2, alongside the SEC-path `answer_events_pre_m4.json`.

9. **HIGH: the TTFB gate is vacuous on `/api/chat`.**
   - Evidence: the §4.5 mapping emits `start {messageId}` when the stream opens, and sse-starlette sends headers at once. Measured naively on the first SSE line, TTFB is about 0 ms. PLAN.md:103 says "TTFB(first step event)".
   - Change:
     - Define TTFB as time to the first `data-retrieval`/`start-step` part (or the `retrieval`/`step` event on `/api/ask`).
     - Pre-register server-side timing spans (Turnstile verify, admission round trip, embed, each Cypher query) so a failure can be diagnosed.
     - Note in the plan that 1.23 s of embedding alone leaves about 0.27 s of headroom.

10. **HIGH: rolling-deploy gate A3 cannot be met as written.**
    - Evidence: Fly's `kill_timeout` defaults to 5 s, maximum 300 s (web). The upload job runs on a daemon `threading.Thread` (code: jobs.py:581). `fly.toml` sets no `kill_timeout`. A job on the machine being replaced dies whatever the sweeper does. In-flight answers (up to 90 s or more) die too.
    - Change:
      - Add a drain design: `kill_timeout` up to 300 s; on shutdown, refuse new uploads and asks on that machine; wait for the running job; use sse-starlette's `shutdown_event`/`shutdown_grace_period` for streams.
      - Restate A3: an upload on the surviving machine finishes `ready`; an upload on the replaced machine either finishes within the drain or is marked `interrupted` within a bounded time, and is never left hanging.

11. **HIGH: the MCP design breaks across machines and may expose paid calls without auth.**
    - Evidence:
      - (web) SDK v2 renamed FastMCP to `MCPServer`. `stateless_http`, `json_response` and `transport_security` moved to `streamable_http_app()`. 2025-era clients use session ids, which break across 2 machines behind the Fly proxy unless the server is stateless.
      - (plan text) §4.5 says `/api/ask` is "used by … MCP", while decision 8 says the MCP server needs "no auth".
      - (code) A search tool would cost about 1.2 s of CPU per call under a read window of 120 per minute per IP (config.py:79).
    - Change:
      - Enumerate the 5 tools and exclude every LLM-backed path.
      - Put any embedding-backed tool behind the embed limiter and a tight per-IP window, or an API key.
      - Configure stateless mode and allowed hosts.
      - Add a two-machine MCP test.

12. **HIGH: the AI Elements answer renderer weakens the XSS invariants.**
    - Evidence (web, search-level): Streamdown, which AI Elements uses to render messages, turns raw HTML on through rehype-raw plus rehype-sanitize, and rehype-harden allows any link prefix by default. The current page escapes everything, makes chips only from the four id forms, allows https-only links (`safeUrl`), and workspace answers strip links and images. None of this uses `dangerouslySetInnerHTML`, so the planned lint ban cannot catch it.
    - Change:
      - Render answers with a ported restricted grammar (for example react-markdown with `skipHtml`, no `img`, links https-only), or pin Streamdown with rehype-raw removed and `allowedLinkPrefixes=['https://']`.
      - Run the chips/badges XSS fuzz fixtures against the rendered React component, not only the pure modules.

**MEDIUM**

13. **The OpenAPI schema is already public today.**
    - Evidence (code, verified by `create_app().openapi()`): main.py:265-266 disables only the docs pages; `openapi_url` defaults to `/openapi.json`, which is live. It lists `/api/admin/policy` and `/api/admin/freshness/check`, undermining the "404 means not authorized" hiding (routes.py:414-418). §2 and risk 11 assume it is off.
    - Change: set `openapi_url=None` in M5a Step 0 now, and curate it in M5c.

14. **The serve requirements are not hash-locked.**
    - Evidence (code): 0 `sha256` lines in deploy/requirements-serve.txt, contrary to PLAN.md:31 ("litellm ≥ 1.83, hash-locked").
    - Change: in Step 0, compile with `--generate-hashes` and install with `--require-hashes` before adding redis, limits, mcp and sse-starlette 3.5.0.

15. **The Valkey client versions may conflict.**
    - Evidence (web): the `limits` 5.8.0 `redis` extra requires `redis<8.0.0`, while the current redis-py is 8.1.0.
    - Change: in Step 0, pin a redis-py version with an import test and a behaviour test of `limits` using `implementation="redispy"` and the moving window.

16. **The sweeper, heartbeat and 24-h deletion promise depend on Valkey.**
    - Evidence: the plan runs the whole sweep only under `lock:sweeper`. The sweep also deletes expired workspaces (code: jobs.py:618-676), so the 24-h deletion promise would stop during a Valkey outage. "Heartbeat refreshed every 15 s by the job thread" misses long blocking stages (parse 90 s, compare 120 s, a slow embed), and a thread cannot use a loop-bound `redis.asyncio` client.
    - Change:
      - Keep the TTL and orphan sweeps unconditional and idempotent on every machine.
      - Put only `fail_interrupted_jobs` under the lock, and make it never fail a job while Valkey is unreachable.
      - Send heartbeats from an independent timer thread with a sync redis-py client.

17. **The `noeviction` reasoning and spike S9 are wrong.**
    - Evidence (web, valkey.io): at maxmemory under `noeviction`, a script's first memory-using write aborts, and INCR/SET/XADD/ZADD fail. Rate limits, heartbeats, reconcile and the ledger would all fail together. valkey.io advises against `noeviction`.
    - Change:
      - Use `volatile-lru` and make counters, leases and policy keys TTL-less (deleted by the sweeper), with TTLs only on caches. Or run a separate cache instance.
      - Add a `used_memory` alert.
      - Rewrite S9's pass criterion.

18. **The Lua spec blocks every ask when the ceiling is 0.**
    - Evidence: `reserve.lua`'s "`paid >= max_paid_day` → 3" has no "0 = off" guard. Today 0 means unlimited (code: routes.py:270). Staging sets 0, so every ask would be refused.
    - Change: add the guard and a test.
    - Also: the reserve's keys cannot all share a `{day}` hash tag (inflight, lease and kill have none), so drop the "cluster-safe" claim.

19. **`retrieval_only` degrade shows a blank answer on the live page.**
    - Evidence: M5a ships `done{degraded:true, answer:""}` while the legacy page is still public. index.html knows no `degraded` field, so it renders a blank answer with badges. The Turnstile/paid-window/embed-limiter gates for this mode are unspecified, so it could become an unauthenticated CPU endpoint.
    - Change:
      - Keep `/api/ask` on the legacy grammar returning the 503 "paused" copy in this mode until the legacy page is updated.
      - Require the full gates and the embed limiter.
      - Add cached, degraded, escalated, workspace, agent, error-before-delta and error-mid-stream fixtures to the AI-stream contract test. Cached and degraded replies have no `delta`, so text parts must be synthesized from `done.answer`.

20. **The load-test mix has blind spots.**
    - Missing from the mix: agent asks (enabled in production), uploads plus job SSE (the CPU interference S1c measured), and static-bundle fetches (under option 1a the API serves them).
    - Mock behaviour is undefined: drafts with no valid ids fail verification and escalate 100% of the time. The escalation model `anthropic/claude-sonnet-5` needs an Anthropic-format mock or an openai-mock name. The agent needs mock tool calls.
    - The CPU criterion on shared machines must be measured against Fly's `cpu_baseline`, with `cpu_balance` not trending to zero over the soak (web). `/proc/stat` utilisation can pass while the burst balance drains.
    - Neo4j (`shared-cpu-1x`) throttles the same way; add it to decision 3 and the cost table.
    - `hard_limit` must be set per machine class.
    - Add a pre/post-M5a live latency smoke run (paid, capped).

21. **The load-test proof goes stale after M2.**
    - Evidence: the owner's order is M5 then M2, and M2 adds reranking and BM25 (PLAN.md:61, :97), which changes TTFB and CPU.
    - Change: pre-register a load-test re-run with the runbook as an M2 or M6 gate.

22. **Scope was dropped silently, and features can be cut without asking.**
    - Evidence: PLAN.md:103's admin console includes "eval scores, Langfuse link", and its Answer screen has an "N risks dropped" badge. C5 and §4.6 omit them without a decision. §1's pre-registered cut order ("each cut is reported") conflicts with "we have time, do it the right way".
    - Change: list them as owner decisions; cuts need owner approval, not only a report.

23. **Policy defaults were set without the owner.**
    - `max_spend_usd_per_day=10.0`, `max_inflight_answers=6`, `answer_slots` staying at 2, a three-level kill switch, and a changed meaning for the public `/api/stats.ledger.all_time` (the rollup expires after 90 days; the stream is `MAXLEN ~200000`; history before cutover stays in `SvcQuery`).
    - Change: make these owner decisions. Define all-time as the Neo4j history plus Valkey, or keep writing `SvcQuery`, since at under 1 write/s it is cheap.
    - Seed `paid:<today>` from `SvcQuery` at cutover, so the ceiling does not effectively double on the cutover day.

24. **API keys have no storage location or quota rules.**
    - Evidence: C2 does not say where the keys live. In Neo4j they are wiped by a dump swap (PLAN.md:27); in Valkey they are lost with Valkey.
    - Change: specify the storage and how it survives a dump swap. State whether keys count against the global paid and spend ceilings.

25. **Per-IP limits are keyed by the full IPv6 address, and the IP hash is unsalted.**
    - Evidence (code): guard.py:57-71 hashes the full address and the hash is unsalted `sha256[:16]`. The whole IPv4 space can be enumerated, so the hash is effectively reversible. The new Valkey ledger stream keeps it for up to 90 days.
    - Change: bucket IPv6 by /64 in the new limiter keys. Use an HMAC with a secret pepper for new keys and the ledger.
    - Also put `ws_hash` in Valkey key names rather than raw workspace ids, matching main.py:50 and jobs.py:90.

26. **The two-process test is not buildable as described, and some files have no owner.**
    - Evidence:
      - An in-process fakeredis cannot be shared by two uvicorn subprocesses.
      - The lifespan needs Neo4j and the 1 GB model (code: main.py:139-148).
      - The job-SSE change lives in `workspace_routes.py:412-453`, which is assigned to no worker.
      - Making the limiter async changes `_read_gate` call sites in routes.py, workspace_routes.py, dossier_routes.py and monitor_routes.py, and none is assigned.
    - Change:
      - Use the CI Valkey service container (or fakeredis' TCP server) with a test app factory that has a fake driver and embedder.
      - Put the async limiter interface and every call-site conversion in main Step 0.
      - Assign `workspace_routes.py` to worker C.
      - Pre-install all shadcn components and npm dependencies in M5b Step 0 so workers never touch `package.json`.

27. **Static serving and the M5b gates have gaps.**
    - `StaticFiles(html=True)` serves `index.html` for directories but not `/company/NVDA` from `NVDA.html`. Pin `trailingSlash: true` and add a test. Exclude `/api/*` from the static mount so API 404s stay JSON.
    - Running `lighthouse` on `web/out` with no API scores error states. Run it against the FastAPI app with recorded fixtures.
    - pdfmake has a history of needing `'unsafe-eval'` (web; 0.3.x behaviour unverified). Spike it under the exact CSP, or use a print stylesheet.
    - Verify that shiki/Streamdown code blocks do not need `wasm-unsafe-eval`.
    - The AI SDK v6 pin is "because AI Elements declares it", but AI Elements is copied into the repo. Record it as a choice, not a constraint.

28. **Follow-up turns (decision 7a) are under-specified.**
    - Retrieval for a follow-up needs a standalone-question rewrite; otherwise "and in 2024?" retrieves nothing. That is a standard pattern and an extra LLM call.
    - Prior turns must go into a separate template, so the `4d0a62f5a0` fingerprint pin holds (code: examples.json).
    - Prior turns come from the client and must be labelled as untrusted.
    - Add a small paid eval that follow-ups do not regress single-turn answers.

**LOW**

29. **Process-local state the plan misses.**
    - config.py:93: an empty `LANGFUSE_HASH_SALT` means a random salt per process, so question hashes will not group across machines. Make it a shared secret.
    - `snapshot_id`, `example_ids` and `graph_stats` are fixed per machine at boot (main.py:228-231), so a dump swap needs a coordinated restart of the whole fleet. Add this to the RUNBOOK.

30. **Minor corrections.**
    - The shared-CPU quota is from Fly's docs, not measured, so relabel it in §2.
    - Pin the `valkey/valkey` image by digest.
    - Also check the `hostname`/`action` fields in the Turnstile siteverify reply (guard.py:130-155 checks only `success`).
    - Allow two origin-auth secrets during rotation.

Checked and not a problem: for a Worker fetching a non-Cloudflare origin, `CF-Connecting-IP` carries the real client IP and cannot be altered (web), so option 1(b)'s client-IP design holds. These plan claims also checked out against the code: 53 examples, fingerprint `4d0a62f5a0`, `check_now` never taking the lease, the sync generator on `/api/ask`, and the M4_PLAN pricing lines.

**Verdict:** The plan is not executable as written. It has 1 CRITICAL and 11 HIGH findings. Its research grounding is good, and it correctly isolates the Cloudflare decisions. Before any M5a code, four things need fixing:
- the load-test traffic model, and the caps and limiters derived from it;
- the position of `reserve` in the gate order, and lease release;
- the rollback backend (Neo4j, not memory) and benchmark answers that never expire;
- the timing of the public UI flip and the MCP/staging exposure.

Decisions 3 and 23 go back to the owner, with the hosted-query-embedding option added. File `/openapi.json` should be closed in Step 0 regardless.

Sources:
- [Cloudflare HTTP headers (CF-Connecting-IP in Worker subrequests)](https://developers.cloudflare.com/fundamentals/reference/http-headers/)
- [MCP Python SDK v2 migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)
- [python-sdk issue #1732, FastMCP renamed to MCPServer](https://github.com/modelcontextprotocol/python-sdk/issues/1732)
- [limits on PyPI (requires_dist)](https://pypi.org/pypi/limits/json)
- [Fly.io configuration reference (kill_timeout, rolling deploys)](https://docs.fly.io/reference/configuration/)
- [Fly.io CPU performance](https://fly.io/docs/machines/cpu-performance/)
- [Valkey scripting and maxmemory](https://valkey.io/topics/eval-intro/)
- [Streamdown security](https://streamdown.ai/docs/security)
- [AI Elements package.json](https://raw.githubusercontent.com/vercel/ai-elements/main/packages/elements/package.json)
- [sse-starlette README](https://raw.githubusercontent.com/sysid/sse-starlette/main/README.md)
- [pdfmake issue #1360, CSP unsafe-eval](https://github.com/bpampuch/pdfmake/issues/1360)
- [pdfmake issue #1371, CSP unsafe-inline](https://github.com/bpampuch/pdfmake/issues/1371)