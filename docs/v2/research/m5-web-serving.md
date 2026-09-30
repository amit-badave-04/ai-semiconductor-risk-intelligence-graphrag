# M5 research: serving/scale stack (as of 2026-09-30)

Scope: `web-serving` track for M5 — Valkey/Redis, atomic admission/spend, FastAPI async SSE + LiteLLM
streaming, Fly.io platform mechanics, Cloudflare-in-front-of-Fly, load testing, Cloudflare Access,
MCP SDKs, API-key handling, Neo4j GDS Community. Read-only research; no repo files changed, no
servers started, no paid APIs called, no Neo4j connections opened.

Every claim is tagged **VERIFIED** (I saw it on a primary source myself — repo file, installed
package source that matches the pinned production version, PyPI/GitHub API JSON, or a docs page I
fetched and quoted) or **UNVERIFIED** (search-engine synthesis I could not pin to an exact quoted
sentence, or a number a summarizing fetch computed rather than quoted verbatim). Web citations carry
the access date **2026-09-30**.

---

## 0. Recommendation summary

| Area | Recommendation | Why |
|---|---|---|
| Valkey hosting | **Self-host** Valkey on its own small Fly machine + a volume, on the private network (`.internal`), `requirepass` + bind 6PN only | Roughly cost-neutral vs. Upstash pay-as-you-go at demo scale (both ≈$9–14/mo-equivalent, §2) — the real case is eviction-policy control and no per-command billing spike during the load test, not cost; one more moving part to operate |
| Python client | **redis-py `redis.asyncio`** (protocol-compatible with Valkey; the project already lists `neo4j`, `litellm`, etc. as plain async deps — no new exotic dependency) | Valkey speaks the Redis serialization protocol; redis-py is the ecosystem default, MIT, huge maintenance base. `valkey-glide` is Valkey's own client (Rust core) and is a fine alternative but is new to this codebase | 
| Rate limiting | **`limits`** library (async Redis/Valkey backend, moving-window strategy) for the sliding-window per-IP/day limits | Purpose-built, MIT, actively maintained (§2) — don't hand-roll the window math |
| Reserve→reconcile (spend ledger) | **Hand-rolled Lua via `register_script`**, integer micro-dollar units, lease as a ZSET scored by expiry | No mainstream library does estimate-then-true-up billing; this is exactly the kind of "differentiator" custom code the owner's global rules call out as justified |
| SSE transport | Convert the **whole streaming call chain** — `_paid_stream` (routes.py:347), the answerer's `yield from` generators (answerer.py:590,:673,:682), and the LangGraph agent path (routes.py:303) — from sync to async, not just the outer function | Today it's pulled through the same 40-slot anyio thread pool as every Neo4j `run_in_threadpool` call (§1 — VERIFIED from the exact pinned library source). Making only `_paid_stream` `async def` and leaving the rest sync just re-adds the thread pool one level down via implicit `to_thread` wrapping (§1) |
| Concurrency model | **More machines, not more uvicorn workers per machine** | Fly's proxy load-balances across machines; a second uvicorn worker doubles the ~1 GB ONNX embedder's RAM on the same machine (§3, §4) |
| Cloudflare in front | **Depends on an owner decision the research cannot make** — see §6 and §13 | Free `*.workers.dev` cannot originate a Transform-Rule header and has a 100k req/day cap (§6); a zone on Cloudflare DNS is the only path to Transform Rules + Access on the API's own hostname |
| Cloudflare Access | Fine as an **invite gate for a buyer demo** (≤50 users, free) — **not** a control for "1,000 concurrent users" | Free-tier seat cap is 50 (§9) |
| Load test | **Never point Locust at the production app** — the per-IP limiter, Turnstile, daily ceiling, cache and ledger writes all corrupt a real test (§8) | Use a throwaway staging app + Turnstile's official dummy keys + varied/cache-busting questions + a mock LLM |
| MCP server | **Official `mcp` Python SDK** (streamable HTTP) over `fastmcp` for a small read-only server, since `fastmcp` itself *depends on* (wraps) the official `mcp` package, not the other way around | `fastmcp-slim` (fastmcp's implementation package) lists `mcp<3.0.0,>=2.0.0` as a hard dependency (VERIFIED, PyPI JSON) — building directly on `mcp` avoids one dependency layer for a small, fixed, read-only tool set (§10) |
| API keys | High-entropy random key, **SHA-256 digest**, store only the hash, compare with `hmac.compare_digest` — mirror the existing, live workspace-token pattern | Matches `src/semigraph/uploads/repo.py:163,174-178` (`hashlib.sha256(token...)`, `hmac.compare_digest(given_hash, stored_hash)`), already shipped and including a dummy-hash constant-time defense (`repo.py:45-46`) (§11) |
| Neo4j GDS | Run GDS **offline, on the pipeline/build Neo4j**, ship precomputed centrality as graph properties in the dump; never load GDS on the 1 GB production Neo4j | GDS Community is free but GPL-licensed when self-assembled, and is a JVM-heap-hungry plugin unsuited to a 1 GB machine (§12) |

**Cost, sin region, per 30 days (VERIFIED where marked — see §5.3 for the full reconciliation between an
owner-vetted repo figure and this research's own, less trustworthy, web-fetched numbers):**

| Scenario | Machines | ≈ cost / 30 days |
|---|---|---|
| Today (as deployed) | 1× API `shared-cpu-2x`/4GB (sin) + 1× Neo4j `shared-cpu-1x`/1GB (sin) | **$35.09** (VERIFIED, `docs/v2/M4_PLAN.md:523`, owner-vetted 2026-09-29) + **≈$9–14 (UNVERIFIED estimate, §5.3)** ≈ **$44–49/mo** |
| M5 steady, **demo traffic** (low, bursty, high embedding-cache hit rate assumed) | 2× API `shared-cpu-2x`/4GB + 1× Neo4j 1GB + 1× Valkey `shared-cpu-1x`/1GB + 1× 1GB volume | ≈ 2×$35.09 + $9–14 + $9–14(est.) + $0.15 ≈ **$88–98/mo** |
| M5 **target-scale / load-test day** (3–7 new q/s, low embedding-cache hit rate — §5.2) | as above, **plus 4–9× `performance-1x` machines** for query-embedding capacity | +≈$240–540/mo (UNVERIFIED — approximate owner-quoted per-machine price, PLAN.md:500; see §5.2 for the full derivation and its uncertainty) |
| Load-test day only (extra, on top of whichever row applies) | +N temporary Fly Locust machines for a few hours, sin region, billed per-second, preferably `performance-1x` (§5.2); teardown after | low single-$ for a few hours (§7) |

**These are materially higher than a generic Fly pricing-page lookup would suggest** — see §5.3 for why:
the owner's own 2026-09-29 quote applies a documented **1.269 regional markup for `sin`** that this
research's own web fetch of the pricing calculator did not surface (it silently defaulted to the `iad`
region). **The gap between the "demo" and "target-scale" rows is the single largest number in this report
and the one least backed by a real quote** — it hinges entirely on an assumed embedding-cache hit rate this
research could not measure (§5.2, §13). Add LLM tokens (mocked during the load test → **$0**) and Cloudflare
(free tier sufficient for a demo, **not** provably sufficient for the sustained load-test's request volume
through a free Worker — §6). Upstash-on-Fly's per-command billing is roughly cost-neutral with self-hosted
Valkey at this scale, not a clear win either way — see §2.

---

## 1. Repo grounding (what M5 inherits — all VERIFIED, read-only)

- **Pinned production stack** — `deploy/requirements-serve.txt`: `fastapi==0.141.1` (:57), `anyio==4.15.1`
  (:13), `litellm==1.100.0` (:143), `neo4j==6.3.0` (:153), `sse-starlette==3.4.11` (:260), `starlette==1.6.0`,
  transitive (:262), `uvicorn==0.52.4` (:308). **No `redis` (or `valkey`) client is pinned in this file at
  all today** — any Redis-protocol version numbers cited elsewhere in this report (e.g. `redis==8.1.0`) are
  this research's own *current-PyPI-version* lookups for a future M5 dependency, not an existing pin; do not
  read them as "already in the repo."
  `docs/v2/PLAN.md:31` separately records `litellm 1.102.1` as the version "import-tested together" and a
  floor of "≥ 1.83, hash-locked" (because 1.82.7/1.82.8 were a supply-chain compromise — confirmed below,
  §2 has no bearing on the fix but it belongs here: the two numbers, 1.100.0 pinned vs 1.102.1 tested,
  should be reconciled before M5 ships).
- **fly.toml**: `app = "semigraph"`, `primary_region = "sin"`, one machine `shared-cpu-2x` / `4gb`
  (`[[vm]] size = "shared-cpu-2x", memory = "4gb"`), `http_service.concurrency` `soft_limit = 20`,
  `hard_limit = 40`, `auto_stop_machines = "off"`, `min_machines_running = 1`. Neo4j is a **separate app**
  reached over the private network.
- **The thread-per-stream blocker PLAN.md:33 names is real and version-exact** (this is the single most
  load-bearing finding in this report, and it took reading the *installed* library source at the *exact
  pinned version* to confirm, not just the changelog):
  - `src/semigraph/serve/routes.py:347` — `_paid_stream(...)`, docstring: *"Sync generator (runs in the
    threadpool)"*.
  - `src/semigraph/serve/main.py:236` — `app.state.answer_slots = threading.BoundedSemaphore(settings.max_concurrent_answers)`;
    `src/semigraph/config.py:68` — `max_concurrent_answers: int = 2`.
  - `src/semigraph/serve/routes.py:277` — `return EventSourceResponse(_paid_stream(...), ping=15, sep="\n")`.
  - `sse_starlette` **3.4.11** (the exact pinned version — `.venv/Lib/site-packages/sse_starlette-3.4.11.dist-info`
    matches `deploy/requirements-serve.txt` exactly), `sse.py:290-292`:
    ```
    if isinstance(content, AsyncIterable):
        self.body_iterator = content
    else:
        self.body_iterator = iterate_in_threadpool(content)
    ```
    `_paid_stream` is a plain sync generator, not `AsyncIterable`, so it is wrapped.
  - `starlette` **1.6.0** (also exact-version-matched), `starlette/concurrency.py:31-33` and `:46-56`:
    `iterate_in_threadpool` calls `await anyio.to_thread.run_sync(_next, iterator)` **once per item
    yielded** — i.e. once per SSE delta, not once per request.
  - `anyio`'s default worker-thread pool is a `CapacityLimiter(40)` (`anyio/_backends/_asyncio.py:3038`,
    confirmed in the locally installed `anyio==4.14.1`; production pins `4.15.1` — I did not re-open the
    exact 4.15.1 source, so treat the *value* 40 as **UNVERIFIED-at-4.15.1-but-very-likely-unchanged**,
    since it is anyio's long-standing default and no anyio 4.15 changelog entry touches it).
  - **Net effect**: every delta of every streamed answer, *and* every `run_in_threadpool(...)` call used
    for Neo4j reads elsewhere in `routes.py` (e.g. `:130`, `:158`, `:168`, `:214`, `:258`, `:261`, `:264`,
    `:268`, `:270`), draws from the **same shared 40-thread pool** on one process. `max_concurrent_answers=2`
    caps concurrent *answers* today, but if that cap is simply raised for M5 without an async rewrite, the
    DB/ledger `run_in_threadpool` calls start queuing behind stream-delta threads on the same limiter —
    this is the mechanism behind PLAN.md:33's "thread-per-stream sync generator" flag, made concrete.
  - **Fix**: make `_paid_stream` an `async def` generator (`async for` over the LiteLLM async stream,
    `await` for Neo4j calls via an async driver session) so it satisfies `AsyncIterable` and sse-starlette
    stops routing it through `iterate_in_threadpool` at all.
