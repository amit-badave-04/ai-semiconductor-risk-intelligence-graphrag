# Operations runbook — semigraph on Fly.io

Two Fly apps in the `personal` org, region `sin`:

| App | What | Machine | State when "online" | Cost when "offline" |
|---|---|---|---|---|
| `semigraph` | FastAPI + ONNX embedder (public: https://semigraph.fly.dev) | shared-cpu-2x, 4 GB (`fly.toml [[vm]]`), always-warm while online | 1 machine started | scaled to 0 → rootfs only (~$0.20/mo) |
| `semigraph-neo4j` | Neo4j Community 2026.07 + 3 GB volume (private, `semigraph-neo4j.internal:7687`) | shared-cpu-1x, 1 GB + 1 GB swap | 1 machine started | stopped → rootfs + volume (~$0.55/mo) |

Everything else persists across STOP/START: the volume (graph + service ledger/cache), Fly
secrets, the baked graph seed in the DB image. All commands below run from the repo root in
PowerShell; `flyctl` must be logged in (`flyctl auth whoami`).

## ▶️ START (bring it online)

```
cd "C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag"
.\scripts\ops.ps1 start
```

Equivalent by hand:

```
$db = (flyctl machines list -a semigraph-neo4j --json | ConvertFrom-Json)[0].id
flyctl machine start $db -a semigraph-neo4j
flyctl deploy --ha=false --remote-only --yes
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch off
```

Database first (`machine start`, ~30 s to `Started.`), then the API (`flyctl deploy`, ~2 min when
the image layers are cached, up to 15 min when the embedder layer rebuilds), then the phone-line
equivalent is rebound (`kill_switch off`). Check: open https://semigraph.fly.dev/ or run
`flyctl status -a semigraph` (1 machine, `started`).

## ⏹️ STOP (take it offline, stop cost)

```
cd "C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag"
.\scripts\ops.ps1 stop
```

Equivalent by hand:

```
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch on
flyctl scale count 0 -a semigraph --yes
flyctl machines list -a semigraph --json          # repeat until it lists no machine (up to about 5.5 minutes)
$db = (flyctl machines list -a semigraph-neo4j --json | ConvertFrom-Json)[0].id
flyctl machine stop $db -a semigraph-neo4j
```

Live questions go silent first (`kill_switch on` — cached answers still work while the page is
up), then the API machine is destroyed (`scale count 0`), **then the script waits until the API machine is gone** (up
to 330 s: the kill timeout of 300 s plus 30), and only then is the database machine stopped (**not** destroyed — its
volume and data stay attached). Check: `flyctl status -a semigraph` shows no machines; `flyctl status -a
semigraph-neo4j` shows the machine `stopped`.

Order matters in each — START brings the database up before the API (the API connects to it on
boot and fails its health check otherwise); STOP silences paid answers before killing machines, and stops the database
last because a draining API still writes its ledger rows to it.

If a paid answer is streaming when the API machine is removed, the process drains first (up to 240 s; usually it is
idle and stops at once): see "Stopping, deploying and the drain (M5a)" below. Whether `flyctl scale count 0` itself
waits for that drain was not verified (`docs/v2/research/fly_raw/` holds no such page), which is why the script polls
the machine list instead of trusting it. If the machine is still listed after 330 s the script warns and stops the
database anyway: Fly has killed the process by 300 s, and the rows of streams it cut are closed as `abandoned_restart` at
their estimate by the next boot.

## Status

```
.\scripts\ops.ps1 status
```

Shows both apps, `/healthz`, the kill-switch flag and the spend ledger (today / all-time paid and
cached answers, estimated USD from provider-reported token usage).

## Cost controls (all enforced server-side)

The limits, what a visitor sees at each, the kill levels, the state backend, the address-hash pepper and the drain are in
"M5a: paid-ask limits, kill levels, the state backend, the pepper and the drain" below.

| Control | Where | Default |
|---|---|---|
| Per-address window | in-process sliding window (`RATE_LIMIT_QUESTIONS` / `RATE_LIMIT_WINDOW_SECONDS`), reseeded from the ledger at boot | 5 per 10 min |
| Daily ceiling on paid answers | the state backend (`MAX_QUERIES_PER_DAY`), rebuilt from the Neo4j `SvcQuery` ledger at boot — survives restarts | 150 |
| Daily spend ceiling | the state backend (`MAX_SPEND_USD_PER_DAY`): estimate-based, a ceiling and not an expected cost | $10 |
| Per-address daily cap | the state backend (`PAID_PER_IP_PER_DAY`) | 20 |
| Kill switch | three levels (`on`, `retrieval_only`, `off`) in Neo4j `SvcPolicy` (`scripts/kill_switch.py`) or env `KILL_SWITCH=true` | off |
| Answer cache | Neo4j `SvcAnswer`, keyed on normalized question + strategy (`ANSWER_CACHE_TTL_HOURS`); the 20 benchmark answers are seeded permanently | 24 h |
| Concurrency | `MAX_CONCURRENT_ANSWERS` leases in flight; a full cap is a 429 before any stream starts | 2 |
| Output budget | `LLM_ANSWER_MAX_TOKENS` (streamed answers cannot regenerate on truncation; truncated answers are not cached) | 2400 on Fly |
| Bot gate | Cloudflare Turnstile (`TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY`). Production refuses to start without `TURNSTILE_REQUIRED=true` and the secret | on |

### Enabling the Turnstile bot gate (one-time, ~5 minutes)

Done for this deployment. Since M5a the production validators make it mandatory: a production process does not start
without `TURNSTILE_REQUIRED=true` and `TURNSTILE_SECRET_KEY` (see "Pre-deploy check" below), so there is no "gate off"
mode on Fly any more. `TURNSTILE_REQUIRED` is not a secret: it is a plain `fly.toml [env]` entry (`TURNSTILE_REQUIRED =
"true"`), where the pre-deploy check and a reader of the repo can see it. Only the two keys below are Fly secrets. (If it
was also pushed as a secret earlier, the two agree; leave it, or remove it with `flyctl secrets unset TURNSTILE_REQUIRED
-a semigraph`, which restarts the machine.)

1. Sign in at https://dash.cloudflare.com (a free account is enough; no domain needs to be on
   Cloudflare — Turnstile works on any hostname).
2. Left menu **Turnstile** → **Add widget**. Widget name: `semigraph`. Hostname: `semigraph.fly.dev`
   (add `localhost` too if you want to test locally). Widget mode: **Managed**. Pre-clearance: off.
   Create.
