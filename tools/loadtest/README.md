# tools/loadtest: the S2 load generator (M5a I5)

The pre-registered 1,000-user test of `docs/v2/M5_PLAN.md` section 6, as code: a Locust generator that offers exactly the registered
traffic model, the offline checks that keep its live asks uncached (council 5), and `scripts/loadtest_report.py`, which turns the raw
files of a run into PASS / FAIL / VOID. Nothing here runs against production: the generator refuses any host that is not loopback or a
`*-stg` Fly app.

## What is where

| file | role | imports |
|---|---|---|
| `model.py` | the traffic model: populations, think time, phases, pacing, per-VU address, host guard, staging-limit warnings | stdlib |
| `sse.py` | strict parser of the site's SSE grammar (`retrieval/step/delta/escalated/done/error`, pings, the `job` stream) | stdlib |
| `salt.py` | `f"{q} (ref {worker}{n:06d})"`, fixed width, deterministic | stdlib |
| `pool.py` + `pool.json`, `pool_composition.json` | the 300-question pool and its composition | stdlib to load; `semigraph` (lazily) to build |
| `salt_check.py` | council 5's offline checks, JSON result for the report | developer side (`semigraph`) |
| `records.py`, `client.py`, `user.py`, `phases.py` | the raw record, the ask / read / upload client, one virtual user, the phase clock | stdlib + `requests` |
| `cpu_watch.py` | 1 Hz CPU / memory sampler, JSONL, one process per machine | `psutil` |
| `locustfile.py` | glue: `HttpUser`, the staged `LoadTestShape`, the per-process log | **locust** (the only file that imports it) |
| `launch.py` | the image's entry point: CPU sampler beside Locust, by `LOADTEST_ROLE` | stdlib |

Image: `deploy/staging/Dockerfile.loadgen` (hash-locked `requirements-loadgen.txt`, no `semigraph`, no PDF library, non-root).
Fly app definition: `deploy/staging/fly.loadgen.toml`. Locust 2.46.7 is MIT-licensed.

## The traffic model, and the one place a generator can miss it

1,000 VUs, a cycle of U(120, 300) s, per iteration the shell + `/api/stats` + `/api/examples` + `/api/freshness` and exactly one ask
(45 % cached example, 40 % live from the pool, 10 % live unique, 5 % live agent), plus an evidence read 20 % and a dossier or
risk-changes read 5 % of the time; 5 upload VUs, one upload per 10 min, watched to `ready`. 4.76 iterations/s, 2.62 live asks/s.
That figure holds only if 120-300 s is the whole cycle **start to start**. Locust's `between(120, 300)` waits after the iteration, which
stretches the cycle by the stream time (about 220-235 s) and offers ~2.35-2.5 live/s, under the VOID floor of 2.47 by construction. So
`user.py` draws the cycle at the start of the iteration and sleeps only the remainder (an overrun is recorded), and a new VU's first
delay is the residual life of the cycle process, so the rate is stationary from the first minute. `tests/test_tools_loadtest.py` pins both.

Council 5's interpretation, stated for the owner: every live ask (the 40 % pool, the 10 % unique and the 5 % agent) carries the same
`(ref ...)` salt, so the 10 % "unique suffix" population differs from the pool population in label only; the cached examples are sent
unsalted and come from the live `/api/examples` response.

## Running it

```
# 1. offline, before any staging window (developer venv)
PYTHONPATH=. uv run python -m tools.loadtest.pool check                      # is pool.json current? (rebuild: ... pool build)
PYTHONPATH=. uv run python -m tools.loadtest.salt_check --out artifacts/loadtest/<date>/salt_check.json
LOADTEST_SALT_CHECK_LOCAL_GRAPH=1 ...                                         # opt-in: local Neo4j + the embedder that will ship

# 2. generators (the image, or locally in a venv that has locust)
LOADTEST_ROLE=master  LOADTEST_HOST=http://semigraph-stg.internal:8080 LOADTEST_EXPECT_WORKERS=2 python -m tools.loadtest.launch
LOADTEST_ROLE=worker  LOADTEST_MASTER_HOST=<master>.internal LOADTEST_WORKER=1 python -m tools.loadtest.launch
#    every machine also needs LOADTEST_RUN_ID, LOADTEST_ORIGIN_AUTH (secret), LOADTEST_SALT_START (see below)

# 3. collect /out/** from each generator into RUN_DIR/generator and RUN_DIR/cpu, add ledger.json, mock.json, server cpu, then
python scripts/loadtest_report.py RUN_DIR --fly-embed-s <S6 figure>
```

Environment (all optional unless marked): `LOADTEST_RUN_ID` (required), `LOADTEST_ORIGIN_AUTH` (required off loopback), `LOADTEST_WORKER`
(the salt's worker digit 0-9, one per generator machine), `LOADTEST_SALT_START` (the first counter; **runs that share an answer cache
must not overlap**: a pilot, a restart and the soak against one Neo4j need different starts, or a reset cache), `LOADTEST_TURNSTILE_TOKEN`,
`LOADTEST_OUT_DIR`, `LOADTEST_POOL`, `LOADTEST_STATIC_PATHS`, `LOADTEST_PHASE_WEBHOOK`, `LOADTEST_SEED`. A non-default `LOADTEST_THINK_*`,
`LOADTEST_UPLOAD_*`, `LOADTEST_VUS` or `LOADTEST_SHAPE=off` is recorded in `meta` and the report refuses a gate verdict for it (VOID).

## The verdict

`scripts/loadtest_report.py` documents the rule and the run directory it reads. In short: VOID if the offered live rate is under
0.95 x 2.6, a generator is over 70 % CPU, an offline check failed, or CPU-seconds per live ask are under 0.8 x the
Fly per-embed cost (the four pre-registered clauses), or a validity input is missing, or one of the clauses the harness added applies
(the mock over 50 % CPU, which is a **harness rule (not pre-registered)**: the 50 % comes from the harness plan, not from `M5_PLAN.md`,
`M5_DECISIONS.md` or council 5, and the rule is kept; a restarted generator, a model other than the registered one, a pool or
salt-format mismatch, a salted ask without a salt or answered from the cache, colliding salted questions); else FAIL on TTFE p95 > 1.5 s, any drop, errors >= 0.5 % (429 / 503 included; over every
request, asks, or live asks, whichever is worst), server CPU p95 > 70 %, ledger rows != terminals, or an overshoot; else PASS. Every VOID
reason is tagged with its source. The VOID / FAIL split and the harness-added clauses are **pending owner sign-off**. The 10-minute
zero-overshoot sub-run is judged with `--profile subrun` (needs `ledger.cap`). Each Locust worker needs its own `LOADTEST_WORKER` digit
(the launcher refuses a worker without one) and one Locust process per digit: `--processes` is unsupported.

## Local smoke (opt-in)

`tests/test_loadtest_local_smoke.py` runs real Locust against a fake staging API (`tests/loadtest_fake_server.py`) and then the report.
Locust is not a dependency of the developer environment (it adds a pytest plugin and gevent): install it in its own venv (short path on
Windows) and point `LOADTEST_PYTHON` at it; the docstring has the commands.