- **Upload-job registry is in-process, single-machine** — `src/semigraph/uploads/jobs.py:19` ("watcher on
  THIS process (`JobRegistry`) — fan-out: every watcher sees every event"), `:111` (`class JobRegistry`),
  and `:646` explicitly names the gap: a "real cross-machine lease (a heartbeat row per job, or a lease
  token like `serve.monitor`'s `SvcLease`)" is what would be needed for more than one machine.
  `src/semigraph/serve/workspace_routes.py:412` (`_job_event_stream`) and `:452`
  (`EventSourceResponse(..., ping=SSE_POLL_TIMEOUT_S, ...)`) serve upload-progress SSE from that same
  in-process registry. **Consequence for M5**: scaling the API to N machines (§5) breaks upload-job SSE
  affinity unless one of (a) `fly-replay` pins that request back to the owning machine, or (b) job state
  moves into Valkey (pub/sub or a stream) so any machine can serve the watch. The freshness monitor already
  has a working cross-machine **lease** pattern to copy: `src/semigraph/serve/monitor.py:40-68` (acquire the
  lock row before the `WHERE` filter, "so two machines racing for the lease cannot both see it as free").
- `src/semigraph/serve/workspace_routes.py:58` — `SSE_POLL_TIMEOUT_S = 10` (comfortably under Cloudflare's
  100 s idle timeout, §6). The paid-answer stream's `ping=15` (`routes.py:277`) is too.
- `src/semigraph/serve/guard.py:59` — the trusted client-IP header is `CLIENT_IP_HEADER=fly-client-ip` on
  Fly; `src/semigraph/config.py:70` documents it must be "set by a TRUSTED proxy." Putting Cloudflare in
  front changes what "trusted proxy" means — Fly's edge stops being the outermost hop (§6).
- `src/semigraph/serve/routes.py:270-277` — the **existing gate order** for a paid answer is: kill-switch →
  daily ceiling → Turnstile → per-IP rate limiter → stream. Every one of these will reject or pollute a
  naive Locust run against production (§8).
- **No OSV advisories** were found against `api.osv.dev/v1/query` (2026-09-30) for the five **actually
  pinned** production versions checked (`litellm==1.100.0`, `sse-starlette==3.4.11`, `starlette==1.6.0`,
  `fastapi==0.141.1`, `anyio==4.15.1`) plus two **not-yet-adopted, latest-PyPI** versions checked as a
  forward look for this report's own recommendations (`redis==8.1.0`, `locust==2.46.6` — neither is in
  `requirements-serve.txt` today). VERIFIED (zero results is itself the verified fact, not an absence of
  checking).

---

## 2. Valkey vs Redis on Fly.io

**Valkey server.** Latest stable release `9.0.6` / `9.1.2` (2026-09-01), with `8.1.10` and `8.0.11` also
current on the 8.x line; a `9.2.0-rc1` prerelease exists (2026-09-16). VERIFIED —
`gh api repos/valkey-io/valkey/releases` (2026-09-30). BSD-3-Clause, 27.3k GitHub stars, pushed same day
as this research — very healthy. VERIFIED — GitHub repo API.