3. Copy the **Site Key** and the **Secret Key** into `.env.fly`:
   `TURNSTILE_SITE_KEY=0x...` and `TURNSTILE_SECRET_KEY=0x...`. The gate fails closed (`TURNSTILE_REQUIRED = "true"` in
   `fly.toml [env]`): if the secret is ever missing or Cloudflare cannot verify, live questions are refused instead of
   silently allowed.
4. Push them (this restarts the API machine):
   `python -m scripts.push_fly_secrets --only TURNSTILE_SITE_KEY,TURNSTILE_SECRET_KEY`
5. Verify: open https://semigraph.fly.dev/ — the widget renders under the Ask button; a live question
   works in the browser, while `curl -X POST .../api/ask` without a token gets **403 Bot check failed**.
   `flyctl logs -a semigraph` must not show the "turnstile not configured" warning any more.

Other protections already on: HSTS + CSP + nosniff/deny-frame headers, per-address windows on the
free read endpoints (`READ_RATE_LIMIT_PER_MINUTE`, default 120), cached and live question windows,
the daily ceiling, and the kill switch. The database is never exposed publicly (private 6PN only).

## M5a: paid-ask limits, kill levels, the state backend, the pepper and the drain

The M5a image (branch `v2`, not deployed yet) changes how paid questions are admitted. Deploy it in this order; each
step has its own section below.

1. **Pre-deploy check** (last section of this chapter, `uv run python -m scripts.check_env_fly`): `fly.toml [env]` plus
   `.env.fly` must pass the production validators, or the new image refuses to boot.
2. **Stage the pepper** ("IP-hash pepper cutover", steps 1 to 3): the new image will not boot without `IP_HASH_PEPPER`.
3. **Restart the Neo4j app with its new setting** ("The Neo4j transaction-monitor setting"), with the kill level `on`.
   Not needed to boot; needed before anyone relies on the 1.1 s bound of a state operation.
4. `flyctl deploy --ha=false --remote-only --yes` for the API (the old machine drains first, see "Stopping, deploying and
   the drain").
5. Verify (the pepper section, step 5).
6. Only on the owner's explicit go: null the old address hashes. It cannot be undone.

Rollback of the API: `flyctl deploy --image registry.fly.io/semigraph:<previous deployment tag>` (tags in
`flyctl releases -a semigraph`), after setting the kill level to `on` or `off`, never `retrieval_only` (next section).

### Paid-ask limits and the state backend

Every paid question (`POST /api/ask`, public or over an upload workspace) first takes a **lease** from the state backend.
The lease is granted only if every limit below allows it, and a durable `reserved` row is written to the ledger (Neo4j
`SvcQuery`) before the answer starts. When the answer ends, the lease is settled at its real cost. Nothing is bought
without a lease.

| Limit | Setting | Live value | A visitor over the limit sees |
|---|---|---|---|
| Paid questions per UTC day, all visitors | `MAX_QUERIES_PER_DAY` | 150 | 429 "The daily budget of live questions is used up — try an example, or come back tomorrow." |
| Spend per UTC day, estimate-based | `MAX_SPEND_USD_PER_DAY` | $10 | the same 429 and the same text |
| Paid questions per address per UTC day (IPv6 counted per /64) | `PAID_PER_IP_PER_DAY` | 20 | 429 "You have used today's live questions for your address — the example questions still work, or come back tomorrow." |
| Paid answers running at once | `MAX_CONCURRENT_ANSWERS` | 2 | 429 "The service is busy answering other questions — try again in a moment." (a plain refusal before any stream starts) |
| Paid questions per address in a short window | `RATE_LIMIT_QUESTIONS` per `RATE_LIMIT_WINDOW_SECONDS` | 5 per 10 min | 429 "Too many questions from your address — please wait a few minutes." |
| Any question per address in the same window (a cached answer needs only this one) | `FREE_RATE_LIMIT_QUESTIONS` | 30 | the same 429 text |
| Answer-cache reads before the bot check, whole process | `CACHE_READ_BUDGET_PER_S` | 10 per second | 429 "Too many requests from your address — please slow down." |
| Bot check failed | `TURNSTILE_REQUIRED` | true | 403 "Bot check failed — reload the page and try again." |

Both daily limits bind and whichever trips first pauses paid questions for the rest of the day. The count binds first
unless the average ask costs more than about 6.32 cents (6.16 cents when the asks are agent asks; the break-even tests of
`tests/test_serve_estimate.py`); above that, the $10 limit does. The spend limit is **estimate-based**: a lease is
charged the dearest the ask could cost (`serve/estimate.py`) until it settles, then its actual cost; a new ask is refused
if today's spend plus its own estimate would pass $10. So an agent ask (estimate about $0.83 with the live models: its
companies are capped at the anchor cap) is refused earlier in the day than a plain one (about $0.58;
`tests/test_serve_estimate.py` derives both from `fly.toml`). The estimates are written to the boot log, one
line per ask type: `flyctl logs -a semigraph | Select-String "ask_type="`. An ask whose client leaves before the answer
ends (or whose stream is cut) is charged its whole estimate, not what the model had spent so far.

The page shows the `detail` text of any refusal. Other refusals, all 503:

| When | A visitor sees |
|---|---|
| Kill level `on`; or the kill level not read for 30 s (never read, or the maintenance thread is stuck); or `KILL_SWITCH=true` | "Live questions are paused right now — the example questions still work." |
| Neo4j unreachable, a state operation slower than 1 s, or no state slot free within `STATE_OP_TIMEOUT_S` (the cache read, the workspace token, the reserve) | "Live questions are temporarily unavailable — please try again in a few minutes." (it does not promise the examples: the cache read itself failed) |
| Kill level `retrieval_only` | "Live questions are limited to cached answers right now — the example questions still work." |
| The process is draining | "The service is restarting — please try again in a minute." (with `Retry-After: 30`) |
| An upload while the kill level is not `off` (or while uploads are off) | "Uploaded documents are not available right now." |

