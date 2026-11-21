"""RT-006 (Low, CWE-770): ``guard.RateLimiter`` never frees its buckets (red-team finding, regression test).

``allow()`` always leaves the key's deque non-empty (it appends ``now``), and the cleanup that runs once the map holds
more than ``MAX_BUCKETS`` keys drops only EMPTY deques. A deque is pruned only when its own key is seen again, so a
stream of one-shot keys (one per spoofed or rotating address) grows the map without bound, and every ``allow()`` past
``MAX_BUCKETS`` then scans the whole map.

Only ``MAX_BUCKETS + 2`` calls are made: once the map is over the bound every call is a full scan, so a 25,000-key loop
would take tens of seconds without showing anything more.
"""

import pytest

from semigraph.serve import guard
from semigraph.serve.guard import RateLimiter

WINDOW_S = 60


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(guard.time, "monotonic", fake)
    return fake


def test_stale_one_shot_buckets_are_freed_once_the_map_is_over_the_bound(clock):
    limiter = RateLimiter(max_events=5, window_seconds=WINDOW_S)
    for i in range(guard.MAX_BUCKETS + 1):                       # the last call is over the bound and scans: nothing stale yet
        assert limiter.allow(f"one-shot-{i}")
    clock.now += WINDOW_S + 1                                    # every one of those windows has now expired
    assert limiter.allow("one-more")
    assert len(limiter._buckets) <= guard.MAX_BUCKETS, (
        f"{len(limiter._buckets)} buckets kept after every window expired (bound {guard.MAX_BUCKETS})")


def test_the_cleanup_never_forgets_a_key_still_inside_its_window(clock):
    """The control: freeing memory must not reset a live window (that would let a key flood clear a victim's limit)."""
    limiter = RateLimiter(max_events=1, window_seconds=WINDOW_S)
    assert limiter.allow("victim")
    clock.now += 1
    for i in range(guard.MAX_BUCKETS + 1):
        limiter.allow(f"flood-{i}")
    clock.now += 1                                               # still inside the victim's window
    assert not limiter.allow("victim"), "the victim's window was reset by the cleanup"