**Self-hosting on Fly.** A dedicated Fly machine + attached volume, on the private network only
(bind 6PN, `requirepass`), gives:
- **Cost**: a `shared-cpu-1x`/1GB machine in sin is **≈$9–14/mo, UNVERIFIED-estimate** (§5.3 — this report
  could not get an owner-vetted or directly-quoted sin figure for this exact machine size; an earlier draft
  of this section said "$2–4/mo," which used this research's own unreliable iad-based per-second rate before
  §5.3's reconciliation against the owner's dated, sin-specific quote — that number is retracted) plus a
  small volume at **$0.15/GB-month, pro-rated hourly, billed even when the attached machine is stopped**
  (UNVERIFIED-paraphrase, consistent across independent sources — see §5.3). **At this price, self-hosting
  is not clearly cheaper than Upstash's pay-as-you-go $0.20/100k-requests at low, demo-scale traffic** — the
  two are close enough that cost alone shouldn't decide it (§13.3). The stronger arguments for self-hosting
  are **eviction-policy control** (below — Upstash's own random-eviction behavior has the same counter-vs-
  cache collision risk, but you can't tune it), **no per-command billing surprise under a sustained load
  test** (§7's burst traffic could run $1–12/hour on Upstash pay-as-you-go per §2 below), and **private-network
  latency** to an always-on API (§5.4) — not raw monthly cost, which this report can no longer claim is
  clearly lower.
- **Persistence**: AOF is Valkey's durable option; RDB is periodic-snapshot. UNVERIFIED specifics for the
  *official* Valkey image's default (the search results I got were for the third-party Bitnami image, which
  defaults AOF on — this does not establish the official `valkey/valkey` Docker Hub image's default; check
  that image's own docs before deploying).
- **Eviction-policy gotcha (real, worth flagging loudly)**: if the same Valkey instance holds both a TTL'd
  query/embedding **cache** and the **spend-ledger/rate-limit counters**, an `allkeys-lru`-style eviction
  policy sized for the cache can evict a counter key under memory pressure, silently resetting the daily
  spend ceiling — an overspend bug that would not show up in normal testing (it only triggers near
  `maxmemory`). Options: (a) two logical databases/instances — `noeviction` + bounded key set for
  counters/leases, LRU for cache; (b) never let counters carry a TTL shorter than their logical lifetime and
  size `maxmemory` generously; (c) use Valkey's per-key persistence guarantees and monitor `evicted_keys`.
  This is my own risk analysis given Valkey's documented eviction semantics, not a single quoted source —
  labeled **UNVERIFIED-as-a-combined-scenario**, but each ingredient (LRU can evict any key regardless of
  logical importance) is standard Redis/Valkey behavior.

**Fly's Upstash Redis integration.** Still live and documented, not deprecated. VERIFIED —
<https://docs.fly.io/upstash/redis/> (fetched 2026-09-30): pay-as-you-go is **"$0.20 per 100k requests"**,
no monthly minimum, "10,000 commands/second max and 10 GB storage"; fixed-price plans run **$10/mo (250MB)
to $400/mo (50GB)** plus per-replica fees ($5–$200). The same page recommends co-locating the database in
your app's region, and warns that "when enabled," eviction "randomly evicts keys — prioritizing those with
TTLs first, then all keys," with writes rejected at the storage limit if eviction is off — the same
counter/cache collision risk as self-hosting, plus you don't control the eviction algorithm's internals.
Persistence and backups are handled by Upstash outside Fly's infrastructure (VERIFIED, same page).
**Cost crossover**: at the scale-target burst of "3–7 new questions/s" (PLAN.md:33) with, say, 5–10 Valkey
commands per question (rate-limit check, reserve, cache get, reconcile, log), sustained traffic would run
$1–12/hour on pay-as-you-go — self-hosting is far cheaper *if* traffic is anything but occasional. For a
short load-test window this is a non-issue either way.

**Alternatives considered**: Fly's own managed Postgres/Redis-alternative offerings were not surfaced as a
drop-in Redis-protocol option beyond the Upstash integration; a self-hosted **KeyDB** or **Dragonfly**
would also work over the same protocol but add an unfamiliar dependency for no clear win over Valkey here
— not researched further (YAGNI per the owner's own coding-style rule).

**Python async client.**
- **redis-py** `8.1.0` (2026-07-30), MIT, 13.6k GitHub stars, pushed 2026-09-30 — VERIFIED PyPI + GitHub
  API. Ships `redis.asyncio`; speaks RESP, so it works unmodified against Valkey (Valkey is a RESP-protocol
  fork of Redis; there is no protocol-level reason redis-py wouldn't work, and this is the client already
  implied by the codebase's existing "plain async client" conventions). I did not find a redis-py changelog
  entry that names Valkey explicitly — **UNVERIFIED** as an explicit compatibility statement, though it
  follows from RESP compatibility and is the industry-standard assumption (Upstash's own docs, AWS
  ElastiCache-for-Valkey docs, etc. all recommend redis-py against Valkey).
- **`valkey` (valkey-py)** — PyPI package `valkey`, version `6.1.1` (2025-08-11), MIT, described as "Python
  client for Valkey forked from redis-py." VERIFIED PyPI. Last release over a year old relative to this
  research date — check its GitHub activity before adopting; redis-py's own release cadence is more active.
- **`valkey-glide`** — PyPI `valkey-glide` `2.5.3` (2026-09-25), Apache-2.0, actively maintained
  (`valkey-io/valkey-glide` pushed 2026-09-30, 792 stars, 510 open issues — a large issue count for a
  younger project, worth a quick skim of open bugs before adopting). VERIFIED PyPI + GitHub API. Rust core
  with Python bindings; Valkey's own official client, "native compatibility with modern Python async
  frameworks like asyncio, anyio, and trio" (VERIFIED, quoted from <https://glide.valkey.io/overview/> via
  search synthesis — not directly fetched, so treat the exact wording as UNVERIFIED even though the general
  claim is corroborated by the PyPI description). A migration guide from redis-py exists
  (`valkey-io/valkey-glide` wiki).
- **Recommendation**: stay on **redis-py `redis.asyncio`** — it is the default the rest of the Python
  ecosystem (including `limits`, below) already assumes, has the deepest maintenance history, and this repo
  has no reason yet to take on GLIDE's Rust-binding build complexity. Revisit only if a specific reliability
  or performance problem shows up that GLIDE specifically fixes (matches the owner's "proven over custom /
  don't churn working code" rule — though there's no existing Valkey client to churn yet, so this is a
  green-field choice, not a migration).

---

## 3. Atomic admission/spend patterns

**Sliding-window rate limiting.** Use **`limits`** (PyPI `limits`, `5.8.0`, 2026-02-05, MIT,
`alisaifee/limits` pushed 2026-08-05, 648 stars, 18 open issues — small, stable, mature). VERIFIED PyPI +
GitHub API. Its own README states: *"The library provides identical APIs for use in sync and async
codebases"* (VERIFIED — read directly from the GitHub-hosted `README.rst` via the GitHub Contents API,
2026-09-30). It implements moving-window, fixed-window, and sliding-window-counter strategies against a
Redis/Valkey/Memcached/MongoDB backend — this replaces hand-rolled window math for the per-IP and
global-daily limiter mentioned in `docs/v2/PLAN.md:33` ("process-local limiter" is today's v1 blocker).

**Correction to the client-library choice in §2: `limits`'s *default* async Redis/Valkey backend is not
redis-py, but redis-py can be selected explicitly.** VERIFIED two ways: (a) `limits` `5.8.0`'s own
`requires_dist` in its PyPI JSON metadata (2026-09-30) shows the **sync** `redis` extra pulling
`redis!=4.5.2,!=4.5.3,<8.0.0,>3` — note the **`<8.0.0` cap**, which would conflict with a future
`redis==8.1.0` pin if this repo ever adds one unpinned alongside `limits[redis]` (there is **no existing
redis pin to conflict with today** — corrected from an earlier draft of this report, §1); the **async**
extras are `async-redis` → `coredis` and `async-valkey` → `valkey>=6` (valkey-py, §2). (b) Directly fetched
`limits.readthedocs.io/en/stable/storage.html` (2026-09-30), quoted: *"redis can be used instead of coredis
by setting `limits.aio.storage.RedisStorage.implementation` to `redispy`"* (added in `limits` 4.2) — so
**coredis is the default async implementation, but redis-py ("redispy") is an explicitly supported,
documented alternative**, not unavailable. This softens but doesn't erase the tradeoff: choosing the
default (coredis) for the rate limiter while using redis-py for the hand-rolled `reserve`/`reconcile` Lua
scripts (below) means **two** Redis-protocol client libraries in the dependency tree; setting
`implementation="redispy"` on `limits`'s storage, or standardizing everything on valkey-py instead, gets it
down to **one**. This is an **owner-relevant tradeoff this research surfaces but does not resolve**
(§13.8).

**Reserve(estimate) → reconcile(actual) with lease TTL.** No mainstream library implements this pattern —
it is inherently domain-specific (estimate token cost before the LLM call, true up after, with a lease so a
crashed request's reservation eventually expires rather than leaking budget forever). This is legitimately
the kind of custom code the owner's own "build on state of the art" rule permits ("custom code is justified
only where it is the actual differentiator... verify no proven package covers it" — confirmed no package
covers it). Design, informed by standard Redis/Valkey Lua-atomicity practice (VERIFIED as a general Redis
pattern via multiple docs/tutorial sources, not a single canonical citation):
- Store spend in **integer micro-dollars** (not `INCRBYFLOAT`, which accumulates floating-point error over
  many small increments — a well-known Redis footgun).
- `reserve(key, estimate_micros, lease_id, ttl_s)`: one Lua script via `register_script` that (a) checks
  `daily_total + estimate <= daily_ceiling`, (b) if so, `INCRBY`s the daily total and writes a lease entry
  `ZADD leases lease_id (now + ttl_s)` scored by expiry, atomically, returning success/failure — single
  round trip, single atomic unit (Redis/Valkey guarantee: "no other command can execute until the script
  completes" — VERIFIED as standard Redis semantics).
- `reconcile(lease_id, actual_micros)`: a second script that removes the lease from the ZSET and adjusts
  `daily_total` by `(actual − estimate)`, positive or negative.
- **Lease expiry sweep**: a periodic job (or lazily, on each `reserve` call) does `ZRANGEBYSCORE leases 0 now`
  and reconciles any lease whose owner never called back (crash/timeout) at its *estimate* (fail toward the
  ceiling, not away from it — a conservative choice worth an explicit owner sign-off, see §13).
- **Global daily ceiling** and **kill switch**: same Lua-script-atomicity approach; the kill switch is
  already a Neo4j-backed policy row (`serve/routes.py` calls `store.kill_switch_on`) — moving it into Valkey
  trades one round trip for another store, and the "fail open or fail closed if Valkey itself is down"
  question is an owner decision (§13), not something research can settle.

---

## 4. FastAPI async SSE, LiteLLM async streaming, workers vs machines

**sse-starlette.** Current `3.5.0` (released 2026-09-28); production pins `3.4.11`, released **2026-09-05**
— i.e. one **minor** version and about three weeks behind, not "one patch behind" (VERIFIED, both dates
read directly from `sse-starlette`'s PyPI JSON `releases` map, 2026-09-30). `sysid/sse-starlette`: 855
stars, 3 open issues, BSD-3-Clause, pushed 2026-09-28 (VERIFIED GitHub API) — small, low-issue-backlog,
actively maintained. Bumping to 3.5.0 before M5 ships is reasonable hygiene but should go through the
repo's normal dependency-bump review, not be treated as a trivial patch. Behavior confirmed directly from the pinned `3.4.11` source (§1):
default ping interval 15 s (`DEFAULT_PING_INTERVAL = 15`), `ping=0` disables pings entirely, a
`send_timeout` parameter exists for per-send backpressure timeouts (not currently passed by this repo's
call sites — `routes.py:277` and `workspace_routes.py:452` only pass `ping=`), disconnect is detected via
the ASGI `http.disconnect` message in `_listen_for_disconnect` which flips `self.active = False` to stop
`_ping` and the generator loop cooperatively. **Sync generators are silently routed through
`iterate_in_threadpool`** (§1) — this is the actionable finding for M5.

**LiteLLM async streaming (`acompletion(..., stream=True)`).** `mock_response` **does** work with
streaming — LiteLLM's own docs (quoted via search, "mock_response streams too... the pieces are cut by
liteLLM, where a real model would send tokens" — **UNVERIFIED exact wording**, I did not fetch
`docs.litellm.ai/docs/completion/mock_requests` directly, only got search-engine synthesis of it) chunks a
fixed mock string into streaming deltas without any network call. **Caveat for load testing**: if
`mock_response` short-circuits before LiteLLM's HTTP client is even constructed (plausible, since the whole
point is "no calling the LLM APIs" — again UNVERIFIED at the implementation-detail level), it cannot
exercise connection-pool or backpressure behavior identical to a real provider call. **Recommendation**: for
the load test's TTFB/backpressure numbers to be trustworthy, prefer a tiny **OpenAI-compatible fake HTTP
server** (FastAPI/Starlette, a dozen lines) that LiteLLM calls over real HTTP with `api_base` pointed at it,
with configurable TTFT and inter-token delay — this exercises the real network path end to end. Use
`mock_response` only for unit tests, not for the load test. I did **not** verify whether closing a LiteLLM
async stream early closes the upstream HTTP/2 connection, nor whether `stream_options={"include_usage": true}`
reliably returns real token counts for reconcile — both are worth a 10-minute empirical check against a
sandboxed key before building the reconcile path on top of them (flagged, not resolved, by this research).

**Workers vs more machines.** Fly's own concurrency model load-balances **across machines**, not across
processes on one machine (§5). Running multiple `uvicorn --workers N` processes on one machine multiplies
the in-process ONNX embedder's memory per worker — VERIFIED directly from the repo's own production
measurement, `docs/v2/M4_PLAN.md:510` (a read-only `flyctl ssh` probe on the live machine): "uvicorn VmRSS
915,772 kB" against "MemAvailable 1,405,288 kB of 2,015,876 kB" on the pre-M4 2 GB machine, and `:516`
states plainly "a subprocess would load a second ~1 GB model" — i.e. a second worker process does not share
the first's loaded embedder, it loads its own copy. **Recommendation**: one uvicorn worker per machine, scale
via **more Fly machines** (§0/§5.2's revised count, not PLAN.md:33's original 2–3 — see below), not via
`--workers`. This also sidesteps needing a process-shared rate limiter/semaphore —
`threading.BoundedSemaphore` (`main.py:236`) is inherently per-process already, and moving admission control
into Valkey (§3) is required *regardless* of whether workers or machines are used, because Fly machines
don't share memory.

**Avoiding thread-per-stream — full scope, and a cheaper stopgap.** Converting `_paid_stream` to `async def`
(§1) only removes the thread-pool dependency if the **entire call chain underneath it** is also async: the
answerer's `yield from` generators (`retrieval/answerer.py:590,673,682`), the LangGraph agent path selected
at `routes.py:303` (when `AGENT_ENABLED`), LiteLLM's `acompletion(..., stream=True)` (§4, already async-
native), and an async Neo4j driver session (the `neo4j` package, pinned `6.3.0`, has supported async
sessions for years — not independently re-verified against 6.3.0's exact API in this pass, but
`langchain-neo4j`'s known lack of async, noted at PLAN.md:31, is exactly why the repo already hand-rolls its
own thin Cypher layer, which can be given an async variant). **Warning**: making only the outer function
`async def` while its body still calls into the sync answerer/Cypher layer directly (no `run_in_threadpool`)
is *worse* than today — a sync, blocking call from inside a true `async def` generator blocks the whole
event loop for every concurrent request on that machine, not just its own thread-pool slot. **Cheaper
stopgap, if the full async rewrite doesn't fit M5's timeline**: raise the anyio worker-thread pool's ceiling
above `hard_limit` plus DB-call headroom — `anyio.to_thread.current_default_thread_limiter().total_tokens =
<N>` at startup (the default is 40, §1) — and let the load test (§7) measure whether that alone closes enough
of the gap between today's 40-request `hard_limit` and the 50–140-stream target, before committing to the
larger async rewrite.

---

## 5. Fly.io platform mechanics

### 5.1 Load balancing, concurrency limits, autostart/autostop

**fly-proxy routing.** VERIFIED, quoted directly from `docs.fly.io/reference/load-balancing/` (fetched
2026-09-30): at or above `soft_limit` but below `hard_limit`, "Traffic will only be sent to this Machine if
all other Machines are also above their `soft_limit`"; above `hard_limit`, "No new traffic will be sent to
the Machine"; when capacity is exceeded, "requests... get queued by Fly Proxy until a Machine is below its
`hard_limit`," and under sustained overload "Fly Proxy might start returning `503 Service Unavailable`
responses for requests that are not able to be routed" — so behavior progresses **queue → 503**, it does not
jump straight to rejection. Today's `fly.toml:41-44` sets `[http_service.concurrency] soft_limit = 20,
hard_limit = 40` with 1 machine (`fly.toml:46,49-50`: `[[vm]] size = "shared-cpu-2x", memory = "4gb"`) —
**that is a hard ceiling of 40 concurrent requests today**, already below PLAN.md:33's low-end target of
"50–140 concurrent streams" at N=1. At N=3 machines with the same per-machine `hard_limit=40`, total capacity
is 120 — still short of the target's upper end (140) and short of the ×1.5 burst multiplier PLAN.md:33
itself calls for on the 50–140 base. **Action item for M5**: either raise `hard_limit` per machine (safe to
do only after the async-SSE fix in §1/§4 — thread-per-stream, not a deliberate capacity choice, is the
likely reason the current limit is set this low) or add a 4th machine; this is a number the load test (§7)
must actually measure, not assume.

**Autostart/autostop and `min_machines_running`.** `fly.toml` already sets `auto_stop_machines = "off"`,
`auto_start_machines = true`, `min_machines_running = 1` — i.e. always-warm by design (the file's own header
comment explains this is deliberate, to avoid a ~10 s cold start). For M5's added machines, the same
always-on choice avoids cold-start latency spikes during a load test or demo, at the cost of paying for idle
capacity — a straightforward cost/latency tradeoff, not something to change silently.

**`fly-replay`**: exists as a mechanism to redirect a specific request to a specific machine (relevant to
the upload-job-registry affinity problem in §1) — UNVERIFIED in this pass (not fetched); confirm its exact
semantics (header name, whether it works across regions, any body-size limit on replay) before relying on
it for M5's upload-job design, or take the simpler path of moving job state into Valkey instead.

### 5.2 Shared-CPU throttling collides with embedder cost — this changes the machine-class recommendation

**This is the single biggest revision this report makes to PLAN.md:33's own "2–3 machines" estimate, and it
comes from the repo's own already-recorded measurements, not new research.** Two facts, both VERIFIED by
reading `docs/v2/M4_PLAN.md` directly (2026-09-30):
- **Shared-CPU baseline is much thinner than the "2x" name suggests.** `M4_PLAN.md:490-491`: "shared vCPUs
  get 5 ms per 80 ms each, pooled per machine (`shared-cpu-2x` = 10 ms / 80 ms = 12.5 % of one core), with a
  per-machine burst balance that starts at 5 s and caps at 500 s." Sustained load beyond that 500 s balance
  throttles the machine down to **12.5 % of one core**, not 2 full vCPUs.
- **A single live query costs roughly a full core-second of embedding, measured on this exact model.**
  `M4_PLAN.md:512-513` (spike results, same q8 ONNX model this app runs): a 500-token passage embed averages
  "~100 tokens/s + ~0.7 s per call" fixed overhead, and directly, "query-embed latency 1.23 s" for one live
  question's query embedding, measured on "a desktop core" (i.e. an unthrottled, dedicated core — likely
  *faster*, not slower, than a throttled shared vCPU).

**The arithmetic that follows is mine, built from those two repo-recorded numbers, not an independently
verified benchmark — but the inputs are solid:** at PLAN.md:33's target burst of 3–7 **new** questions/s,
and assuming §7/§8's load-test design (deliberately varied, cache-busting questions) is representative of
real usage at scale, each question needs its own ~1.2 s query-embed on an unthrottled core — call it
**3.5–8.5 sustained core-equivalents of embedding work alone**, before Neo4j, before LLM overhead (mocked in
the test but not in real traffic), before SSE. A `shared-cpu-2x` machine throttled to 12.5 % of one core
supplies roughly **1/8 of a core** once its 500 s burst balance is spent — nowhere close. Reaching even the
low end of that range on shared CPU would require on the order of **25–30 `shared-cpu-2x` machines** running
in parallel for embedding alone, which is not a serious architecture. The realistic paths are:
1. **`performance-1x` machines** (a dedicated, unshared core) — M4_PLAN.md:500 quotes an **approximate,
   owner-facing figure of "~$60/month in `sin`"** for one (UNVERIFIED as an exact current quote — it was an
   internal estimate for a different decision, not a fresh price lookup, so treat it as directionally right,
   not precise). Covering 3.5–8.5 needed cores would mean **roughly 4–9 `performance-1x` machines**, i.e.
   **≈$240–540/mo** for embedding capacity alone — an order of magnitude above this report's earlier,
   shared-CPU-based cost table.
2. **A real query-embedding cache-hit rate assumption that isn't 0 %.** PLAN.md:46 already designs for "exact
   + query-embedding cache" in Valkey; if repeated or near-duplicate questions hit that cache in real usage
   (unlike this report's own load-test design, which deliberately busts it), the sustained embedding-core
   requirement drops proportionally. **This ratio is not measured anywhere in the repo and is the single
   most important unknown for M5's actual cost** — it belongs in the load test itself (measure the
   cache-hit rate under a *realistic*, not fully-randomized, question mix) before locking a machine-class
   decision.
3. **A separate, dedicated embedding tier** (matching the pattern M4 already used for Docling parsing — "a
   separate on-demand app," PLAN.md:86) instead of embedding inline in the answer-serving process, so the
   answer-serving machines can stay on cheaper shared CPU and only the embedding tier needs performance-class
   cores, sized independently and possibly shared across more answer-serving machines than a 1:1 ratio.

**This also means the load generator needs the same consideration.** If Locust workers run on shared-CPU Fly
machines (§7), sustained high-RPS load generation is itself CPU-bound (gevent event loop, request
construction/parsing) and can throttle the same way, understating real achievable load. Prefer
`performance-1x` (or at minimum `shared-cpu-2x` with headroom well below its own 500 s burst cap) for the
Locust worker machines, not the cheapest available size.

**Net effect on this report's cost table (§0) and on §5.3's pricing**: the "M5 steady" row there assumes
`shared-cpu-2x` throughout and is realistic **only if the embedding-cache hit rate at real (non-test) demo
traffic is high enough that sustained uncached embedding load stays low** — plausible for a low-traffic buyer
demo, **not** demonstrated for the load test's own target-scale numbers. A separate "target-scale, load-test
day" cost line using `performance-1x` for the embedding path is added to §0's cost table and to the owner
decisions in §13.

### 5.3 Pricing — reconciling an owner-vetted sin figure against this research's own (unreliable) fetch

**The owner already has a better, sin-specific, dated number than anything this pass could re-derive from a
generic pricing-page fetch, and it should win.** `docs/v2/M4_PLAN.md:522-523` (an existing, in-repo, VERIFIED
record of a 2026-09-29 owner decision, one day before this research) states, citing `docs.fly.io/about/pricing`
directly: *"Priced for the real region: `sin` carries a 1.269 regional markup... `shared-cpu-1x` 2 GB
~$17.54 -> `shared-cpu-2x` 4 GB ~$35.09 per 30 days online, +$17.55."* That means **the current production
API machine (`shared-cpu-2x`/4GB, sin) costs ≈$35.09/30 days**, not the ≈$13.48/mo this research's own
WebFetch of `docs.fly.io/about/pricing/` computed.

That discrepancy is a **flaw in this pass's own method**, not a contradiction to average away: my WebFetch
call (2026-09-30) returned per-second rates ($0.00000506/sec for `shared-cpu-2x`/4GB) that it explicitly
attributed to the **iad** (Ashburn) region and said it "could not confirm a sin-specific compute rate" —
i.e. it silently used the pricing calculator's regional default rather than sin, and even applying the
owner-cited 1.269 sin markup to that iad figure only gets to ≈$17/mo, still roughly half the owner's vetted
$35.09. I could not fully explain the remaining gap in the time available (possible causes, unconfirmed:
Fly changed its calculator/pricing model between the owner's 2026-09-29 lookup and this pass's 2026-09-30
fetch; the calculator applies additional line items — e.g. a 2026 IP/bandwidth reservation charge one search
result alluded to — that aren't broken out per-machine; or my fetch simply mis-selected a plan). **Given
that ambiguity, this report uses the owner's $35.09/30-days figure as the trusted sin baseline for
`shared-cpu-2x`/4GB, and flags everything derived from my own fetch as an estimate, not a verified number**:

| Machine | Memory | Region | ≈ $/30 days | Confidence |
|---|---|---|---|---|
| shared-cpu-1x | 2 GB | sin | **$17.54** | VERIFIED — `docs/v2/M4_PLAN.md:523`, owner-vetted 2026-09-29 |
| shared-cpu-2x | 4 GB | sin | **$35.09** | VERIFIED — same source |
| shared-cpu-1x | 1 GB | sin | **≈$9–14** | **UNVERIFIED estimate** — no owner-vetted sin figure exists for this exact size; interpolated below the $17.54 (2 GB) figure by the same rough per-GB increment my own (unreliable) iad fetch showed, then scaled by the 1.269 markup. Treat as a placeholder pending a real quote for the Neo4j machine's own size. |

The **iad-only per-second rates my own fetch returned** ($0.00000075/sec for 1x/1GB, $0.00000411/sec for
1x/4GB, $0.00000150/sec for 2x/1GB, $0.00000506/sec for 2x/4GB, from `docs.fly.io/about/pricing/`,
2026-09-30) are recorded here for completeness but **should not be used for sin cost planning** given the
mismatch above.

Fly's bandwidth pricing **is** region-group-based and was directly quoted by the same fetch (treat this part
as VERIFIED independent of the compute-pricing confusion above):
- North America & Europe egress: **$0.02/GB**; private cross-region: **$0.006/GB**.
- Asia-Pacific & Oceania (sin falls here) egress: **$0.04/GB**; private cross-region: **$0.015/GB**.
(VERIFIED, quoted from the same fetch.)

**Volumes**: **$0.15/GB-month**, pro-rated hourly, **charged even when the attached machine is stopped**;
volume **snapshots** are billed separately since Jan 1 2026 at **$0.08/GB-month with the first 10 GB free**
(UNVERIFIED — search-engine synthesis of Fly's pricing docs/community posts, not a direct fetch — but the
two numbers were consistent across independent sources).

### 5.4 Private networking

`.internal` DNS resolves to the 6PN (WireGuard mesh, IPv6) addresses of an app's running machines directly —
**machine-to-machine**, and only reaches *started* machines. **Flycast** instead routes through fly-proxy,
so it works with autostart/autostop private apps and load-balances geographically. VERIFIED via search
synthesis of `fly.io/docs/networking/private-networking/` and `fly.io/docs/networking/flycast/` (exact
wording UNVERIFIED-paraphrase). **Recommendation**: reach Valkey over plain `.internal` (both API and Valkey
are always-on per `min_machines_running=1`/similar, so Flycast's autostart benefit doesn't apply), which is
also the simpler, lower-latency path for a chatty admission-control round trip on every question.

---

## 6. Cloudflare in front of Fly

This section is presented as **two options** rather than a recommendation, because it turns on an owner
decision (does the project have or want a custom domain on Cloudflare DNS?) that this research cannot make
— see §13.

**Does Cloudflare require a zone on Cloudflare DNS?** For the *proxy + WAF + Transform Rules* feature set —
yes. Transform Rules (needed to inject an origin-auth shared-secret header before requests reach
`*.fly.dev`) are a **zone-level** product; VERIFIED that the feature exists and does what's needed (fetched
`developers.cloudflare.com/rules/transform/request-header-modification/`, 2026-09-30: *"Set the value of an
HTTP request header to a literal string value, overwriting its previous value or adding a new header to the
request"*), but the fetch could not confirm which plan tier it's available on — Transform Rules are widely
documented elsewhere as available on Cloudflare's **Free** zone plan for a limited rule count, but I did not
independently re-verify that limit here (**UNVERIFIED** plan-tier detail).

**Option 1 — a zone on Cloudflare DNS, orange-clouded, in front of the `fly.dev` origin.** Gets Transform
Rules (origin-auth header), WAF, Turnstile (already used, and origin-agnostic — Turnstile just needs a
sitekey, no DNS requirement), and Cloudflare Access on the API's own hostname. Requires owning/pointing a
domain at Cloudflare — an owner decision, not a technical blocker.

**Option 2 — a Worker on the free `*.workers.dev` subdomain, `fetch()`-proxying to `*.fly.dev`.** No custom
domain needed. VERIFIED limit that matters: Cloudflare Workers **Free** plan has **"a daily request limit
of 100,000 requests, resetting at midnight UTC"** (fetched `developers.cloudflare.com/workers/platform/limits/`,
2026-09-30, quoted directly). At the scale target's steady burst of 3–7 **new questions**/s, sustained
question-POST traffic alone would consume 100,000 requests in roughly **4–9 hours** (100,000 ÷ 3–7/s ÷
3600 s/hr ≈ 4.0–9.3 hours) — a short load-test *run* (well under 4 hours) could fit inside the free daily
cap, but a full-day soak test, or the same cap shared with the demo's own static-asset and evidence-lookup
traffic (not counted in this arithmetic), would not. The free Worker path is comfortably fine for a **buyer
demo's actual traffic** (low, bursty, nowhere near target scale) but is **only conditionally sufficient for
the load test**, depending on run length — size the test window against this 4–9-hour budget, bypass the
Worker during the load test (hit `*.fly.dev` directly, at the cost of not testing the CF hop), or upgrade to
a paid Workers plan for that day.

**The CF-Connecting-IP trust rule, stated explicitly.** Once anything sits in front of Fly, the app's own
`*.fly.dev` hostname typically **stays publicly reachable** in parallel (Fly doesn't firewall it off just
because a Worker or a zone also points at it) — so a client that goes straight to `*.fly.dev` can forge
`CF-Connecting-IP` (or bypass Cloudflare, hence Turnstile and the WAF, entirely) unless the origin refuses to
trust that header on its own. **These must be two mutually exclusive deployment modes selected by config,
not two rules layered on the same running app** — layering them (trust the CF header *and* keep the
Fly-only fallback live on the same deployment) reopens exactly the bypass the CF-mode rule exists to close,
since a real attacker would simply use the still-open fallback path instead of forging the CF header:
1. **"Cloudflare mode" (the production app, once Cloudflare is in front)**: trust `CF-Connecting-IP` (or
   whichever header carries the client IP) **only** alongside a second, secret, origin-auth header, set by a
   Transform Rule (Option 1) or by Worker code (Option 2) and checked with a constant-time compare (the same
   `hmac.compare_digest`/`secrets.compare_digest` pattern already used at `routes.py:417` and
   `uploads/repo.py:178`). **Reject the request outright if that origin-auth header is missing or wrong**,
   before any IP-based rate-limiting logic runs — and **stop trusting `fly-client-ip` for rate-limiting
   purposes in this mode**, since a direct `*.fly.dev` request that skips Cloudflare must not be treated as
   a legitimate alternate path once Cloudflare mode is the deployment's actual front door.
2. **"Direct-Fly mode" (today's deployment, and any environment without Cloudflare in front)**: keep the
   existing `CLIENT_IP_HEADER=fly-client-ip` trust (`guard.py:59`) exactly as it is now. This mode has no
   origin-auth header check because there is no second hop to authenticate.
A single running deployment should be in one mode or the other, driven by a config flag, not both at once.

**Consequence for the load test's IP-diversity plan (§8, corrected below): `fly-client-ip` cannot be
client-supplied against the real Fly edge.** `fly.toml`'s own comment on that header (quoted at `config.py:70`:
"set by a TRUSTED proxy... never trust it elsewhere") means Fly's edge **sets this header itself from the
real TCP connection**, overwriting anything a client sends — so Locust cannot simply "send distinct
synthetic client IPs via the trusted header" against `*.fly.dev` the way §8 originally suggested; that only
works if the header Locust controls is one the **staging** app has been configured to trust instead (see the
correction in §8).

**SSE through the Cloudflare proxy.** Confirmed, multiple independent Cloudflare Community threads
(UNVERIFIED-paraphrase, consistent across sources, not a single canonical doc page): **Free/Pro plans idle-
time out an unresponsive connection at 100 s**, and the proxy can **buffer** a response until ~100 KB
accumulates unless streaming is signaled correctly. Mitigations, all already partially present in this repo:
send a `X-Accel-Buffering: no` header (sse-starlette already sets this on every response —
`sse.py`'s header block, confirmed in §1's source read: `_headers["X-Accel-Buffering"] = "no"`) and keep a
heartbeat well under 100 s — this repo's `ping=15` and `SSE_POLL_TIMEOUT_S=10` (§1) are already safely
inside that window, so **no change needed here**, only confirmation once Cloudflare actually sits in front.

**Cost**: Cloudflare's proxy/CDN/WAF, Turnstile, and a Worker on the free plan are all **$0** at this scale
(Turnstile: VERIFIED "free with no request cap" per Cloudflare's own blog, quoted via search synthesis,
UNVERIFIED exact wording); the only line item that can turn non-zero is Workers if the load test runs
through it and exceeds the daily cap, or a zone if the owner wants a paid Cloudflare plan for reasons beyond
this stack (e.g. more WAF rules).

---

## 7. Load testing

**Locust.** Current `2.46.6` (2026-09-17), MIT, `locustio/locust` pushed 2026-09-26, 28.2k stars, only 5
open issues — extremely healthy. VERIFIED PyPI + GitHub API. `locust-plugins` `5.0.3` (2026-06-12,
Apache-2.0) exists for extra reporting/transport helpers. **Distributed mode**: `--master` / `--worker`
(optionally `--master-host` for workers on other machines) — UNVERIFIED exact wording (search-engine
synthesis of Locust's own distributed-load docs, not a direct fetch), but Locust's master/worker CLI flags
are long-stable and
consistently described). **Consuming SSE**: Locust's built-in `HttpUser`/`FastHttpUser` are request/response
oriented; there is no first-class SSE consumer in Locust core (not found in the docs search) — the
standard approach is a **custom Locust `User`** whose task opens the SSE endpoint with `httpx`'s streaming
client (or `requests` with `stream=True`) inside `self.client`'s timing context, manually recording a
custom Locust event for **TTFB of the first SSE event** (time from request start to the first
`data:`/`event:` line) and again for stream completion, using Locust's `environment.events.request.fire(...)`
API to report both as separate named "requests" in Locust's stats. `FastHttpUser` (gevent-based) is lower
overhead per VU and was the tool's own summary claim of "possibly over ten thousand requests per second" for
simple payloads (UNVERIFIED exact wording) — for 1,000 VUs holding long-lived SSE connections, VU-count
matters far more than requests/sec, so either `HttpUser` or `FastHttpUser` should work; `FastHttpUser` uses
less load-generator CPU/RAM per VU, which matters more as VU count grows.

**Mock LLM options.**
1. **LiteLLM `mock_response`** — fast, zero network, but (per §4) may not exercise the real HTTP/connection-
   pool path, so it risks understating real backpressure/latency characteristics.
2. **A small OpenAI-compatible fake server** (FastAPI + `sse-starlette` — the same libraries already in the
   stack) with configurable TTFT and per-token delay, run as its own Fly machine or alongside the load
   generator — the more faithful choice for TTFB measurement, at the cost of writing ~50–100 lines of code.
   **Recommendation**: build this; it is small, reuses libraries already vetted in this report, and directly
   answers "what does a first SSE event look like under real network latency."

**Where to run 1,000 VUs.** A single Windows workstation behind a home/office IP is very likely to become
the actual bottleneck (NIC, OS socket limits, single public IP for outbound TCP, background load on the
same machine) well before the Fly app does — this is a general networking-scale observation, not something
specific I fetched a source for. **Recommendation**: temporary Fly machines in **sin** (or near it) running
distributed Locust (master + N workers), billed per-second and torn down right after the run — using §5.3's
verified per-second compute rate, even a dozen `shared-cpu-2x` load-generator machines for a couple of hours
costs low single-digit dollars. Whether Locust `--processes` (multi-core auto-worker spawning) exists in
`2.46.6` was not directly confirmed in this pass — **UNVERIFIED**, check `locust --help` output before
relying on it; if absent, spawn multiple `--worker` processes manually.

---

## 8. Load-test methodology constraints (do not point this at production)

Every one of these will corrupt or block a naive Locust run against the live `semigraph` app, per the exact
gate order read from `routes.py:270-277` in §1:
- The **per-IP rate limiter** (`routes.py:275`) will 429 a single-machine Locust load generator almost
  immediately, since all VUs share one or a handful of source IPs.
- **Turnstile** (`routes.py:272`) will reject unsolved/absent tokens — Cloudflare's own **published dummy
  keys** exist for exactly this. **VERIFIED, directly fetched and quoted from
  `developers.cloudflare.com/turnstile/troubleshooting/testing/` (2026-09-30)**: test sitekey
  `1x00000000000000000000AA` "always passes" (visible widget; a matching invisible-widget variant,
  `1x00000000000000000000BB`, also always passes), test secret key
  `1x0000000000000000000000000000000AA` "always passes validation"; test sitekeys generate a dummy token
  `XXXX.DUMMY.TOKEN.XXXX`, and "production secret keys will reject the dummy token" (and vice versa) — so
  this requires a **separate staging configuration**, not a flag flip on the live secret. (The same page
  also lists always-fail and "token already spent" variants, useful for negative-path tests but not needed
  for a capacity load test.)
- The **global daily ceiling** (`routes.py:270`) will trip and 503 the whole app for real users mid-test.
- **`store.log_query`** (`routes.py:264`) will pollute the production ledger/analytics with synthetic load.
- The **answer cache** (`routes.py:261-266`) will short-circuit repeated identical questions after the
  first — a naive fixed question set will look like it scales infinitely because most "questions" are
  served from cache, not from a live LLM call.

**Recommended test design**: a **throwaway staging Fly app** with a copy of the current graph dump, its own
Turnstile dummy-key config, its own (or bypassed) daily ceiling, a **varied or cache-key-randomized**
question set, and the test hitting the **public edge** (through Cloudflare if that's in the final
architecture, or directly at `*.fly.dev` otherwise) rather than `.internal`, so fly-proxy's own concurrency
limits (§5.1) are actually in the loop.

**IP diversity, corrected (§6 has the full reasoning).** Locust **cannot** inject distinct `fly-client-ip`
values against the real Fly edge — Fly's proxy sets that header itself from the actual connection
(`config.py:70`'s own comment: "set by a TRUSTED proxy... never trust it elsewhere"), overwriting whatever a
client sends. Two honest options for staging, in order of preference: (a) configure the **staging app only**
with a different, Locust-controlled `CLIENT_IP_HEADER` (e.g. `X-Test-Client-IP`) so the per-IP limiter code
path is genuinely exercised with varied IPs — safe only because staging is throwaway and never carries real
traffic, and this must never be the production app's configuration (§6); or (b) accept that all load-test
traffic shares one or a few real source IPs and instead **raise the per-IP rate limit on staging** for the
test's duration, accepting that the per-IP-limiter code path itself goes untested by this particular run.

Size the **think-time** distribution around PLAN.md:33's 120–300 s assumption and run long enough
to cover several full think-time cycles — 1,000 VUs with **no** think time is a materially different (much
harder) test than the Little's-law-derived 50–140 concurrent-stream target, and conflating the two would
produce a misleading capacity number. Keep the **1 GB Neo4j machine and the in-process ONNX embedder in the
loop** (only the LLM should be mocked) — PLAN.md:33's own framing ("LLM capacity... fits standard paid
tiers" while everything else is the actual open question) suggests Neo4j and the embedder, not the LLM, are
the more likely first bottlenecks at this scale.

---

## 9. Cloudflare Access free tier (invite gate)

**Partially VERIFIED, mixed with UNVERIFIED search synthesis.** The blocking mechanism itself is VERIFIED,
directly fetched and quoted from Cloudflare's own `developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/`
(2026-09-30): *"Once the total amount of seats in the subscription has been consumed, additional users who
attempt to log in are blocked."* — confirming there's no automatic upgrade/billing surprise, just a hard
stop. The specific **free-tier count of 50 seats**, however, is **UNVERIFIED** in this pass — I could not
land a direct fetch that quoted that exact number (two attempts against Cloudflare's pricing/plans pages
returned generic navigation content, not the number itself); it is widely and consistently reported across
independent secondary sources, but treat "50" as reported-consensus, not independently confirmed here.
**One-time PIN (OTP)** login is a built-in identity method (UNVERIFIED exact wording, search-engine
synthesis) — Access emails a PIN to an approved address, no external IdP required, and can be offered
alongside a real IdP. This is a good fit for **inviting a specific buyer** (or a short list of named
reviewers) to a gated demo — it is explicitly **not** a mechanism for gating "1,000 concurrent users," and
should be labeled as such in any M5 plan so it isn't confused with the load-test's actual audience.

**Interaction with an API on another origin.** Access issues a `CF_Authorization` cookie (browser flow) and
sends a `Cf-Access-Jwt-Assertion` header to the **protected origin itself** for server-side validation.
Interesting nuance found in this research: Cloudflare Access supports **"multiple domains in a single
self-hosted application"**, and once a user authenticates to one domain in that application, Access
**"automatically issues a `CF_Authorization` cookie when they go to another domain in the same Access
application"** (UNVERIFIED exact wording — search-engine synthesis of Cloudflare's own Access docs, not a
direct fetch) —
meaning if both the demo frontend's hostname *and* the API's hostname are Cloudflare-managed and added to
the **same** Access application, a single login covers both without the frontend needing to forward a raw
JWT itself. This still requires the **API's own hostname to be on Cloudflare** (reinforcing the Option-1-vs-
Option-2 fork in §6 — a bare `*.fly.dev` origin, or an API reached only through a `*.workers.dev` Worker
with no Access application attached to it, would need its own separate auth check, e.g. validating a
service token or the forwarded header manually).

---

## 10. FastMCP / official MCP Python SDK

**Official SDK** — PyPI `mcp` **`2.2.0`** (2026-09-07), MIT, `modelcontextprotocol/python-sdk` pushed
2026-09-29, 24.4k stars, 457 open issues (large but proportionate to a widely-adopted, fast-moving SDK).
VERIFIED PyPI + GitHub API.

**FastMCP** — PyPI `fastmcp` **`4.0.10`** (2026-09-25), Apache-2.0, `jlowin/fastmcp` pushed 2026-09-30,
27.9k stars, 424 open issues. VERIFIED PyPI + GitHub API. (Note: FastMCP is a separate, higher-level
project from the "FastMCP" that was folded into the official SDK's own decorator-based API in earlier MCP
history — this is the standalone `jlowin/fastmcp` project, actively maintained independently.)

**Spec version.** The current Model Context Protocol specification revision is **`2026-07-28`** (VERIFIED,
`blog.modelcontextprotocol.io/posts/2026-07-28/` and the spec's own `docs/specification/2026-07-28/`
path exist and were returned by search — UNVERIFIED exact prose, but the version identifier itself is
corroborated by multiple independent MCP-project URLs). Per search synthesis of that revision's changelog
(**UNVERIFIED** exact wording, not independently confirmed against the spec text itself): Streamable HTTP
now expects `Mcp-Method`/`Mcp-Name` headers for routing/metering without parsing JSON bodies, list-type
responses carry `ttlMs`/`cacheScope` hints, and the spec is described as formalizing a "stateless protocol
core."

**Dependency direction, corrected.** `fastmcp-slim` (the implementation package behind PyPI `fastmcp`
`4.0.10`) lists `mcp<3.0.0,>=2.0.0` as a hard dependency (VERIFIED, read directly from its PyPI JSON
`requires_dist`, 2026-09-30) — **FastMCP is built on top of the official `mcp` SDK, not the reverse.** The
official SDK's own README (VERIFIED, read directly via the GitHub Contents API, 2026-09-30) states it can
*"Speak every standard transport: stdio, Streamable HTTP, and SSE"* and shows `uv run mcp run server.py
--transport streamable-http` as the way to run one. I searched that same README for "stateless",
"json_response" and "TokenVerifier" (the specific terms the stateless-mode and auth claims would need) and
**did not find them** — so **"stateless mode" and the SDK's auth primitives are UNVERIFIED in this pass**;
they may exist in the SDK's fuller docs (`py.sdk.modelcontextprotocol.io`, not fetched here) but I cannot
confirm the API surface from the README alone. Downgrade "stateless mode" in the recommendation below from
a confirmed feature to a thing to check before committing to it.

**Recommendation**: build the read-only MCP server on the **official `mcp` SDK**'s streamable-HTTP
transport, not FastMCP — for a small, fixed, read-only tool set, building on the lower-level package
FastMCP itself depends on avoids one dependency layer, and FastMCP's extra convenience (prompts, resources
templating, client testing helpers) is more valuable for a larger or more dynamic server than this one.
**Before locking this in, confirm directly against the SDK's own docs** (not just its top-level README)
that a stateless/no-session-affinity mode actually exists for streamable HTTP, since that claim is
UNVERIFIED here and materially affects whether the MCP server needs sticky routing across Fly machines
(unlike the upload-job-registry problem in §1, a read-only MCP server plausibly needs no session affinity
at all — but "plausibly" is not "confirmed"). **Auth**: not deeply researched in this pass beyond the
2026-07-28 spec's UNVERIFIED "authorization hardening" claim above — worth a dedicated look before M5 locks
the MCP auth model, since a public MCP server that's read-only over a public corpus plausibly needs no auth
at all (simpler and safer than half-implementing OAuth for a server with nothing sensitive behind it) — an
explicit owner decision (§13).

---

## 11. API keys: generation, storage, rotation, per-key limits

**Storage.** For a **high-entropy, machine-generated** secret (not a human password), OWASP's own guidance
and current practitioner consensus (VERIFIED as a coherent, consistent position across multiple sources
including the OWASP Cheat Sheet Series pages themselves, though I read them via search synthesis rather
than a direct fetch — UNVERIFIED exact OWASP wording) is: **do not use bcrypt/Argon2/scrypt for API keys** —
those algorithms' deliberate slowness defends against *offline guessing of low-entropy human passwords*, and
buys nothing when the secret already has ≥128–256 bits of entropy, while adding real CPU cost per request
validated. Instead: generate the key with a CSPRNG, hash it with **SHA-256** (or better, **HMAC-SHA-256
with a server-held pepper**, which the OWASP Cryptographic Storage guidance apparently prefers over bare
SHA-256 — "a MAC such as HMAC-SHA256... is a better choice than SHA-256... alone", UNVERIFIED exact wording),
store only the digest, and compare in constant time.

**This is not merely "matching PLAN.md:85's design note"** (that line lives under the "Superseded by
`docs/v2/M4_PLAN.md`" banner at `docs/v2/PLAN.md:81` and documents an earlier plan, not current code) —
**it's already shipped, live code**, VERIFIED by reading `src/semigraph/uploads/repo.py` directly: the
docstring at `:12` states *"tokens: only `sha256(token)` is stored;`authenticate` compares with
`hmac.compare_digest`"*; `:163` does `token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest()` on
creation; `:174-178` does the same hash on the incoming token and compares with
`hmac.compare_digest(given_hash, stored_hash)`; and `:45-46` even pre-computes a `_DUMMY_TOKEN_HASH` so
looking up a nonexistent workspace id costs the same `hmac.compare_digest` call as a real wrong-token check
— a timing-attack defense worth carrying into the API-key design too. An M5 API-key scheme should be a
direct extension of this existing, already-shipped pattern, not a new design. Practical additions worth
adopting: store a short **unhashed prefix** of the key (e.g. first 8 chars) alongside the hash for O(1) DB
lookup without a full-table hash comparison, and a **per-key salt** for defense in depth if keys might ever
be generated with lower entropy than intended (belt-and-suspenders, not strictly required at 128+ bits).

**Rotation & per-key limits.** Not deeply researched beyond the general pattern (issue a new key, mark the
old one's row `revoked_at`, keep it hash-comparable for a grace window if needed, tie usage/ledger rows to
the key's id rather than its raw value) — this is standard practice, not something requiring a citation, and
follows directly from the workspace-token precedent already shipped in `src/semigraph/uploads/repo.py`
(`workspace_id` + TTL sweeper, per `docs/v2/M4_PLAN.md`'s current design).

---

## 12. Neo4j Graph Data Science (GDS) Community

**Versioning moved to calendar versioning tracking Neo4j itself.** VERIFIED, `gh api
repos/neo4j/graph-data-science/releases`: recent tags are `2026.09.0` (2026-09-21), `2026.08.1`
(2026-09-11), `2026.07.0` (2026-08-05), `2026.06.0`, `2026.05.0` — one release per month, matching Neo4j's
own release cadence. **VERIFIED, fetched `neo4j.com/docs/graph-data-science/current/installation/supported-neo4j-versions/`
(2026-09-30)**: the compatibility table includes the row **`"2026.07.0" | "2026.07"`** — i.e. **GDS 2026.07.0
is the release built for Neo4j 2026.07.x**, which is a direct match for the task's stated production pin of
"Neo4j Community 2026.07.1." Rows for `2026.09.0`, `2026.08.1`, `2026.06.0`, `2026.05.0`, `2026.04.0`, and
`2026.03.0` are also present, each paired with its corresponding Neo4j month.

**License.** Two layers, VERIFIED by reading the repo's own `LICENSE.txt` file directly via the GitHub
Contents API (2026-09-30) — I also attempted `NOTICE.txt` at the same path but the API call returned no
usable content (wrong path guess, most likely, given the repo's `LICENSE.txt` itself points to a separate
"NOTICE.txt file" and "LICENSES.txt for full license texts" that I did not successfully locate — this
sub-claim about the license split is corroborated by `LICENSE.txt`'s own text and by web search, not by a
second file I could independently open):
- The **officially distributed GDS plugin** ("Neo4j GDS is available to download and use under the
  constraints of its license" — this is Neo4j's own commercial-but-free-for-Community-use license, distinct
  from an OSI license) — by default it **runs as GDS Community** and only unlocks Enterprise algorithms
  with a separate Enterprise license file (UNVERIFIED exact wording — search-engine synthesis of the
  official docs, not a direct fetch — but corroborated by the repo's own README statement that the source
  can alternatively be
  self-built as **"OpenGDS," licensed GPLv3**, which only makes sense if the *official* binary distribution
  is under different, more restrictive terms).
- The repo's top-level `LICENSE` file (fetched directly) states the software is **GPLv3** when self-assembled
  from source ("OpenGDS"), which is the path this task's constraint ("running offline for centrality
  precomputation" on an internal/offline pipeline, not the production API) most naturally fits if avoiding
  the official binary's non-OSI terms matters; if it doesn't, the **official pre-built plugin** is simpler
  to install and is free for Community-tier algorithms.

**Recommendation**: run GDS **offline**, on the **pipeline/build Neo4j instance** (not the 1 GB production
Community machine), matching GDS 2026.07.0 to production Neo4j 2026.07.1, compute the needed centrality
measures there, and **ship the results as ordinary graph properties in the versioned dump** the rest of the
architecture already produces (`docs/v2/PLAN.md`'s "versioned full rebuild → closure → dump" pipeline stage,
line 50). This avoids ever loading the GDS plugin (a JVM-heap-hungry, Java-based library) into the
production 1 GB Neo4j Community machine, sidesteps any licensing ambiguity about running GDS **in
production**, and requires no new production dependency at all — the production Neo4j only ever reads plain
graph properties GDS wrote offline.

---

## 13. Owner decisions this research surfaced but cannot resolve

1. **Custom domain?** Whether Cloudflare sits in front via a real DNS zone (Transform Rules, Access on the
   API's own hostname, no Workers daily-request cap) or via a free `*.workers.dev` Worker (no domain needed,
   but a 100k req/day ceiling that the load test itself would likely exceed, §6).
2. **Fail-open or fail-closed** when Valkey is unreachable — should the kill switch, rate limiter, and spend
   ledger default to blocking all paid answers (safe, but an outage becomes a full outage) or to allowing
   them through unmetered (available, but the daily ceiling stops protecting spend) (§3).
3. **Self-hosted Valkey vs Fly's Upstash integration** — roughly cost-neutral at demo scale (§2, corrected
   from an earlier draft's "cheaper-but-more-ops" framing); the real tradeoff is eviction-policy control and
   per-command billing risk under load-test-scale traffic (self-hosted) vs. zero ops (Upstash) — the owner's
   call.
4. **Machine class and embedding-cache hit rate at target scale (§5.2, new and load-bearing).** This
   report's cost table shows roughly a **6× jump** (from ≈$88–98/mo to ≈$330–630/mo) between a "demo
   traffic" assumption and a "target-scale, low-cache-hit" assumption for query-embedding compute alone,
   derived from the repo's own shared-CPU throttling and embedding-latency measurements. Whether M5 needs
   `performance-1x` machines (or a separate dedicated embedding tier) instead of more `shared-cpu-2x`
   machines depends entirely on a real-world embedding-cache hit rate this report could not measure — this
   should be the **first thing the load test measures**, before any other capacity number is trusted.
5. **One Valkey instance or two** — combining the TTL'd answer/embedding cache with the spend-ledger/lease
   counters risks an LRU eviction quietly resetting a counter (§2); splitting them is simple insurance but
   is one more moving part.
6. **Reserve-lease timeout behavior** — when a lease expires without a `reconcile` call (crashed request),
   should the ledger charge the *estimate* (conservative, protects the ceiling, may overcharge) or nothing
   (undercounts real spend) (§3)?
7. **Does the public MCP server need auth at all**, given it's explicitly read-only over a public corpus
   (§10)?
8. **One Redis-protocol client library or two** — `limits`'s default async backend is `coredis`, with
   redis-py available via an explicit `implementation="redispy"` setting, and `valkey-py` available through
   a separate `async-valkey` extra (§2, §3); picking one library for both the rate limiter and the
   hand-rolled reserve/reconcile Lua scripts avoids running two.
9. **Get real sin quotes** for the Neo4j-sized machine (`shared-cpu-1x`/1GB), the Valkey machine, the
   `performance-1x` machine (§5.2's figure is an approximate 2026-09-29 owner estimate for a different
   decision, not a fresh quote), and the load-generator machines, before M5's budget is finalized — none of
   these has a directly-quoted, dated sin price the way the current API machine does (§5.3). Also confirm
   whether Fly's volume pricing itself carries the same sin regional markup as compute (UNVERIFIED, §5.3).

---

## Sources consulted (primary, access date 2026-09-30 unless noted)

- Repo (read-only): `fly.toml`; `deploy/neo4j/fly.toml`; `deploy/requirements-serve.txt`; `docs/v2/PLAN.md`;
  `docs/v2/M4_PLAN.md` (esp. lines 490-523, the owner-vetted sin pricing decision); `docs/RUNBOOK.md`;
  `src/semigraph/config.py`; `src/semigraph/serve/main.py`; `src/semigraph/serve/routes.py`;
  `src/semigraph/serve/guard.py`; `src/semigraph/serve/workspace_routes.py`; `src/semigraph/uploads/jobs.py`;
  `src/semigraph/uploads/repo.py` (live token-hashing implementation); `src/semigraph/serve/monitor.py`;
  `.venv/Lib/site-packages/sse_starlette/sse.py` (v3.4.11, matches production pin);
  `.venv/Lib/site-packages/starlette/concurrency.py` (v1.6.0, matches production pin);
  `.venv/Lib/site-packages/anyio/_backends/_asyncio.py` (v4.14.1, local dev version — production pins 4.15.1).
- PyPI JSON API (`pypi.org/pypi/<pkg>/json`, including each package's `requires_dist` metadata) for: fastapi,
  sse-starlette, starlette, litellm, uvicorn, anyio, redis, valkey-glide, limits, locust, mcp, fastmcp,
  fastmcp-slim, valkey, hiredis, locust-plugins, uvicorn-worker, gunicorn.
- GitHub REST API (`gh api repos/<org>/<repo>`, `.../releases`, and `.../readme`) for: tiangolo/fastapi,
  sysid/sse-starlette, encode/starlette, encode/uvicorn, BerriAI/litellm (incl. `LICENSE` and
  `enterprise/LICENSE.md` contents), redis/redis-py, valkey-io/valkey-glide, valkey-io/valkey,
  valkey-io/valkey-py, alisaifee/limits (incl. `README.rst`), locustio/locust,
  modelcontextprotocol/python-sdk (incl. top-level `README.md`), jlowin/fastmcp, neo4j/graph-data-science
  (incl. `LICENSE.txt`; a `NOTICE.txt` fetch attempt returned no usable content — see §12).
- OSV API (`api.osv.dev/v1/query`) for the five **actually pinned** production versions (litellm,
  sse-starlette, starlette, fastapi, anyio) plus two not-yet-adopted, latest-PyPI versions checked as a
  forward look for this report's own recommendations (redis, locust) — zero advisories found for all seven.
- WebFetch (directly quoted): `docs.fly.io/about/pricing/`; `docs.fly.io/upstash/redis/`;
  `docs.fly.io/reference/load-balancing/`; `neo4j.com/docs/graph-data-science/current/installation/supported-neo4j-versions/`;
  `developers.cloudflare.com/workers/platform/limits/`;
  `developers.cloudflare.com/rules/transform/request-header-modification/`;
  `developers.cloudflare.com/turnstile/troubleshooting/testing/`;
  `developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/`;
  `limits.readthedocs.io/en/stable/storage.html`.
- WebSearch synthesis (used where a direct fetch wasn't taken, or returned unusable content — flagged
  individually above as UNVERIFIED-exact-wording): Fly concurrency/load-balancing docs (cross-checked
  against the direct fetch above), Fly private-networking/Flycast docs, Fly volumes pricing, Cloudflare
  Access pricing/free-tier exact seat count and multi-domain Access-application behavior, Cloudflare
  SSE-proxy buffering/100s timeout behavior, LiteLLM `mock_response` streaming docs, Locust distributed-mode
  and `FastHttpUser` docs,
  redis-py/Valkey-GLIDE compatibility and comparisons, OWASP API-key hashing guidance, the March 2026
  litellm 1.82.7/1.82.8 supply-chain compromise (corroborated by Bitsight, LiteLLM's own blog, Datadog
  Security Labs, and Trend Micro coverage), MCP 2026-07-28 specification changelog.