**Failing closed.** When Neo4j is unreachable, or a state operation takes more than 1 s (`STATE_OP_TIMEOUT_S`), paid
questions are refused with the "temporarily unavailable" text (the "paused" text is the kill switch's alone), cached
answers are unavailable (503 too: a cache hit also writes a ledger row) and nothing falls through to a paid call. The same
text answers when all four state slots are held by calls stuck on a silent connection: an ask waits at most
`STATE_OP_TIMEOUT_S` for a slot, then is refused (`stream_runtime.slot_call`). `/healthz` answers 503 `degraded` while Neo4j cannot be reached.

**Gate order** (`serve/routes.py`): validate the question; draining; the free window; the answer cache (a hit is served
here, so cached answers are never stopped by the kill level or the daily limits; a workspace ask checks its token here
instead); the kill level; Turnstile; the short per-address window; then the lease (daily count, daily spend,
per-address daily, in flight). A refusal by the lease has already used a Turnstile check and one slot of the short
window; it takes nothing from the ledger.

**Which backend.** `STATE_BACKEND` (set in `fly.toml [env]`):

| Value | Counters | Use |
|---|---|---|
| `inprocess` (live) | in memory under one lock, one process; every ask still writes its ledger row to Neo4j | the one live machine |
| `neo4j` | in Neo4j, in the same transaction as the row | the rollback, and the only choice for more than one machine on one database; each ask pays a Neo4j round trip in the admission check |

Switch by changing `STATE_BACKEND` in `fly.toml` and redeploying (`flyctl deploy --ha=false --remote-only --yes`), or
without a deploy by `flyctl secrets set STATE_BACKEND=neo4j -a semigraph` (a restart; which of a secret and an `[env]`
entry wins when both exist was not verified, as in "Answering models": Fly's secrets page does not say, re-checked
2026-10-07, so `scripts/check_env_fly.py` assumes the secret wins and warns when a name is in both). Check what the machine runs:
`flyctl ssh console -a semigraph -C "printenv STATE_BACKEND"`, and the admin route `GET /api/admin/state` (header
`X-Admin-Token`) shows `state.backend`, today's count and spend, the leases in flight and the drain. There is nothing to
migrate: both backends write the same ledger rows and rebuild their counters from them.

**What a restart does** (boot rebuild, logged as `state rebuilt: N paid asks today (M micro-dollars), E closed after the
last stop, W address windows seeded`):

- Every `reserved` row of this machine, and any other long-expired one, is closed as `abandoned_restart` **and charged
  its estimate**: a stream killed by a restart costs its estimate against the day's count and $10.
- Today's count, spend and per-address counts are rebuilt from the ledger; the short per-address windows are reseeded from
  the rows of the last window. The free window, the read windows and the embedding cache start empty.
- The service does not serve paid questions until this succeeded and the kill level was read once. The rebuild is
  retried for 90 s; after that the boot fails with "the paid-ask ledger could not be read after 90s: not serving".
- The answer cache lives in Neo4j and survives.

### Kill levels

Three levels, stored in the `SvcPolicy` node `kill_switch` and cached in memory by the API (refreshed every
`KILL_SWITCH_REFRESH_S` = 10 s):

| Level | Paid questions and uploads | Cached answers and the example questions |
|---|---|---|
| `off` | accepted, subject to the limits | served |
| `retrieval_only` | refused, 503 with its own text (see above); uploads refused too | served |
| `on` | refused, 503 "paused"; uploads refused too | served |

The effective level is `on` whenever it was never read, was last read more than `KILL_SWITCH_STALE_S` = 30 s ago, or the
`KILL_SWITCH=true` environment override is set. Until the page has a retrieval-only view, `retrieval_only` looks to a
visitor like `on` with a different sentence.

Set it:

```
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch get                 # prints the stored level and the ledger
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch on
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch retrieval_only
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch off
```

This calls the admin route (`POST /api/admin/policy` with `X-Admin-Token`, body `{"kill_switch": "on"|"retrieval_only"|"off"}`;
`true` and `false` still mean `on` and `off`), reading `APP_BASE_URL` and `ADMIN_TOKEN` from `.env.fly`. The flip is
immediate on that machine. **A level that holds is applied in memory the moment the request arrives**, before the route waits
for its own one-token admin limiter, so an earlier admin call stuck on a silent connection (up to ~120 s) cannot delay an
emergency kill. A level holds when it is not `off` and at least as tight as the level the gates apply now (`on` while the
cache is stale or was never read). If it then cannot be stored (the database is down, or the admin limiter does not free
within `STATE_OP_TIMEOUT_S`) the answer is 200 with `"stored": false`: it is in force and queued, and the maintenance thread
writes it on its next tick once the database answers. **Confirm it:** `kill_switch get`, or `GET /api/admin/policy`
(`kill_switch` is the STORED level, `effective` what the gates apply), until the stored level is the one you set;
`kill_switch on` prints the same banner whether or not it was stored. A level that does not hold (a relaxation, or `off`) is
applied only once stored: a database or admin limiter that cannot do it gives 503 and changes nothing, and nothing is queued
(set it again). A `retrieval_only` set while the cache reads `on` only by age is such a relaxation: 503 during an outage.
`--direct` talks to a local or staging database instead, and refuses to store `retrieval_only` unless given
`--i-know-the-live-image-treats-it-as-off`.

**Rollback rule.** A pre-M5 image treats `retrieval_only` as `off`: it only knows `on` as stopped, so it would accept paid
questions while you believe they are paused. The image that is live today is such an image. **Before rolling back (or
deploying an older image), set the level to `on` or `off`, never `retrieval_only`.**

### IP-hash pepper cutover

Until M5a, a ledger row stored the client address as a 64-bit unsalted SHA-256, which anyone can brute-force for IPv4. The
new image stores an HMAC-SHA-256 under a secret pepper, and writes the pepper's version id (`IP_HASH_VERSION`, first one
2) on each row as `ip_hash_v`. **Production refuses to boot without a pepper of at least 32 bytes**, so the order matters.

1. Generate a pepper straight into `.env.fly` (the value is never printed and never in the shell history). The leading
   newline keeps the last line of a file without a trailing newline intact:
   ```
   (Select-String -Path .env.fly -Pattern '^IP_HASH_PEPPER=').Count        # 0 expected (it prints a count, not the value)
   uv run python -c "import secrets; open('.env.fly','a',encoding='utf-8').write('\nIP_HASH_PEPPER=' + secrets.token_urlsafe(48) + '\nIP_HASH_VERSION=2\n')"
   ```
   (48 random bytes, 64 characters.) Save `.env.fly` as UTF-8. A BOM is tolerated: `push_fly_secrets` and `check_env_fly` both decode it away (`utf-8-sig`), so
   it no longer mangles the first key name.
2. Check what will be pushed (names only): `& ".\.venv\Scripts\python.exe" -m scripts.push_fly_secrets --only IP_HASH_PEPPER,IP_HASH_VERSION --dry-run`
3. Stage it without restarting the running machine: `& ".\.venv\Scripts\python.exe" -m scripts.push_fly_secrets --only IP_HASH_PEPPER,IP_HASH_VERSION --stage`
4. **Then** deploy the image (`flyctl deploy --ha=false --remote-only --yes`). Deploying it before step 3 fails the health
   check by design.
