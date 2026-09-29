"""M4 step 0: the global daily upload budget is service state (``SvcUploadDay``), taken atomically (docs/v2/M4_PLAN.md 3, 4.4).

``MAX_UPLOADS_PER_DAY`` bounds the CPU the machine spends on uploads (40 x ~190 CPU-s stays below the shared-cpu-2x baseline).
Two uploads racing for the last slot must not both get it: the statement writes a lock property BEFORE it reads the count.
"""

import pytest

from semigraph.serve import store


class FakeCypher:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, driver, query, **params):
        self.calls.append((query, params))
        return self.rows


@pytest.mark.parametrize("rows,taken", [([{"n": 1}], True), ([{"n": 40}], True), ([], False)])
def test_a_slot_is_taken_only_while_the_day_is_below_the_limit(monkeypatch, rows, taken):
    fake = FakeCypher(rows)
    monkeypatch.setattr(store, "run_cypher", fake)
    assert store.reserve_daily_upload(object(), 40) is taken
    (query, params), = fake.calls
    assert params["limit"] == 40 and params["day"] == store._today()
    assert "MERGE (c:SvcUploadDay {day: $day})" in query


def test_the_counter_node_is_locked_before_its_count_is_read(monkeypatch):
    fake = FakeCypher([{"n": 1}])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.reserve_daily_upload(object(), 40)
    query = fake.calls[0][0]
    assert query.index("SET c._lock = true") < query.index("c.n < $limit") < query.index("SET c.n = c.n + 1")


def test_a_zero_or_negative_limit_takes_nothing_and_asks_nothing(monkeypatch):
    fake = FakeCypher([{"n": 1}])
    monkeypatch.setattr(store, "run_cypher", fake)
    assert store.reserve_daily_upload(object(), 0) is False and fake.calls == []


def test_the_counter_is_unique_per_day_so_merge_cannot_duplicate_it(monkeypatch):
    fake = FakeCypher([])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.ensure_indexes(object())
    assert any("FOR (u:SvcUploadDay) REQUIRE u.day IS UNIQUE" in q for q, _ in fake.calls)
