# tools/s7: spike S7, Neo4j service state under sustained load

Question (docs/v2/M5_DECISIONS.md section 3): can the staging clone of live's Neo4j (a 1 GB `shared-cpu-1x`) carry the service
state of M5a at 5 live asks per second (about 1.9 x the gate's 2.6), with levels 10 and 20 charting capacity?

There is no API and no model in this test. A driver on its own machine sends the database the operations an ask makes, using the
service's OWN functions: the state backend (`serve/state`: reserve, reconcile, renew, sweep, the kill-level read, the answer
cache), the ledger rows (`store.log_query`), and the graph reads (`hybrid_retrieve`, with the question's vector handed in and an
embedder that fails if it is ever called). Arrivals are open loop (a seeded Poisson process on a monotonic clock), the thread
gates have the service's sizes (4 state tokens with a 1 s bounded wait, 32 graph-read tokens), and the real `MaintenanceThread`
runs beside them. Nothing is re-implemented, and no `Settings` is built (the production config refuses an app name it does not know).

| Module | What it is |
|---|---|
| `limits.py`, `mix.py` | the pre-registered limits; the operation mixes (M5a and today's control), the W3 schedule, the arrivals |
| `plan.py` | `LevelConfig` / `ReplayInputs`, the salt, the service's own default sizes |
| `flows.py` | the asks (what each one sends, in which order, under which gate), the timed-operation runner |
| `schedule.py`, `gates.py`, `counting.py`, `watch.py` | the open-loop dispatcher, the thread gates, the statement counter, the probe / CPU sampler / ledger check |
| `replay.py` | `run_level`, `prepare`, the command line, the host guard |
| `record.py`, `evidence.py`, `report.py` | the raw sample file, restarts and out-of-memory kills, the verdict (`s7.json`) |
| `vectors.py` | the pre-embedded pool (S7 measures the database, not the embedder) |

## Run order (window W3; `scripts/staging.py quote W3` first, then `create`, `seed`, `deploy W3 tools`)

```
# on the developer machine, once per pool / embedder (the staging tools machine has no embedding model):
uv run python -m tools.s7.replay vectors --pool tools/loadtest/pool.json --out tools/s7/data/vectors.json

# on the tools machine (flyctl ssh console), S7_NEO4J_PASSWORD is a Fly secret and never an argument:
python -m tools.s7.replay prepare                                  # the schema and the cached examples
python -m tools.s7.replay run --phase baseline --out /out/baseline  # 15 min at 0.5 live asks/s; judged after the first 5
python -m tools.s7.replay run --phase soak     --out /out/soak      # 120 min at 5/s: drains the burst balance (not judged)
python -m tools.s7.replay run --phase L5       --out /out/L5        # 60 min, judged on the last 30
python -m tools.s7.replay run --phase control  --out /out/control   # today's flow, 30 min (reported, not judged)
python -m tools.s7.replay run --phase L10 ...   /   --phase L20 ...

# after EACH level, not only at the end of the window: `scripts/staging.py snapshot W3 --out DIR/L5` collects the database log
# and machine events (the directory is named after the phase); then, once all levels have run,
python -m tools.s7.report /out --out s7.json --evidence DIR     # DIR/<phase>/ per level where it exists, else DIR itself
```

`flyctl logs --no-tail` returns a short recent buffer and a machine's event list is truncated too. A snapshot taken at the end of a
six-hour window does not reach back to the first levels, so the report names a log or an event list among the evidence of a
level's absence of restarts and out-of-memory kills ONLY if its earliest timestamp is at or before the level's start; otherwise a
level with no other evidence is `INCOMPLETE`, never a pass. Findings always count. The probe stream (`server.jsonl`) and the error
statuses (a Neo4j memory error is recorded with the server's own code, never its message) are evidence for the whole level.

The replay needs the 300-question pool's vectors in the image (`tools/s7/data/vectors.json`, built on the developer machine with
the production embedder, so decision 14's choice of embedder decides it). `staging.py deploy W3 tools` refuses an image without
them, or with vectors of another pool. The cached asks are drawn from the examples `prepare` can actually seed
(`replay.seedable_examples`: the service's own `seed_examples` refuses an example whose stored checks failed).

Every phase has its own block of 100,000 salt counters (`replay.SALT_BLOCK`); a rerun of a phase on the same database needs a new
`--salt-start` or `staging.py reset-ledger W3 --cache`, or its questions would be answered from the previous run's cache.

### Data growth across levels (an open decision, not made here)

Each level runs on what the earlier ones left: every ask adds a ledger row and a live ask adds a cached answer, so L20 runs on a
database holding the baseline, the soak, L5 and L10's rows (tens of thousands), and the control's day count (`count_read`) includes
every row the replay wrote that day, a population live never has. The pre-registration is silent on whether the database is reset
between levels. W3 has no API, so `staging.py reset-ledger W3 --cache` can run between levels at any time; whether it does is the
OWNER's decision. Each `level.json` records `table_counts` (the ledger and the cache, at the level's start and end) so the report
shows what each level ran on.

## What the report decides

`VOID` (the driver was the bottleneck: its CPU, the rate it offered, how late it started asks, a crashed job), else `FAIL` (any
pre-registered limit missed), else `INCOMPLETE` (a criterion could not be judged: no baseline, no probe or log evidence), else
`PASS`. M5a needs level 5. Nothing is tuned after a result.

## Not proven offline

The Cypher the replay itself sends (the uptime probe `dbms.queryJmx`, the ledger check) and the service's own statements against a
real server were run only against a fake driver. `tests/test_tools_s7.py::test_a_level_against_a_throwaway_neo4j` does it for a
throwaway server (opt-in: `S7_ITEST_URI=bolt://localhost:7898`, never 7687 or 7699). If the server refuses `dbms.queryJmx` the
probe falls back to `RETURN 1` and records no uptime; the report then cannot call a restart from the probes alone and says so.