5. Verify: `/healthz` is 200; the boot log has `state rebuilt: ...`; an example question is served cached; one live question
   from a browser (the bot check rejects scripts) works; and the new row carries the version:
   `flyctl ssh console -a semigraph-neo4j -C "cypher-shell -u neo4j -p <password> 'MATCH (q:SvcQuery) WHERE q.ip_hash_v = 2 RETURN count(q)'"`
   is at least 1. (Single quotes inside the double-quoted string: in Windows PowerShell 5.1 a `\"` ends the string and
   `(q:SvcQuery)` is then run as a command.)
6. **Only on the owner's explicit go**, null the old hashes. This is **IRREVERSIBLE**: those rows lose their address hash
   for good. The script (`scripts/null_legacy_ip_hashes.py`) runs on the API machine, against its own Neo4j; the runtime
   image carries only the installed package, so put the script there first:
   ```
   flyctl ssh sftp shell -a semigraph
       put scripts/null_legacy_ip_hashes.py /tmp/null_legacy_ip_hashes.py
   flyctl ssh console -a semigraph -C "python /tmp/null_legacy_ip_hashes.py"        # dry run: counts, changes nothing
   flyctl ssh console -a semigraph -C "python /tmp/null_legacy_ip_hashes.py --yes"  # the IRREVERSIBLE null
   ```
   The dry run prints which database it would act on (host and port, never a password). The script builds the app's
   settings from the machine's environment, so the production validators run in the ssh session too: if the session has
   `FLY_APP_NAME` but not the app's `[env]` values (check `flyctl ssh console -a semigraph -C "printenv ENVIRONMENT"`), it
   stops with a validator message such as `ENVIRONMENT must be production on the Fly app semigraph` and changes nothing.
   Afterwards
   `MATCH (q:SvcQuery) WHERE q.ip_hash IS NOT NULL AND q.ip_hash_v IS NULL RETURN count(q)` must be 0. It only touches
   rows that have an `ip_hash` and no `ip_hash_v`; rows written under a pepper are never changed. Exit status 1 means legacy
   rows remain.

After **any rollback to an image that writes unsalted hashes** (every image before M5a), that image adds new legacy rows:
once the M5a image is live again, run the dry run and then the null again.

**Per-address windows reset on cutover day.** The boot rebuild counts a row for a per-address daily count or window only if
it carries the current `ip_hash_v`; rows the old image wrote earlier the same day have none and are skipped. On the day of
the cutover every address therefore starts at 0 of 20 and with an empty 5-per-10-minute window. The day's total count and
spend still include every row, old or new. Rotating the pepper later does the same (windows reset, older rows stop
correlating): rotate only if it was exposed, and bump `IP_HASH_VERSION` with it, pushing both together.

### Stopping, deploying and the drain

The image runs `python -m semigraph.serve.drain` (not plain `uvicorn`): stock uvicorn closes its listener the moment it is
signalled and sse-starlette cuts every open stream. The drain starts on the **first SIGINT or SIGTERM**
(`fly.toml` pins `kill_signal = "SIGTERM"` and `kill_timeout = 300`, Fly's maximum, and they reach the machine with the
deploy of this `fly.toml`; `flyctl`'s own default for `machine stop` is SIGINT, per Fly's docs as recorded in
`docs/v2/research/m5-facts-fly-2026-10-03.md`. The drain treats the two alike).

- Nothing streaming and no upload running: the process shuts down at once (the drain looks every 100 ms).
- A stream or an upload is running: **the site drains for up to `DRAIN_TIMEOUT_S` = 240 s**. New paid questions, workspace
  questions, workspace creations and uploads get 503 "The service is restarting — please try again in a minute." Reads,
  `/healthz`, evidence, the example questions and cached answers keep being served. The drain ends the moment the last stream
  has written its ledger row and closed, or at 240 s: streams still running are then cut, their rows are written as
  abandoned and charged their estimate. Exit status 0 means it drained empty, 1 means streams were cut or it was forced.
- An upload whose body was still being read when the signal came is refused too (503, nothing started, the upload slot
  given back): the drain count is taken when the upload takes its slot, not only when the request arrives. An upload that
  was already running is waited for like a stream.
- **The shutdown budget** (it must fit the 300 s `kill_timeout`, and uvicorn's graceful timeout does not cover all of it):
  the drain (at most `DRAIN_TIMEOUT_S`) + uvicorn's wait for connections and tasks + the lifespan shutdown (which uvicorn
  runs AFTER that wait) + a 10 s margin. The lifespan's worst case is 32.5 s, budgeted as 35 s: up to 10 s waiting for
  stragglers, 5 s joining the state maintenance thread, 3 s flushing queued settles plus one state operation (1.5 s), 5 s
  stopping the freshness monitor, 5 s the upload sweeper, 3 s the tracer, then the two database drivers close. So after a
  drain that ran out at 240 s the wait for the cut streams to finish is cut to **15 s** (`240 + 15 + 35 + 10 = 300`); a
  drain that ends early keeps the full 250 s. A cut stream's cleanup normally takes a second or two (one ledger write); the
  lifespan then still waits up to 10 s for any that is not finished. `DRAIN_TIMEOUT_S` (read by the app's settings, 1 to
  254) cannot be set above 254: with the 1 s floor of the wait that is the most that still fits. A raised
  `STATE_OP_TIMEOUT_S` spends the slack of the 35 s (the budget assumes its 1 s default). `tests/test_serve_drain.py` sums
  these bounds from the constants of the code that sets them and fails when one grows. If Fly's SIGKILL does arrive during
  the shutdown, a settle that was lost is charged at its estimate by the next boot (an over-charge, never an
  under-charge).
- **A second signal exits without waiting** (streams ended at once; locally that is Ctrl+C twice). It sets uvicorn's
  `force_exit`, which skips the whole lifespan shutdown: **no settle flush, no stop of the maintenance thread, no driver
  close** (the connections to Neo4j are simply cut when the process ends). Streams are ended first, so their own cleanup
  runs, but a settle that could not be written is lost with the process: its row stays `reserved` and the next boot closes
  it as `abandoned_restart` at its estimate (an over-charge, never an under-charge). Whether Fly delivers a second signal
  to a machine that is already stopping was not verified. The hard limit is the 300 s `kill_timeout`, after which Fly kills
  the process.
