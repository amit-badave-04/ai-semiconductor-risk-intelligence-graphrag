"""F2: RateLimiter memory bound. A key flood on a day- or hour-window limiter must not grow the map past MAX_BUCKETS for
a whole window, and eviction must not sort the whole map. Fake clock, no sleeping."""

from __future__ import annotations

import pytest

from semigraph.serve import guard

BOUND = 10
DAY = 86_400


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(guard.time, "monotonic", c)
    monkeypatch.setattr(guard, "MAX_BUCKETS", BOUND)
    return c


def _flood(limiter, clock, n, prefix="k"):
    for i in range(n):
        clock.now += 0.01                       # the whole flood stays inside the minimum sweep interval
        limiter.allow(f"{prefix}{i}")


def test_f2_a_day_window_flood_is_back_under_the_bound_after_the_minimum_interval(clock):
    limiter = guard.RateLimiter(5, DAY)
    _flood(limiter, clock, 3 * BOUND)
    clock.now += guard.MIN_SWEEP_INTERVAL_S + 0.1
    limiter.allow("new")
    assert len(limiter._buckets) <= BOUND


def test_f2_expired_keys_are_always_removed(clock):
    limiter = guard.RateLimiter(5, 100)
    _flood(limiter, clock, 3)
    clock.now += 101
    limiter.allow("fresh")
    assert set(limiter._buckets) == {"fresh"}


def test_f2_a_live_key_is_never_removed_while_the_map_is_at_or_under_the_bound(clock):
    limiter = guard.RateLimiter(5, 100)
    _flood(limiter, clock, BOUND - 1, "live")
    clock.now += 95
    for i in range(BOUND - 1):
        limiter.allow(f"live{i}")
    clock.now += 6                               # past one window since the last sweep: a sweep runs now
    limiter.allow("extra")                       # BOUND keys, exactly at the bound
    assert {f"live{i}" for i in range(BOUND - 1)} <= set(limiter._buckets)


def test_f2_over_the_bound_the_longest_idle_keys_go_and_a_just_used_key_survives(clock):
    limiter = guard.RateLimiter(5, DAY)
    _flood(limiter, clock, 2 * BOUND)
    clock.now += guard.MIN_SWEEP_INTERVAL_S + 0.1
    limiter.allow("k0")                          # the oldest key, used again just now
    assert len(limiter._buckets) <= BOUND
    assert "k0" in limiter._buckets and "k1" not in limiter._buckets
    assert f"k{2 * BOUND - 1}" in limiter._buckets


def test_f2_allow_never_sorts_the_whole_map(clock, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("sorted() called")
    monkeypatch.setattr(guard, "sorted", boom, raising=False)
    limiter = guard.RateLimiter(5, DAY)
    _flood(limiter, clock, 5 * BOUND)
    clock.now += guard.MIN_SWEEP_INTERVAL_S + 0.1
    limiter.allow("new")
    assert len(limiter._buckets) <= BOUND
