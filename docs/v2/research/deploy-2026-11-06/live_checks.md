# Decision 21 deploy: live check outputs (2026-11-06, UTC)

Captured from the commands run during the deploy (record: `../../M5_DECISIONS.md` 10.9). Secrets and personal data omitted.

## Before the pause

- Neo4j seed marker on the machine (`cat /data/.seeded`): `f8211546df347d82`; local `deploy/neo4j/seed/neo4j.dump` sha256[:16]:
  `f8211546df347d82` (equal). `/data/import` empty (no pending import dump).
- `flyctl secrets list -a semigraph`: `IP_HASH_PEPPER` Staged (then Deployed after v15).
- `scripts/kill_switch status`: off; ledger today paid 0 / cached 0; all time paid 29 / cached 9, $0.0462.

## Neo4j monitor interval (04:59:55 kill switch on)

- `flyctl machine update 2879170f3e5228 -a semigraph-neo4j --env NEO4J_db_transaction_monitor_check_interval=100ms --yes`:
  "updated successfully", health 1/1.
- `printenv NEO4J_db_transaction_monitor_check_interval` on the machine: `100ms`. Boot log: "Bolt enabled", "Started." 05:01:05;
  the `system` database kept its creation date 2026-09-09.
- Old app `/healthz` after the restart: `{"status":"ok","db":true,"embedder":"onnx:model_q8.onnx"}`.

`stats.post.json` and `healthz.post.json` were captured right after v15 came up, while the kill switch was still `on`
(`"paused": true`); the round trip below ends `off`, and a later read-only `/api/stats` shows `"paused": false`.

## Kill switch round trip on v15 (uncached question, no Turnstile token sent)

```
KILL SWITCH ON              -> 503 "Live questions are paused right now — the example questions still work."
KILL SWITCH RETRIEVAL_ONLY  -> 503 "Live questions are limited to cached answers right now — the example questions still work."
KILL SWITCH OFF             -> 403 "Bot check failed — reload the page and try again."
cached example N1           -> 200 event: done {"event": "done", "cached": true, ...}
kill switch: off; ledger today paid 0 / cached 1, $0.0; all time paid 29 / cached 10, $0.0462
```

## Ledger rows by IP-hash version (one read-only query on the serve machine, after the cached ask)

```
ip_hash_v=2     cached=True   has_hash=True  rows=1
ip_hash_v=None  cached=True   has_hash=True  rows=9
ip_hash_v=None  cached=False  has_hash=True  rows=29
legacy (unsalted, no ip_hash_v): 38     <- decision 22, not run
```

## Pre-push checks for `6c77042..2afdd6e`

- `/security-review` (Opus): no finding at confidence >= 8.
- `gitleaks git --log-opts=6c77042..HEAD`: 2 commits scanned, no leaks found. No lockfile changed.
- CI run 42164683354: security-scan, neo4j-state (333 passed), unit (9520 passed, 468 skipped), deepeval-wrapper (24 passed),
  serve-shipped (5865 passed, 213 skipped) all success.

## Independent verification (two Opus agents, read-only)

- Live re-check: PASS_WITH_NOTES (healthy; fp32; kill switch off; pepper Deployed, nothing Staged; no ERROR, Traceback, OOM or
  restart in the log window; Neo4j 100ms). Note: Neo4j's stop before the restart was not clean (VM exited 5 s after SIGTERM, journal
  recovered on boot).
- Record audit: PASS_WITH_NOTES; its corrections are applied in 10.9, the M5a log, the spec and the RUNBOOK.