- `ops.ps1 stop` sets the kill level to `on` first, so no new paid question starts, then runs `flyctl scale count 0` and
  waits until the API machine is gone before it stops the database (see STOP): the drain only waits for streams that were
  already open. A `flyctl deploy` over a running machine replaces it the same way,
  so a deploy also drains the old machine first (`ops.ps1 start` deploys onto an app with no machine: nothing to
  drain). Do not deploy during a demo.
- How Fly's proxy routes requests that arrive while the machine drains is not verified; the rolling-deploy drill in
  `docs/v2/M5A_BUILD_PLAN.md` section 6 measures it.

### The Neo4j transaction-monitor setting

Every state operation carries a 1 s server-side transaction timeout, but the server enforces it only when its transaction
monitor next looks: every `db.transaction.monitor.check.interval`, 2 s by default. `deploy/neo4j/fly.toml` now sets
`NEO4J_db_transaction_monitor_check_interval = "100ms"` (one underscore between every word: the setting name has no
underscore in it). **It is not live yet**: the `semigraph-neo4j` machine has to be redeployed once.

- **Symptom while it is missing:** an operation blocked on a lock (two asks racing for the day counter, a held lock) comes
  back after about 2 s instead of 1.1 s (measured: 1.99 s median, 2.01 s worst, against 1.04 s and 1.09 s with 100 ms), and
  the paid question that waited is refused with the "paused" text. Nothing is lost or wrong; it is only slower to fail.
  Do this before anything relies on the 1.1 s bound (the S7 and S2 measurements, an answer to "how long can a visitor wait").
