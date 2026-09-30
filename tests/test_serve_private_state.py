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


def test_m4_settings_default_off_with_the_pre_registered_caps():
    """docs/v2/M4_PLAN.md section 3 revision 2 (owner-approved: ~2x the single-core option) and section 6 step 0.4."""
    from semigraph.config import Settings

    s = Settings(_env_file=None)
    assert s.freshness_enabled is False and s.uploads_enabled is False
    assert (s.freshness_poll_hours, s.freshness_boot_delay_s) == (6, 300)
    assert s.workspace_ttl_hours == 24
    assert s.upload_max_bytes == 15 * 1024 * 1024
    assert (s.upload_max_pages, s.upload_max_tokens, s.upload_max_chunk_tokens, s.upload_max_chunks) == (30, 16000, 512, 120)
    assert (s.upload_max_documents, s.upload_max_versions) == (3, 5)
    assert (s.upload_max_workspace_pages, s.upload_max_workspace_tokens) == (120, 48000)
    assert (s.upload_parse_timeout_s, s.upload_embed_timeout_s, s.upload_compare_timeout_s) == (90, 1200, 120)
    assert (s.max_uploads_per_day, s.workspace_create_per_day, s.uploads_per_hour) == (40, 3, 10)


def test_the_counter_is_unique_per_day_so_merge_cannot_duplicate_it(monkeypatch):
    fake = FakeCypher([])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.ensure_indexes(object())
    assert any("FOR (u:SvcUploadDay) REQUIRE u.day IS UNIQUE" in q for q, _ in fake.calls)


def test_the_monitor_lease_and_result_nodes_are_unique_per_key(monkeypatch):
    """Review of M4 build: two machines' first-ever MERGE on the same key must not create two lease/result nodes."""
    fake = FakeCypher([])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.ensure_indexes(object())
    queries = [q for q, _ in fake.calls]
    assert any("FOR (l:SvcLease) REQUIRE l.key IS UNIQUE" in q for q in queries)
    assert any("FOR (f:SvcFreshness) REQUIRE f.key IS UNIQUE" in q for q in queries)
