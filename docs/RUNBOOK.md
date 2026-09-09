# Operations runbook — semigraph on Fly.io

Two Fly apps in the `personal` org, region `sin`:

| App | What | Machine | State when "online" | Cost when "offline" |
|---|---|---|---|---|
| `semigraph` | FastAPI + ONNX embedder (public: https://semigraph.fly.dev) | shared-cpu-1x, 2 GB, auto-stops when idle | 0 or 1 machine (starts on request) | scaled to 0 → rootfs only (~$0.20/mo) |
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
`flyctl status -a semigraph` (1 machine; `started` or stopped-by-autoscaler are both fine).

## ⏹️ STOP (take it offline, stop cost)

```
cd "C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag"
.\scripts\ops.ps1 stop
```

Equivalent by hand:

```
& ".\.venv\Scripts\python.exe" -m scripts.kill_switch on
flyctl scale count 0 -a semigraph --yes
$db = (flyctl machines list -a semigraph-neo4j --json | ConvertFrom-Json)[0].id
flyctl machine stop $db -a semigraph-neo4j
```

Live questions go silent first (`kill_switch on` — cached answers still work while the page is
up), then the API machine is destroyed (`scale count 0`), then the database machine is stopped
(**not** destroyed — its volume and data stay attached). Check: `flyctl status -a semigraph`
shows no machines; `flyctl status -a semigraph-neo4j` shows the machine `stopped`.

Order matters in each — START brings the database up before the API (the API connects to it on
boot and fails its health check otherwise); STOP silences paid answers before killing machines.

## Status

```
.\scripts\ops.ps1 status
```

Shows both apps, `/healthz`, the kill-switch flag and the spend ledger (today / all-time paid and
cached answers, estimated USD from provider-reported token usage).

## Cost controls (all enforced server-side)

| Control | Where | Default |
|---|---|---|
| Per-address window | in-process sliding window (`RATE_LIMIT_QUESTIONS` / `RATE_LIMIT_WINDOW_SECONDS`) | 5 per 10 min |
| Daily ceiling on paid answers | Neo4j `SvcQuery` ledger (`MAX_QUERIES_PER_DAY`) — survives restarts | 150 (≈ $9/day worst case at ~$0.06/answer) |
| Kill switch | Neo4j `SvcPolicy` (`scripts/kill_switch.py`) or env `KILL_SWITCH=true` | off |
| Answer cache | Neo4j `SvcAnswer`, keyed on normalized question + strategy (`ANSWER_CACHE_TTL_HOURS`); the 20 benchmark answers are seeded permanently | 24 h |
| Concurrency | `MAX_CONCURRENT_ANSWERS` LLM calls in flight | 2 |
| Output budget | `LLM_ANSWER_MAX_TOKENS` (streamed answers cannot regenerate on truncation; truncated answers are not cached) | 2400 on Fly |
| Bot gate | Cloudflare Turnstile when `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` are set; otherwise a warning is logged and the caps above are the control | off |

### Enabling the Turnstile bot gate (one-time, ~5 minutes)

1. Sign in at https://dash.cloudflare.com (a free account is enough; no domain needs to be on
   Cloudflare — Turnstile works on any hostname).
2. Left menu **Turnstile** → **Add widget**. Widget name: `semigraph`. Hostname: `semigraph.fly.dev`
   (add `localhost` too if you want to test locally). Widget mode: **Managed**. Pre-clearance: off.
   Create.
3. Copy the **Site Key** and the **Secret Key** into `.env.fly`:
   `TURNSTILE_SITE_KEY=0x...` and `TURNSTILE_SECRET_KEY=0x...`, and add `TURNSTILE_REQUIRED=true`
   (fail closed: if the secret is ever missing or Cloudflare cannot verify, live questions are refused
   instead of silently allowed).
4. Push them (this restarts the API machine):
   `python -m scripts.push_fly_secrets --only TURNSTILE_SITE_KEY,TURNSTILE_SECRET_KEY,TURNSTILE_REQUIRED`
5. Verify: open https://semigraph.fly.dev/ — the widget renders under the Ask button; a live question
   works in the browser, while `curl -X POST .../api/ask` without a token gets **403 Bot check failed**.
   `flyctl logs -a semigraph` must not show the "turnstile not configured" warning any more.

Other protections already on: HSTS + CSP + nosniff/deny-frame headers, per-address windows on the
free read endpoints (`READ_RATE_LIMIT_PER_MINUTE`, default 120), cached and live question windows,
the daily ceiling, and the kill switch. The database is never exposed publicly (private 6PN only).

## Secrets

`.env.fly` (git-ignored) holds the production values; `.env` stays local (Neo4j Desktop,
sentence-transformers). Push with `python -m scripts.push_fly_secrets` (API app) and
`python -m scripts.push_fly_secrets --app semigraph-neo4j` (database). Values travel on stdin,
only key names are printed. Rotate `ADMIN_TOKEN` by editing `.env.fly` and re-pushing. Rotating
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

1. Rebuild locally (notebooks or `semigraph build-graph`), then export in Community format:
   see [deploy/neo4j/seed/README.md](../deploy/neo4j/seed/README.md).
2. `cd deploy\neo4j; flyctl deploy --ha=false --remote-only --yes` — the entrypoint detects the new
   dump hash and reloads it on boot (this replaces the service ledger/cache too, and resets the kill switch to off — run `kill_switch on` again if the demo should stay paused).
3. The API needs no redeploy; a restart re-seeds the benchmark answers:
   `flyctl machine restart <id> -a semigraph`.

## Troubleshooting

- `/healthz` 503 → the API cannot reach Neo4j: `flyctl status -a semigraph-neo4j` (machine must be
  `started`), `flyctl logs -a semigraph-neo4j`. The API retries the connection for 90 s on boot.
- First request after idle takes ~10 s (machine boot + 1 GB embedder load). Expected.
- `flyctl logs -a semigraph` — every answered question logs strategy, citation count, hallucinated
  count and cost.
- Out-of-memory on the API machine → the embedder needs ~1.3 GB resident; keep `memory = "2gb"`.
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