- **Check first that the redeploy will not reload the seed.** `restore-and-start.sh` loads the baked dump only when its hash
  differs from `/data/.seeded` (or when `/data/import/neo4j.dump` exists); a reload replaces the service ledger and
  cache and deletes every live workspace. The local dump is git-ignored and may have been rebuilt:
  ```
  (Get-FileHash deploy\neo4j\seed\neo4j.dump -Algorithm SHA256).Hash.Substring(0,16).ToLower()
  flyctl ssh console -a semigraph-neo4j -C "cat /data/.seeded"
  ```
  The two must be equal (the image uses the first 16 hex characters of the dump's sha256). If they differ, stop and ask.
- **Apply it with the kill level `on`**, because the database restarts and the API fails closed meanwhile:
  ```
  & ".\.venv\Scripts\python.exe" -m scripts.kill_switch on
  cd deploy\neo4j; flyctl deploy --ha=false --remote-only --yes; cd ..\..
  flyctl ssh console -a semigraph-neo4j -C "printenv NEO4J_db_transaction_monitor_check_interval"     # 100ms
  curl.exe -s https://semigraph.fly.dev/healthz                                                          # db: true
  & ".\.venv\Scripts\python.exe" -m scripts.kill_switch off
  ```
  The `printenv` shows that the variable reached the container, not that the server accepted it; the boot log of
  `flyctl logs -a semigraph-neo4j` must show a normal start. The setting's effect itself was measured against a local
  Neo4j Community 2026.07.1, not against this machine.

### Pre-deploy check

The production validators in `src/semigraph/config.py` stop a production process from starting when any of these is true
(`ENVIRONMENT` and `CLIENT_IP_HEADER` are trimmed and lower-cased first, so `production ` is production; every pin below is
"not looser than what runs today", and a 0 means "off" elsewhere, so it is refused too):

| Setting | Refused when |
|---|---|
| `FLY_APP_NAME` (set by Fly itself) with `ENVIRONMENT` | the app is `semigraph` and `ENVIRONMENT` is not `production`, or `semigraph-stg` and it is not `staging`, or the app is any other name. No `FLY_APP_NAME` is a local process |
| `TURNSTILE_REQUIRED` / `TURNSTILE_SECRET_KEY` | not true / empty |
| `CLIENT_IP_HEADER` | not `fly-client-ip` |
| `IP_HASH_PEPPER` | under 32 bytes |
| `ADMIN_TOKEN` | set but under 32 characters. Empty is allowed: every `/api/admin` route then answers 404, and `scripts/kill_switch.py` and `ops.ps1` cannot flip the kill level |
| `MAX_QUERIES_PER_DAY` / `PAID_PER_IP_PER_DAY` / `MAX_CONCURRENT_ANSWERS` | not 1 to 150 / 1 to 20 / 1 to 4 |
| `MAX_SPEND_USD_PER_DAY` | not above 0 and at most 10 |
| `RATE_LIMIT_QUESTIONS` / `FREE_RATE_LIMIT_QUESTIONS` / `READ_RATE_LIMIT_PER_MINUTE` | not 1 to 5 / 1 to 30 / 1 to 120 (`fly.toml` sets none of them: the live value is the code default) |
| `WORKSPACE_CREATE_PER_DAY` / `UPLOADS_PER_HOUR` | not 1 to 3 / 1 to 10 |
| `RATE_LIMIT_WINDOW_SECONDS` | under 600 (a window of 0 never holds an event) |
| `CACHE_READ_BUDGET_PER_S` / `KILL_SWITCH_STALE_S` | above 10 / above 30 |
| `DRAIN_TIMEOUT_S` (any environment) | not 1 to 254 |

A refused boot fails the deploy's health check, so run the same validators first, over what the machine will see: the
`[env]` table of `fly.toml` plus the secrets of `.env.fly`. From the repo root, before every deploy:

```
uv run python -m scripts.check_env_fly
```

It prints one line per setting the validators cover, `PASS  NAME` or `FAIL  NAME  <the validator's reason>`, then `WARN`
lines and `RESULT: PASS` (exit code 0) or `RESULT: FAIL (n)` (exit code 1). **It prints setting names and the validators'
fixed reason texts only, never a value**, and a failure of the check itself prints the exception type only.

- **What it builds:** `fly.toml [env]`, plus the secrets of `.env.fly` that `push_fly_secrets` pushes by default (its
  `FLY_KEYS["semigraph"]`, non-empty values only), with the `fly.toml` app name as `FLY_APP_NAME`. `ENVIRONMENT` comes from
  `[env]` as it does on the machine, so a missing one fails the app-name tie (it is not assumed). It ignores the shell's
  variables and a `.env` in the working directory (the settings class is given the two files and nothing else).
- **Warnings** (they do not change the exit code): a UTF-8 BOM at the start of `.env.fly` (harmless since
  2026-10-08: `push_fly_secrets` reads `utf-8-sig` too; the warning text in `check_env_fly.py` still says the push would
  mangle the first key name, which is no longer true); a name that is both a
  secret and in `[env]`; keys of `.env.fly` that the default push does not send (they reach the machine only with `--only`,
  so they are not checked); and the pushed keys that are empty or absent from `.env.fly`, by name.
- **Assumption, not verified:** when a name is both a secret and an `[env]` entry, the check takes the secret. Fly's secrets
  documentation does not say which wins (re-checked 2026-10-07), hence the warning: keep each name in one place.
- **A secret that exists only on Fly is not seen.** If the check fails on `IP_HASH_PEPPER` or `ADMIN_TOKEN` and the secret
  is on the app (`flyctl secrets list -a semigraph` lists names only), put the same line into `.env.fly`.
- **If it fails on `ADMIN_TOKEN`:** the live token may be shorter than 32 characters (older images accepted any), and the new
  image will not boot with it. Append a generated one (the value is never printed; the later line wins when the file is
  read, delete the old line afterwards), re-run the check, and stage it so it arrives with the deploy:
  ```
  uv run python -c "import secrets; open('.env.fly','a',encoding='utf-8').write('\nADMIN_TOKEN=' + secrets.token_urlsafe(32) + '\n')"
  uv run python -m scripts.check_env_fly
  uv run python -m scripts.push_fly_secrets --only ADMIN_TOKEN --stage
  ```
  Until that deploy, the live machine still holds the old token, so `scripts/kill_switch.py` (which reads `.env.fly`) is
  refused (404) in between: flip the kill level before staging, or after the deploy.

## Secrets

`.env.fly` (git-ignored) holds the production values; `.env` stays local (Neo4j Desktop,
sentence-transformers). Push with `python -m scripts.push_fly_secrets` (API app) and
`python -m scripts.push_fly_secrets --app semigraph-neo4j` (database). Values travel on stdin,
only key names are printed. `SEC_USER_AGENT` (the freshness monitor's SEC identity, "Name email") is a secret rather
than a `fly.toml` entry because it names a real person. Rotate `ADMIN_TOKEN` (at least 32 characters, or the production
validators refuse to boot) by editing `.env.fly` and re-pushing. Rotating
`NEO4J_PASSWORD` needs `ALTER USER neo4j SET PASSWORD` inside the database
(`flyctl ssh console -a semigraph-neo4j -C "cypher-shell ..."`) because the container applies
`NEO4J_AUTH` only to a fresh system database, then re-push `NEO4J_PASSWORD` to the API app.

## First-time setup (already done for this deployment)

```
flyctl apps create semigraph-neo4j --org personal
flyctl volumes create neo4j_data -a semigraph-neo4j -r sin -s 3 -y
python -m scripts.push_fly_secrets --app semigraph-neo4j --stage     # NEO4J_AUTH
cd deploy\neo4j; flyctl deploy --ha=false --remote-only --yes; cd ..\..
flyctl apps create semigraph --org personal
python -m scripts.push_fly_secrets --stage                            # API secrets
flyctl deploy --ha=false --remote-only --yes
flyctl ips allocate-v4 --shared -a semigraph; flyctl ips allocate-v6 -a semigraph   # if the first deploy could not allocate
```

## Updating the graph

1. Refresh and rebuild locally on Neo4j Community (`semigraph ingest`, `freshness`, `extract`, `risk-items`, `align-items`,
   `build-graph --rebuild`, `scripts/verify_graph.py`; see the README), then dump it and prove the dump loads:
   [deploy/neo4j/seed/README.md](../deploy/neo4j/seed/README.md). A plain `semigraph align-items` is free and safe: it replays every
   recorded model answer (`adjudications.jsonl`, `passage_adjudications.jsonl`) and calls no model. Buying missing answers is a separate
   paid step (`align-items --adjudicate --adjudicate-passages --adjudicate-all-absent --max-usd <cap>`, estimate first with `--dry-run`;
   the worst case it checks against `--max-usd` covers every retry a call can bill, so it is several times the likely cost).
   `build-graph` refuses tables whose `alignment_provenance.json` shows they were built before the checkpoints reached their present
   state: whenever a checkpoint changed after the tables were written (a buying run's own tables are already consistent), rerun the plain
   `align-items`, and only after every paid run has finished (a job still running rewrites the tables with the code it was started with).
2. Run the benchmark on the new graph into its own runs file and regenerate the example answers
   (they carry the snapshot id; the service does NOT seed examples from another snapshot):
   `semigraph eval --runs-file eval_runs.<snap>.jsonl --report-suffix .<snap> --max-answer-usd 1.75`, then
   `PYTHONPATH=src python scripts/build_examples.py --runs data/processed/eval_runs.<snap>.jsonl --snapshot <snap id>`.
3. `python -m scripts.kill_switch on`, then `cd deploy\neo4j; flyctl deploy --ha=false --remote-only --yes` — the
   entrypoint detects the new dump hash and reloads it on boot (this replaces the service ledger/cache, resets the
   kill switch to off and deletes every live upload workspace: avoid it during a demo). Then from the repo root `flyctl deploy --ha=false --remote-only --yes` (new examples.json),
   verify (`/healthz`, `/api/stats` shows the new snapshot id, an example click is served cached), then `kill_switch off`.
4. Rollback: `flyctl deploy --image registry.fly.io/<app>:<previous deployment tag>` for each app (find the tags in
   `flyctl releases -a <app>`); the older DB image carries the older seed and reloads it. Keep the previous dump outside git
   (`data/backups/`). The Turnstile gate rejects scripted clients, so a live paid question can only be tested from a browser.

## Answering models (v1.2)

Three settings, three roles (defined in `src/semigraph/config.py`, documented in `.env.example`):

| Setting | Role | Code default | Production |
|---|---|---|---|
| `ANSWER_MODEL` | drafts every live answer | `anthropic/claude-sonnet-5` | `openai/gpt-6-luna` |
| `ESCALATION_MODEL` | answers change-over-time questions directly and re-answers any draft the checks reject | empty (no escalation) | `anthropic/claude-sonnet-5` |
| `LLM_MODEL` | extraction and the evaluation judges only (schema-sensitive; it never follows the answering model) | `anthropic/claude-sonnet-5` | not used by the service |

Production values are pinned in `fly.toml [env]` (`ANSWER_MODEL`, `ESCALATION_MODEL`), so a redeploy reproduces them; the
code default for `ANSWER_MODEL` stays Sonnet so a local run is Sonnet. A draft is rejected when it is empty, truncated, cites
an id outside the retrieved context, has no citation (unless it is a refusal or every dollar figure is in the METRICS
block), carries a number that matches nothing in the retrieved context, or uses a bracketed pseudo-citation. A draft is
buffered until it passes, so the first token appears after generation. Provider keys are Fly secrets (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`); push names with `python -m scripts.push_fly_secrets --only OPENAI_API_KEY [--env .env]`.

**Before the first deploy of this configuration**, check what the running machine will see:
`flyctl ssh console -a semigraph -C "printenv ANSWER_MODEL ESCALATION_MODEL LLM_MODEL"`. The v1.2 deployment staged
`LLM_MODEL=openai/gpt-6-luna` and `ESCALATION_MODEL` as Fly *secrets*; after this change `LLM_MODEL` no longer affects
answering, and `ESCALATION_MODEL` is now set in two places (same value). To keep one source of truth, run
`flyctl secrets unset ESCALATION_MODEL -a semigraph` once the deploy shows the right values (which of a secret and an
`[env]` entry wins when both are set was not verified here; do not rely on it).

**Revert to Sonnet only:** in `fly.toml` set `ANSWER_MODEL = "anthropic/claude-sonnet-5"` and `ESCALATION_MODEL = ""`
(with an empty escalation model, or the same model in both roles, Sonnet streams every answer live and nothing is
buffered), then `flyctl deploy --ha=false --remote-only --yes`. Without editing the repo (a secret change restarts the machines): `flyctl secrets set ANSWER_MODEL=anthropic/claude-sonnet-5`
and `flyctl secrets unset ESCALATION_MODEL` (subject to the caveat above). Check the models from inside the container without
the bot gate: `flyctl ssh console -a semigraph -C "python -c ..."` calling `litellm.completion(**completion_params(model, n))`.

**What the page's answer badge means** (from the `done` event; it never says more than what happened): *routed* = a
change-over-time question went straight to `ESCALATION_MODEL` with no cheap draft; *escalated after a failed check (reasons)* =
the `ANSWER_MODEL` draft was rejected and re-answered; *`<model>` draft passed the checks* = the draft was released. The
checks line reports only what `checks` proves: cited ids were retrieved, numbers matched the retrieved context, and any
unmatched numbers or bracketed pseudo-citations are listed as warnings. None of this proves a sentence is supported by the
passage it cites.

## The M3 agent (`strategy=agent`)

Opt-in: a cheap retrieval planner (`AGENT_PLANNER_MODEL`, default `openai/gpt-6-luna`) adds up to `AGENT_MAX_TOOL_CALLS`
bounded, read-only lookups in front of the SAME shared writer the fixed path uses (`stream_answer_for_context`) — never a
separate answer format, never its own citation or verification rules. A planner failure of any kind falls back to the
plain retrieval with no error shown to the visitor.

| Setting | Role | Default |
|---|---|---|
| `AGENT_ENABLED` | turns the strategy on; `guard.validate_strategy` rejects `agent` and `/api/stats` reports `agent_enabled` when off | `false` |
| `AGENT_PLANNER_MODEL` | plans tool calls only; the answer still comes from `ANSWER_MODEL` / `ESCALATION_MODEL` | `openai/gpt-6-luna` |
| `AGENT_MAX_TOOL_CALLS` / `AGENT_MAX_MODEL_CALLS` / `AGENT_TIME_BUDGET_S` | bounds on one run | `4` / `3` / `25` |

Production values are pinned in `fly.toml [env]`, so a redeploy reproduces them. The API image ships `langgraph` in
`deploy/requirements-serve.txt` unconditionally (`AGENT_ENABLED` is then a config flip, not an image rebuild), but
`serve/main.py`'s lifespan only imports `semigraph.agent.stream` when the flag is on, so a deployment with it off never
needs the package to actually work. Turning it on for the first time: `flyctl ssh console -a semigraph -C "printenv
AGENT_ENABLED"` after deploy to confirm, then watch `flyctl logs -a semigraph` for `neo4j reachable` and no import error
at boot (a missing `langgraph` in a future slimmed image would fail fast there, before the first agent question, not on it).

**Ship gate** (docs/v2/M3_AGENT_PLAN.md section 7): the flag was turned on only after a live paid evaluation showed the
agent never scores worse than the fixed path on the 60-question benchmark, adds no new safety-check failure the fixed
path did not already have on the identical question, and stays within the latency/cost envelope. That evaluation is
`semigraph eval-agent` (PAID; needs `--confirm-paid` and `--max-usd`); re-run it after any change to the planner prompt,
the tool set, or the shared answer prompt.

**Revert to fixed-path only:** `flyctl secrets set` is not needed — `AGENT_ENABLED` is a plain (non-secret) `[env]` value;
set it to `false` in `fly.toml` and `flyctl deploy --ha=false --remote-only --yes`, or flip it without a deploy with
`flyctl secrets set AGENT_ENABLED=false` (subject to the same env-vs-secret precedence caveat as the answering models
above — verify with the `printenv` check after either path). No existing question can be mid-flight in a way this
affects: `strategy` is chosen once per request.

## Freshness monitor (M4)

A background thread in the API process compares what EDGAR and the Federal Register have NOW with what the SERVED graph
holds: in-scope filings (the same `select_targets` rule the pipeline uses) that are not a `Filing` node, and the live BIS rule
count against the `ExportControl` nodes. It **detects and reports; it never ingests** (docs/v2/M4_PLAN.md D1): promotion stays
the reviewed pipeline in "Updating the graph".

| Setting | Role | Production |
|---|---|---|
| `FRESHNESS_ENABLED` | starts the thread | `true` (`fly.toml [env]`) |
| `FRESHNESS_POLL_HOURS` / `FRESHNESS_BOOT_DELAY_S` | cadence; the first check runs 300 s after boot, and only when the last one is older than the poll | `6` / `300` |
| `SEC_USER_AGENT` | SEC fair-access identity ("Name email"); a Fly **secret** because it names a real person | pushed with `python -m scripts.push_fly_secrets --only SEC_USER_AGENT` (add `--env .env` when `.env.fly` does not carry it) |

- One machine checks at a time (a `SvcLease` node in Neo4j); the result is stored as `SvcFreshness` and survives restarts.
- `GET /api/freshness` (public) returns the last result; `/api/stats` carries its short form for the page header.
- `POST /api/admin/freshness/check` with `X-Admin-Token` runs a check now (409 while one runs, 503 without `SEC_USER_AGENT`).
- A failed check keeps the last good result, records `last_error_at`, and retries after 30 minutes.
- Without `SEC_USER_AGENT` the monitor reports `unconfigured` and idles; the service still starts.
- `scripts/freshness_heartbeat.py <base url>` is the external check (exit 0 on ok/never, non-zero on error/stale/unconfigured,
  a warning when the app is stopped on purpose). The GitHub workflow that runs it every 6 hours activates once `v2` is on the
  default branch (GitHub runs scheduled workflows only from there).
- Parity with the pipeline's own check: `scripts/freshness_parity.py --as-of <date>` against the local graph (record in
  `artifacts/freshness_parity.json`).

## Upload workspaces (M4)

Visitors create a private workspace, upload PDF, DOCX, Markdown, HTML or text documents (and newer versions of them), ask
questions that cite them as `doc:` ids next to the filing evidence, and see what changed between versions.

| Setting | Role | Production |
|---|---|---|
| `UPLOADS_ENABLED` | every workspace route and workspace ask; off = 503 | `true` (`fly.toml [env]`) |
| `WORKSPACE_TTL_HOURS` | a workspace and everything in it is deleted after this | `24` |
| `UPLOAD_MAX_PAGES` / `UPLOAD_MAX_TOKENS` | per version (the embedding time sets them; see docs/v2/M4_PLAN.md section 3) | `30` / `16000` |
| `UPLOAD_MAX_WORKSPACE_TOKENS` / `MAX_UPLOADS_PER_DAY` | embedding budget per workspace / uploads per day, all visitors | `48000` / `40` |

- **Machine:** `shared-cpu-2x`, 4 GB (`fly.toml [[vm]]`): one upload embeds on one core while live questions use the other.
  Uploads embed locally with the same ONNX model as queries; no document leaves the deployment.
- **Isolation:** workspace data lives only in `User*` nodes keyed by `workspace_id`; the token is shown once and only its
  sha256 is stored; an unknown workspace and a wrong token both answer 404. Workspace answers are never cached, never use the
  agent, and are logged as counts only; the access log shows `<ws:hash>`.
- **Turnstile** is mandatory for creating a workspace and for each upload (`X-Turnstile-Token` header, checked before the body
  is read). Without `TURNSTILE_SECRET_KEY` in production these routes answer 503.
- **Parsing** runs in a separate process with a 1 GiB address-space limit, a 90 s timeout and no secrets in its environment;
  only the parsed text comes back. The API process marks itself non-dumpable at boot, so that child cannot read the API's
  secrets through `/proc` either (the boot log says "process marked non-dumpable"). `flyctl ssh console` runs as root and
  still reads `/proc/<pid>/status` for memory checks.
- **Deletion:** `DELETE /api/workspace/{id}` removes everything at once; a sweeper deletes expired workspaces every 15 minutes
  (it runs even when `UPLOADS_ENABLED` is off). A dump swap of the database (see "Updating the graph") also deletes every live
  workspace.
- **Soft rollback:** `UPLOADS_ENABLED = "false"` in `fly.toml` and redeploy (existing workspaces still expire on schedule).
  **Image rollback to a pre-M4 image:** first delete workspace data, because an older image has no sweeper:
  `flyctl ssh console -a semigraph-neo4j -C 'cypher-shell -u neo4j -p <password> \"MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH ''User'') DETACH DELETE n\"'`
  (a PowerShell single-quoted string: its `\"` reach the remote command as double quotes and `''` as one single quote;
  the same command inside a double-quoted string breaks in Windows PowerShell 5.1).
  Moving the VM back to `shared-cpu-1x` / 2 GB requires `UPLOADS_ENABLED = "false"`.

## Troubleshooting

- `/healthz` 503 → the API cannot reach Neo4j: `flyctl status -a semigraph-neo4j` (machine must be
  `started`), `flyctl logs -a semigraph-neo4j`. The API retries the connection for 90 s on boot.
- The machine will not boot and `flyctl logs -a semigraph` shows `... must be set to at least 32 bytes in production`,
  `ENVIRONMENT must be production on the Fly app semigraph` or another `... in production` line: a production validator
  refused the settings (it names the setting, never a value). Fix the secret or `[env]` entry and redeploy; run the
  pre-deploy check (`uv run python -m scripts.check_env_fly`) first.
- Boot fails with "the paid-ask ledger could not be read after 90s: not serving": Neo4j was unreachable for the whole boot
  rebuild. Start the database (`flyctl status -a semigraph-neo4j`) and restart the API machine.
- Every live question answers "Live questions are paused right now": the kill level is `on` (or was never read, or is older
  than 30 s: the maintenance thread is stuck), or `KILL_SWITCH=true` is set. (With Neo4j down a visitor sees "temporarily
  unavailable" instead, never "paused": the cache read comes before the kill gate.) `& ".\.venv\Scripts\python.exe" -m scripts.kill_switch get`,
  then `GET /api/admin/state` (header `X-Admin-Token`): `state.kill`, `state.kill_age_s`, `pending_settles` and `maintenance.alive`.
- Every live question answers "Live questions are temporarily unavailable": Neo4j is unreachable or slower than 1 s, or the
  four state slots are all held by calls stuck on a silent connection (each can hold one for up to ~120 s).
  `/healthz`, then `GET /api/admin/state`: `limiters.state.borrowed` of 4 and `limiters.admin`. The admin routes run on their
  own one-token limiter, so they still answer.
- The API is always-warm while online (`min_machines_running = 1`); if it was ever auto-stopped, the first request pays ~10 s (machine boot + 1 GB embedder load).
- `flyctl logs -a semigraph` — every answered question logs strategy, citation count, hallucinated
  count and cost.
- Out-of-memory on the API machine → the embedder needs ~1.3 GB resident and an upload adds up to ~0.4 GB while it embeds;
  keep `memory = "4gb"` while uploads are enabled.
- Restoring a dump by hand: `flyctl ssh console -a semigraph-neo4j -C "mkdir -p /data/import"`, copy
  the dump to `/data/import/neo4j.dump` (an sftp client over `flyctl ssh sftp shell`), then
  `flyctl machine restart` — the entrypoint loads it before Neo4j starts.

## Local development

```
uv sync --extra serve
$env:EMBEDDING_BACKEND="onnx"; $env:ONNX_MODEL_PATH="models\qwen3-embedding-0.6b-q8\model_q8.onnx"
$env:ADMIN_TOKEN="localtest"
uv run uvicorn semigraph.serve.main:app --app-dir src --port 8080
```

The ONNX model is built once with `uv run python scripts/build_onnx_embedder.py` (downloads the
2.4 GB fp32 export, writes ~1.1 GB, verifies fidelity ≥ 0.98 cosine against sentence-transformers).
Without it, `EMBEDDING_BACKEND=local` uses the torch model from the Hugging Face cache.
