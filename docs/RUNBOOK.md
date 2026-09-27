# Operations runbook — semigraph on Fly.io

Two Fly apps in the `personal` org, region `sin`:

| App | What | Machine | State when "online" | Cost when "offline" |
|---|---|---|---|---|
| `semigraph` | FastAPI + ONNX embedder (public: https://semigraph.fly.dev) | shared-cpu-1x, 2 GB, always-warm while online | 1 machine started | scaled to 0 → rootfs only (~$0.20/mo) |
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
| Daily ceiling on paid answers | Neo4j `SvcQuery` ledger (`MAX_QUERIES_PER_DAY`) — survives restarts | 150 (≈ $9/day worst case at Sonnet-only prices, ~$0.06/answer; the Luna path measured about $0.0067/answer on the benchmark) |
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
   entrypoint detects the new dump hash and reloads it on boot (this replaces the service ledger/cache and resets the
   kill switch to off). Then from the repo root `flyctl deploy --ha=false --remote-only --yes` (new examples.json),
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

## Troubleshooting

- `/healthz` 503 → the API cannot reach Neo4j: `flyctl status -a semigraph-neo4j` (machine must be
  `started`), `flyctl logs -a semigraph-neo4j`. The API retries the connection for 90 s on boot.
- The API is always-warm while online (`min_machines_running = 1`); if it was ever auto-stopped, the first request pays ~10 s (machine boot + 1 GB embedder load).
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
