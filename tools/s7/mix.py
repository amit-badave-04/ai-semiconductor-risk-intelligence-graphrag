"""The operation mixes, the W3 schedule and the open-loop arrivals of spike S7 (docs/v2/M5_DECISIONS.md section 3).

The mixes are the pre-registered ones:

* ``M5A``: per live ask 1 cache read, 1 reserve write, about 5 retrieval reads (counted, not assumed: ``hybrid_retrieve`` is the
  real function) and 2 writes after (the settle and the cache write); per cached ask 1 cache read and 1 ledger row. The
  maintenance thread adds the kill-level read (10 s), the lease renewals and the sweep (15 s).
* ``TODAY``: the control, the flow the live site ran before M5a (git ``bfd7b71~1``): the kill-switch policy read and the day's paid
  count on every live ask, no lease, the ledger row and the cache write after.

Cached asks arrive at the gate ratio (45 % cached to 55 % live, about 0.82 per live ask). Arrivals are OPEN LOOP: a seeded
Poisson process, scheduled in advance, so a slow database cannot slow the offered load down (a closed loop hides queueing).
"""

import heapq
import random
from collections.abc import Iterator
from dataclasses import dataclass

from tools.s7 import limits

CACHED_PER_LIVE = 45 / 55
SEED_MINUTES = 20                          # restoring the clone and seeding the examples: not a replay phase


@dataclass(frozen=True)
class Mix:
    name: str
    live_pre: tuple[str, ...]              # before the stream: the ops of the route and the retrieval
    live_post: tuple[str, ...]             # after it
    cached: tuple[str, ...]
    leases: bool                           # a lease is held for the stream and renewed by the maintenance thread


M5A = Mix("m5a", live_pre=("cache_read", "reserve", "retrieval"), live_post=("settle", "cache_put"),
          cached=("cache_read", "cached_log"), leases=True)
TODAY = Mix("today", live_pre=("cache_read", "policy_read", "count_read", "retrieval"), live_post=("paid_log", "cache_put"),
            cached=("cache_read", "cached_log"), leases=False)
MIXES = {"m5a": M5A, "today": TODAY}


@dataclass(frozen=True)
class Phase:
    name: str
    mix: str
    live_rate: float
    minutes: int
    judged_s: int | None                   # None: not judged (the soak only drains the burst balance)


PHASES = {p.name: p for p in (
    Phase("baseline", "m5a", limits.BASELINE_LIVE_RATE, 15, 15 * 60 - limits.BASELINE_WARMUP_S),
    Phase("soak", "m5a", 5, 120, None),
    Phase("L5", "m5a", 5, limits.LEVEL_MINUTES, limits.JUDGED_WINDOW_S),
    Phase("control", "today", 5, 30, limits.JUDGED_WINDOW_S),
    Phase("L10", "m5a", 10, limits.LEVEL_MINUTES, limits.JUDGED_WINDOW_S),
    Phase("L20", "m5a", 20, limits.LEVEL_MINUTES, limits.JUDGED_WINDOW_S),
)}


def _stream(rate: float, duration_s: float, rng: random.Random, kind: str) -> Iterator[tuple[float, str]]:
    if rate <= 0:
        return
    t = rng.expovariate(rate)
    while t < duration_s:
        yield t, kind
        t += rng.expovariate(rate)


def arrivals(live_rate: float, cached_rate: float, duration_s: float, seed: int) -> Iterator[tuple[float, str]]:
    """``(seconds from the start, "live" | "cached")`` in time order. Two independent seeded Poisson streams, merged."""
    if live_rate < 0 or cached_rate < 0 or duration_s < 0:
        raise ValueError("rates and the duration must not be negative")
    live = _stream(live_rate, duration_s, random.Random(f"{seed}:live"), "live")
    cached = _stream(cached_rate, duration_s, random.Random(f"{seed}:cached"), "cached")
    yield from heapq.merge(live, cached)
