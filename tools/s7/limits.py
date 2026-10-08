"""The pre-registered limits of spike S7 and the validity rules of this harness (docs/v2/M5_DECISIONS.md section 3).

The pass limits below were fixed before any run and are copied from the decisions file (a test reads the file and checks the
text); nothing here may be tuned after a result. The validity rules are the harness's own and say only whether the driver
delivered the load it claims to (they mirror the S2 generator rules: a driver above 70 % CPU voids the level).

S7 pass (per level, judged on the last 30 of its 60 minutes): pre-stream state-operation p95 <= 50 ms and p99 <= 200 ms; writes
p95 <= 100 ms; retrieval p95 <= 1.5 x its own p95 at 0.5 live asks/s; errors <= 0.1 %; zero lost ledger rows; no Neo4j restart or
out-of-memory. M5a requires level 5 (about 1.9 x the gate's 2.6 live asks/s); levels 10 and 20 chart capacity.
"""

PRE_STREAM_P95_MS = 50.0
PRE_STREAM_P99_MS = 200.0
WRITES_P95_MS = 100.0
RETRIEVAL_P95_FACTOR = 1.5
BASELINE_LIVE_RATE = 0.5                  # live asks per second of the baseline phase
ERROR_RATE_MAX = 0.001
JUDGED_WINDOW_S = 1800                    # the last 30 minutes of a 60-minute level
LEVEL_MINUTES = 60
LEVELS = (5, 10, 20)                      # live asks per second
REQUIRED_LEVEL = 5

# Harness choices (the pre-registration is silent): the baseline is judged after its first five minutes (a cold page cache is
# not "its own p95 at 0.5 live asks/s"); the whole baseline phase is also reported.
BASELINE_WARMUP_S = 300

# Validity: the driver, not the database, is the bottleneck if any of these holds.
DRIVER_CPU_MAX = 0.70                     # share of the driver's one core (a performance-1x machine)
OFFERED_RATE_FLOOR = 0.95                 # live asks started / live asks asked for
ARRIVAL_LAG_P99_MAX_S = 1.0               # an ask started more than this late means the driver queued the load

# Which ops form each gated group. "Pre-stream" is every state operation an ask makes before its stream (or its cached replay)
# starts; "writes" is every write the state makes; "retrieval" is the whole graph read of one live ask.
GROUPS = {
    "pre_stream": ("cache_read", "reserve", "cached_log", "policy_read", "count_read"),
    "writes": ("reserve", "settle", "cache_put", "cached_log", "renew", "paid_log"),
    "retrieval": ("retrieval",),
}
# Reported per op but gated by no limit: the maintenance thread's own reads and the sweep.
BACKGROUND_OPS = ("kill_read", "sweep")
