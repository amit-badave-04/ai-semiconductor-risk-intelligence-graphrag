"""The Cypher of the state package (``serve/state/ledger.py``), pinned with a recording driver: no server needed.

What is pinned here is the SHAPE of each statement, because the properties that matter (one winner for the last slot,
one charge per lease) hold only if the statement takes its locks BEFORE it reads what it decides on:

- reserve: the day counter and the per-IP counter are locked (``SET _lock``) before the first ``WHERE``;
- settle / renew / expire: the row is locked before its ``status`` is checked, so two transactions that matched the row
  while it was still ``reserved`` cannot both settle it (the lost-update pattern ``store.reserve_daily_upload``
  documents);
- every statement runs as a managed transaction function carrying a server-side timeout.

The server-side behaviour itself is proven against a real Neo4j in ``tests/integration/test_state_neo4j.py`` and in
``tests/test_state_contract.py`` (both opt-in).
"""

import logging
import re
import time
from collections import namedtuple

import pytest
from neo4j.exceptions import ServiceUnavailable
from test_state_inprocess import FakeClock, state_settings

from semigraph.serve.state import (
    Denied,
    Lease,
    StateConfig,
    StateDrivers,
    StateUnavailable,
    ledger,
    settle_queue,
    usd_to_micro,
)
from semigraph.serve.state.neo4j import Neo4jBackend

Statement = namedtuple("Statement", "text params kind timeout")


class RecordingTx:
    def __init__(self, driver, kind, timeout):
        self._driver, self._kind, self._timeout = driver, kind, timeout

    def run(self, query, parameters=None, **kwargs):
        text = getattr(query, "text", query)
        params = {**(parameters or {}), **kwargs}
        self._driver.statements.append(Statement(text, params, self._kind, self._timeout))
        return list(self._driver.respond(text, params))


class RecordingSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute_write(self, fn, *args, **kwargs):
        self._driver.transactions.append("write")
        return fn(RecordingTx(self._driver, "write", getattr(fn, "timeout", None)), *args, **kwargs)

    def execute_read(self, fn, *args, **kwargs):
        self._driver.transactions.append("read")
        return fn(RecordingTx(self._driver, "read", getattr(fn, "timeout", None)), *args, **kwargs)

    def run(self, query, parameters=None, **kwargs):
        return RecordingTx(self._driver, "auto", None).run(query, parameters, **kwargs)


class RecordingDriver:
    """Records every statement with the kind of transaction it ran in and that transaction's server-side timeout.
    ``respond(text, params)`` returns the rows a statement answers with (default none)."""

    def __init__(self, respond=None):
        self.statements: list[Statement] = []
        self.transactions: list[str] = []
        self.respond = respond or (lambda text, params: [])

    def session(self, **config):
        return RecordingSession(self)


def first_where(text: str) -> int:
    return re.search(r"\bWHERE\b", text).start()


def cap_where(text: str) -> str:
    """The WHERE that applies the caps: from the aggregate that counts in-flight to the first increment."""
    return text[text.index("WITH c, i, count(r)"):text.index("SET c.paid = c.paid + 1")]


CAPS = ledger.Caps(max_count=150, max_spend_micro=10_000_000, max_per_ip=20, max_inflight=2)
# A share of this file's own, NOT the production one: the boundary cases of the denial reasons below (500_000 + 750_000 and
# the like) are written against it. The backend tests further down take the share from ``state_settings`` instead.
SHARE_MICRO = 1_250_000
CAPS_WITH_SHARE = ledger.Caps(max_count=150, max_spend_micro=10_000_000, max_per_ip=20, max_inflight=2,
                              max_share_micro=SHARE_MICRO)


def reserve(driver, **overrides):
    kwargs = dict(lease_id="lease-1", day="2026-10-06", ip_hash="ab12", ip_hash_v=2, strategy="hybrid",
                  workspace=False, estimate_micro=60_000, now_wall=1_790_000_000.0, lease_until=1_790_000_060.0,
                  machine_id="m1", caps=CAPS, timeout_s=1.0)
    return ledger.reserve_counted(driver, **{**kwargs, **overrides})


# ---------------------------------------------------------------------------------------------------- reserve

def test_reserve_locks_both_counters_before_any_where():
    driver = RecordingDriver(lambda text, params: [{"id": "lease-1"}] if "CREATE (q:SvcQuery" in text else [])
    reserve(driver)
    text = driver.statements[0].text
    assert text.index("MERGE (c:SvcDayCounter") < text.index("SET c._lock = true")
    assert text.index("SET c._lock = true") < first_where(text)
    assert text.index("MERGE (i:SvcIpDay") < text.index("SET i._lock = true") < first_where(text)
    # nothing is read before both locks are held
    assert text.index("SET i._lock = true") < text.index("OPTIONAL MATCH")


def test_reserve_counts_inflight_over_unexpired_reserved_rows_with_no_day_filter():
    driver = RecordingDriver(lambda text, params: [{"id": "lease-1"}] if "CREATE (q:SvcQuery" in text else [])
    reserve(driver)
    text = driver.statements[0].text
    clause = text[text.index("OPTIONAL MATCH"):text.index("WITH c, i, count(r)")]
    assert "status: 'reserved'" in clause and "lease_until > $now" in clause
    assert "day" not in clause


def test_reserve_applies_all_four_caps_in_one_where_and_writes_the_increments_after_it():
    driver = RecordingDriver(lambda text, params: [{"id": "lease-1"}] if "CREATE (q:SvcQuery" in text else [])
    reserve(driver)
    text = driver.statements[0].text
    where = cap_where(text)
    for needle in ("$max_count", "$max_spend", "$max_ip", "$max_inflight"):
        assert needle in where
    assert "c.spend_micro + $estimate" in where
    assert text.index("SET c.paid = c.paid + 1") < text.index("CREATE (q:SvcQuery")


def test_reserve_checks_the_share_after_both_locks_and_returns_the_new_counters():
    """Council 4: the address's share is one more term of the SAME where that applies the other caps, so it is decided on
    counters both locks protect, and the increments and the row are written in that transaction. The new day counters come
    back from the same statement (the warnings need them) and are read after the increments."""
    driver = RecordingDriver(lambda text, params: [{"id": "lease-1", "paid": 3, "spend_micro": 180_000}]
                             if "CREATE (q:SvcQuery" in text else [])
    outcome = reserve(driver, caps=CAPS_WITH_SHARE)
    text, params = driver.statements[0].text, driver.statements[0].params
    where = cap_where(text)
    assert text.index("SET i._lock = true") < text.index("$max_share") < text.index("SET c.paid = c.paid + 1")
    assert "$max_share = 0 OR" in where                                           # 0 turns the check off
    assert "coalesce(i.spend_micro, 0) + $estimate <= $max_share" in where          # `<=`, as for the day's spend
    assert where.index("$max_ip") < where.index("$max_share") < where.index("inflight < $max_inflight")
    increments = text[text.index("SET c.paid = c.paid + 1"):text.index("CREATE (q:SvcQuery")]
    assert "i.spend_micro = coalesce(i.spend_micro, 0) + $estimate" in increments
    returned = text[text.index("CREATE (q:SvcQuery"):]
    assert "RETURN q.id AS id" in returned and "c.paid AS paid" in returned
    assert "c.spend_micro AS spend_micro" in returned
    assert params["max_share"] == SHARE_MICRO and params["estimate"] == 60_000
    assert outcome.granted and (outcome.paid, outcome.spend_micro) == (3, 180_000)


def test_a_new_ip_node_starts_its_spend_at_zero_and_an_old_one_without_the_property_is_read_as_zero():
    text = ledger.RESERVE_COUNTED
    assert "ON CREATE SET i.paid = 0, i.spend_micro = 0" in text
    assert "i.spend_micro + $estimate" not in text                  # a node written before the share has no property


def test_reserve_creates_the_row_with_the_columns_the_existing_readers_know_plus_the_new_ones():
    driver = RecordingDriver(lambda text, params: [{"id": "lease-1"}] if "CREATE (q:SvcQuery" in text else [])
    outcome = reserve(driver)
    assert outcome == ledger.ReserveOutcome(True, None)
    text = driver.statements[0].text
    create = text[text.index("CREATE (q:SvcQuery"):]
    for column in ("id:", "day:", "ts:", "created_at:", "status: 'reserved'", "cached: false", "strategy:",
                   "workspace:",
                   "ip_hash:", "ip_hash_v:", "estimate_micro:", "lease_until:", "machine_id:"):
        assert column in create
    params = driver.statements[0].params
    assert params["estimate"] == 60_000 and isinstance(params["estimate"], int)
    assert params["max_spend"] == 10_000_000 and params["max_inflight"] == 2
    assert params["created_at"].startswith("2026-")        # ISO text, as store.log_query writes it
    assert params["now"] == 1_790_000_000.0 and params["lease_until"] == 1_790_000_060.0


@pytest.mark.parametrize("snapshot, reason", [
    ({"paid": 150, "spend_micro": 0, "ip_paid": 0, "inflight": 0}, "daily_count"),
    ({"paid": 3, "spend_micro": 9_950_000, "ip_paid": 0, "inflight": 0}, "daily_spend"),
    ({"paid": 3, "spend_micro": 0, "ip_paid": 20, "inflight": 0}, "ip_daily"),
    ({"paid": 3, "spend_micro": 0, "ip_paid": 0, "inflight": 2}, "inflight"),
    # several caps hit at once: the first in the documented order names the denial
    ({"paid": 150, "spend_micro": 9_999_999, "ip_paid": 20, "inflight": 9}, "daily_count"),
    ({"paid": 3, "spend_micro": 9_999_999, "ip_paid": 20, "inflight": 9}, "daily_spend"),
])
def test_a_denied_reserve_names_its_cap_from_a_read_in_the_same_transaction(snapshot, reason):
    def respond(text, params):
        return [snapshot] if "RETURN c.paid AS paid" in text else []

    driver = RecordingDriver(respond)
    outcome = reserve(driver)
    assert outcome == ledger.ReserveOutcome(False, reason)
    assert driver.transactions == ["write"]                       # the read ran inside the locked write transaction
    assert [s.kind for s in driver.statements] == ["write", "write"]


QUIET = {"paid": 3, "spend_micro": 0, "ip_paid": 1, "inflight": 0, "ip_spend_micro": 0, "ip_live_micro": 0}


@pytest.mark.parametrize("snapshot, estimate, reason", [
    # settled spend 700_000 + the estimate already passes the share: it is used up
    (dict(ip_spend_micro=700_000, ip_live_micro=0), 580_089, "ip_spend"),
    # 600_000 of the 700_000 is running: settled 100_000 + the estimate fits, only the running ask pushes it over
    (dict(ip_spend_micro=700_000, ip_live_micro=600_000), 580_089, "ip_spend_inflight"),
    # the boundary: settled + estimate equal to the share fits (inflight), one micro-dollar over does not (settled)
    (dict(ip_spend_micro=900_000, ip_live_micro=400_000), 750_000, "ip_spend_inflight"),
    (dict(ip_spend_micro=900_000, ip_live_micro=400_000), 750_001, "ip_spend"),
    # the order of the documented caps: the per-address count first, then the share, then in-flight
    (dict(ip_spend_micro=700_000, ip_paid=20), 580_089, "ip_daily"),
    (dict(ip_spend_micro=700_000, inflight=2), 580_089, "ip_spend"),
    (dict(ip_spend_micro=100_000, inflight=2), 580_089, "inflight"),
    # a node written before the share existed has no property at all
    (dict(ip_spend_micro=None, ip_live_micro=None, inflight=2), 580_089, "inflight"),
])
def test_a_denied_reserve_names_the_share_and_whether_the_address_is_settled_or_only_inflight(snapshot, estimate, reason):
    driver = RecordingDriver(lambda text, params: [{**QUIET, **snapshot}] if "RETURN c.paid AS paid" in text else [])
    outcome = reserve(driver, caps=CAPS_WITH_SHARE, estimate_micro=estimate)
    assert outcome == ledger.ReserveOutcome(False, reason)


def test_a_zero_share_never_names_the_share():
    no_share = ledger.Caps(max_count=150, max_spend_micro=10_000_000, max_per_ip=20, max_inflight=2)
    driver = RecordingDriver(lambda text, params: [{**QUIET, "ip_spend_micro": 10**9, "inflight": 2}]
                             if "RETURN c.paid AS paid" in text else [])
    assert reserve(driver, caps=no_share).reason == "inflight"
    assert driver.statements[0].params["max_share"] == 0


def test_the_denial_read_sums_this_addresses_live_leases_in_a_stage_of_its_own():
    """A second OPTIONAL MATCH in the stage that counts the in-flight rows would multiply ``count(r)`` by the number of
    this address's leases; the sum is taken after that count is aggregated."""
    text = ledger.DENIAL_READ
    counted = text.index("WITH c, i, count(r) AS inflight")
    own = text.index("OPTIONAL MATCH", counted)
    assert text.index("OPTIONAL MATCH (r:SvcQuery") < counted < own < text.index("RETURN")
    clause = text[own:text.index("RETURN")]
    for needle in ("status: 'reserved'", "day: $day", "ip_hash: $ip_hash", "lease_until > $now"):
        assert needle in clause
    returned = text[text.index("RETURN"):]
    assert "c.paid AS paid" in returned and "i.spend_micro AS ip_spend_micro" in returned
    assert "sum(p.estimate_micro)" in returned and "AS ip_live_micro" in returned and "inflight" in returned


def test_a_zero_cap_is_off_except_for_inflight_where_zero_means_nothing_is_allowed():
    no_caps = ledger.Caps(max_count=0, max_spend_micro=0, max_per_ip=0, max_inflight=0)
    driver = RecordingDriver(lambda text, params: [{"paid": 10**6, "spend_micro": 10**12, "ip_paid": 10**6,
                                                    "inflight": 0}] if "RETURN c.paid AS paid" in text else [])
    outcome = reserve(driver, caps=no_caps)
    assert outcome == ledger.ReserveOutcome(False, "inflight")    # 0 in flight is not below a limit of 0
    where = cap_where(driver.statements[0].text)
    assert "$max_count = 0 OR" in where and "$max_spend = 0 OR" in where and "$max_ip = 0 OR" in where
    assert "inflight < $max_inflight" in where and "$max_inflight = 0" not in where


# --------------------------------------------------------------------------------- lock the row, then check it

LOCK_THEN_CHECK = [
    ("settle_counted", lambda d: ledger.settle_counted(d, "lease-1", outcome="done", usage={"prompt_tokens": 3},
                                                       cost_micro=40_000, now_wall=1_790_000_100.0, timeout_s=1.0)),
    ("settle_row", lambda d: ledger.settle_row(d, "lease-1", outcome="done", usage=None, actual_micro=40_000,
                                               now_wall=1_790_000_100.0, timeout_s=1.0)),
    ("renew_row", lambda d: ledger.renew_row(d, "lease-1", lease_until=1_790_000_160.0, timeout_s=1.0)),
    ("expire_reserved", lambda d: ledger.expire_reserved(d, now_wall=1_790_000_100.0, grace_s=30.0, machine_id="m1",
                                                         timeout_s=1.0)),
]


@pytest.mark.parametrize("name, call", LOCK_THEN_CHECK, ids=[n for n, _ in LOCK_THEN_CHECK])
def test_a_reserved_row_is_locked_before_its_status_is_checked(name, call):
    driver = RecordingDriver()
    call(driver)
    text = driver.statements[0].text
    assert text.index("SET q._lock = true") < text.index("REMOVE q._lock") < text.index("q.status = 'reserved'")


def test_settle_counted_takes_the_day_counter_lock_before_the_row_lock_and_floors_the_spend_at_zero():
    driver = RecordingDriver()
    ledger.settle_counted(driver, "lease-1", outcome="done", usage=None, cost_micro=None, now_wall=1.0, timeout_s=1.0)
    text = driver.statements[0].text
    assert text.index("SET c._lock = true") < text.index("SET q._lock = true")     # the same order every writer uses
    assert "CASE WHEN" in text and "THEN 0" in text                                  # floor at zero
    # an unknown cost keeps the estimate
    assert "coalesce($cost_micro, q.estimate_micro)" in text


def test_settle_counted_adjusts_the_ip_counter_in_the_day_then_ip_then_row_order():
    """The address's spend moves with the day's, by the same ``actual - estimate`` and the same floor, under the lock order
    every writer uses: the day counter, then the address's counter, then the row (whose status is checked last)."""
    driver = RecordingDriver()
    ledger.settle_counted(driver, "lease-1", outcome="done", usage=None, cost_micro=40_000, now_wall=1.0, timeout_s=1.0)
    text = driver.statements[0].text
    assert text.index("SET c._lock = true") < text.index("SET i._lock = true") < text.index("SET q._lock = true")
    assert text.index("SET q._lock = true") < text.index("REMOVE q._lock") < text.index("WHERE q.status = 'reserved'")
    # the node is only matched: a settle never creates a counter for a lease whose reserve did not (the rollback from the
    # other backend), and a null address on the row must not make the statement fail (MERGE refuses a null property)
    assert "OPTIONAL MATCH (i:SvcIpDay {day: q.day, ip_hash: q.ip_hash})" in text and "MERGE (i:SvcIpDay" not in text
    adjust = text[text.index("i.spend_micro = "):]
    assert "CASE WHEN" in adjust and "THEN 0" in adjust and "actual - q.estimate_micro" in adjust
    assert "coalesce(i.spend_micro, 0)" in adjust                       # a node written before the share has none
    assert text.index("coalesce($cost_micro, q.estimate_micro)") < text.index("i.spend_micro = ")


def test_settle_counted_is_idempotent_by_matching_nothing_the_second_time():
    seen = []

    def respond(text, params):
        seen.append(text)
        return [{"id": "lease-1"}] if len(seen) == 1 else []

    driver = RecordingDriver(respond)
    args = dict(outcome="done", usage=None, cost_micro=1, now_wall=1.0, timeout_s=1.0)
    assert ledger.settle_counted(driver, "lease-1", **args) is True
    assert ledger.settle_counted(driver, "lease-1", **args) is False


# ------------------------------------------------------------------------------------------------ the bound

CALLS_WITH_A_TIMEOUT = [
    ("reserve_counted", lambda d, t: reserve(d, timeout_s=t)),
    ("reserve_row", lambda d, t: ledger.reserve_row(d, lease_id="x", day="2026-10-06", ip_hash="ab", ip_hash_v=2,
                                                    strategy="hybrid", workspace=False, estimate_micro=1, now_wall=1.0,
                                                    lease_until=2.0, machine_id="m", timeout_s=t)),
    ("settle_counted", lambda d, t: ledger.settle_counted(d, "x", outcome="done", usage=None, cost_micro=1,
                                                          now_wall=1.0, timeout_s=t)),
    ("settle_row", lambda d, t: ledger.settle_row(d, "x", outcome="done", usage=None, actual_micro=1, now_wall=1.0,
                                                  timeout_s=t)),
    ("renew_row", lambda d, t: ledger.renew_row(d, "x", lease_until=2.0, timeout_s=t)),
    ("expire_reserved", lambda d, t: ledger.expire_reserved(d, now_wall=1.0, grace_s=1.0, machine_id="m", timeout_s=t)),
    ("expired_lease_ids", lambda d, t: ledger.expired_lease_ids(d, machine_id="m", now_wall=1.0, skip=(), limit=5,
                                                                timeout_s=t)),
    ("day_sums", lambda d, t: ledger.day_sums(d, day="2026-10-06", now_wall=1.0, machine_id="m", ip_hash_v=2,
                                              timeout_s=t)),
    ("sync_day_counters", lambda d, t: ledger.sync_day_counters(d, day="2026-10-06", now_wall=1.0, machine_id="m",
                                                                ip_hash_v=2, timeout_s=t)),
    ("snapshot_counted", lambda d, t: ledger.snapshot_counted(d, day="2026-10-06", now_wall=1.0, timeout_s=t)),
    ("run_read", lambda d, t: ledger.run_read(d, "RETURN 1 AS n", timeout_s=t)),
    ("run_write", lambda d, t: ledger.run_write(d, "RETURN 1 AS n", timeout_s=t)),
]


@pytest.mark.parametrize("name, call", CALLS_WITH_A_TIMEOUT, ids=[n for n, _ in CALLS_WITH_A_TIMEOUT])
def test_every_statement_runs_as_a_managed_transaction_with_the_server_side_timeout(name, call):
    def respond(text, params):
        # rows shaped like each statement's answer, so no call fails on an empty reply
        return [{"paid": 0, "spend_micro": 0, "ip_paid": 0, "inflight": 0, "n": 1, "foreign": 0, "id": "x",
                 "ip": "ab", "ts": 1.0, "cost": 0}]

    driver = RecordingDriver(respond)
    call(driver, 0.75)
    assert driver.statements, name
    assert {s.kind for s in driver.statements} <= {"read", "write"}      # no auto-commit statement (it has no timeout)
    assert {s.timeout for s in driver.statements} == {0.75}


@pytest.mark.parametrize("name, call", CALLS_WITH_A_TIMEOUT, ids=[n for n, _ in CALLS_WITH_A_TIMEOUT])
@pytest.mark.parametrize("bad", [0, -1.0])
def test_a_non_positive_timeout_is_refused_because_the_server_reads_zero_as_no_timeout(name, call, bad):
    driver = RecordingDriver()
    with pytest.raises(ValueError, match="timeout"):
        call(driver, bad)
    assert driver.statements == []


# ------------------------------------------------------------------------------------------------ the schema

def test_ensure_state_schema_creates_the_three_constraints_and_two_indexes_idempotently():
    driver = RecordingDriver()
    ledger.ensure_state_schema(driver)
    statements = [s.text for s in driver.statements]
    assert all("IF NOT EXISTS" in text for text in statements)
    joined = "\n".join(statements)
    for name in ("svc_query_id_unique", "svc_day_counter_unique", "svc_ip_day_unique", "svc_query_status",
                 "svc_query_day_status"):
        assert name in joined
    # composite uniqueness, not NODE KEY (Enterprise)
    assert "REQUIRE (i.day, i.ip_hash) IS UNIQUE" in joined
    assert "ON (q.day, q.status)" in joined and "ON (q.status)" in joined


# --------------------------------------------------------------------------- what the boot rebuild counts

def sums_responder(text, params):
    """DAY_SUMS answers one aggregate row; IP_ROWS answers one row per ask (here: none)."""
    return [{"paid": 4, "spend_micro": 200_000, "foreign": 0}] if "count(q) AS paid" in text else []


def test_the_day_sums_count_rows_written_before_the_state_columns_existed():
    driver = RecordingDriver(sums_responder)
    ledger.day_sums(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=2, timeout_s=1.0)
    sums = driver.statements[0].text
    assert "cached: false" in sums and "{day: $day" in sums
    assert "q.status IS NULL" in sums                                   # a pre-I4 ledger row has no status: it counts
    assert "q.status = 'settled'" in sums and "q.lease_until >= $now" in sums
    assert "coalesce(q.cost_micro" in sums and "q.cost_usd" in sums     # legacy rows are read at their dollar cost
    assert "q.estimate_micro" in sums                                    # a reserved row counts its estimate


def test_per_ip_rows_come_only_from_rows_made_under_the_current_pepper():
    driver = RecordingDriver(sums_responder)
    ledger.day_sums(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=2, timeout_s=1.0)
    ip_statement = driver.statements[1]
    assert "q.ip_hash_v = $ip_hash_v" in ip_statement.text and ip_statement.params["ip_hash_v"] == 2
    assert "q.ip_hash IS NOT NULL" in ip_statement.text


def test_per_ip_rows_come_with_the_same_micro_as_the_day_sums_count_for_them():
    """The per-address spend is rebuilt from the same rows with the same rule as the day's: a reserved row at its estimate,
    a settled one at its cost, a legacy one at its dollar cost."""
    driver = RecordingDriver(sums_responder)
    ledger.day_sums(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=2, timeout_s=1.0)
    ip_rows = driver.statements[1].text
    assert "CASE WHEN q.status = 'reserved' THEN coalesce(q.estimate_micro, 0)" in ip_rows
    assert "coalesce(q.cost_micro, toInteger(round(coalesce(q.cost_usd, 0.0) * 1000000)))" in ip_rows
    assert "AS micro" in ip_rows and "q.ip_hash AS ip" in ip_rows and "AS ts" in ip_rows
    assert "count(q) AS paid" not in ip_rows                           # the stub that answers DAY_SUMS must not catch it


def test_the_day_sums_add_up_each_addresss_spend_next_to_its_count():
    def respond(text, params):
        if "count(q) AS paid" in text:
            return [{"paid": 4, "spend_micro": 500_000, "foreign": 0}]
        return [{"ip": "aa", "ts": 1.0, "micro": 300_000}, {"ip": "aa", "ts": 2.0, "micro": 150_000},
                {"ip": "bb", "ts": 3.0, "micro": 50_000}, {"ip": "cc", "ts": None, "micro": None}]

    sums = ledger.day_sums(RecordingDriver(respond), day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=2,
                           timeout_s=1.0)
    assert sums.per_ip == {"aa": 2, "bb": 1, "cc": 1}
    assert sums.per_ip_spend == {"aa": 450_000, "bb": 50_000, "cc": 0}


def test_a_day_sums_built_without_a_per_address_spend_has_an_empty_one():
    sums = ledger.DaySums(1, 2, 0, {"a": 1}, ())                      # the positional shape the fakes use
    assert sums.per_ip_spend == {}


def test_the_boot_sync_writes_each_addresss_spend_and_zeroes_the_others():
    def respond(text, params):
        if "count(q) AS paid" in text:
            return [{"paid": 3, "spend_micro": 350_000, "foreign": 0}]
        if text == ledger.LOCK_DAY:
            return [{"paid": 9, "spend_micro": 9}]
        if "q.ip_hash AS ip" in text:
            return [{"ip": "aa", "ts": 1.0, "micro": 300_000}, {"ip": "aa", "ts": 2.0, "micro": 20_000},
                    {"ip": "bb", "ts": 3.0, "micro": 30_000}]
        return []

    driver = RecordingDriver(respond)
    result = ledger.sync_day_counters(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=2,
                                      timeout_s=1.0)
    assert result.synced and result.sums.per_ip_spend == {"aa": 320_000, "bb": 30_000}
    (write,) = [s for s in driver.statements if s.text == ledger.WRITE_IP_DAYS]
    assert write.params["rows"] == [{"ip": "aa", "n": 2, "spend": 320_000}, {"ip": "bb", "n": 1, "spend": 30_000}]
    assert "i.spend_micro = row.spend" in write.text and "i.paid = row.n" in write.text
    (zero,) = [s for s in driver.statements if s.text == ledger.ZERO_OTHER_IP_DAYS]
    assert "i.spend_micro = 0" in zero.text and "i.paid = 0" in zero.text and zero.params["ips"] == ["aa", "bb"]


def test_per_ip_rows_are_not_read_at_all_when_the_deployment_has_no_pepper_version():
    driver = RecordingDriver(lambda text, params: [{"paid": 0, "spend_micro": 0, "foreign": 0}])
    sums = ledger.day_sums(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=None, timeout_s=1.0)
    assert len(driver.statements) == 1 and sums.per_ip == {} and sums.ip_events == ()


def test_the_day_sums_split_off_the_leases_of_other_machines():
    driver = RecordingDriver(lambda text, params: [{"paid": 5, "spend_micro": 300_000, "foreign": 2}])
    sums = ledger.day_sums(driver, day="2026-10-06", now_wall=1.0, machine_id="m1", ip_hash_v=None, timeout_s=1.0)
    assert (sums.paid, sums.spend_micro, sums.foreign_leases) == (5, 300_000, 2)
    assert driver.statements[0].params["me"] == "m1"


# ------------------------------------------------------------------ the neo4j backend, against a scripted driver
# (the server-side behaviour belongs to the opt-in suites; this pins the Python around the Cypher without a server)



class Scripted(RecordingDriver):
    """Answers each ledger statement from ``answers`` (rows, or a callable ``(params) -> rows``), raising the errors
    queued
    for it first. The kill policy reads ``off`` unless ``policy`` says otherwise."""

    def __init__(self, answers=None, errors=None, policy="off"):
        super().__init__(self._answer)
        self.answers, self.errors, self.policy = answers or {}, errors or {}, policy

    def _answer(self, text, params):
        if "SvcPolicy" in text and "MATCH" in text:
            return [{"v": self.policy}]
        queue = self.errors.get(text)
        if queue:
            raise queue.pop(0)
        answer = self.answers.get(text, [])
        return answer(params) if callable(answer) else answer

    def count(self, statement):
        return sum(1 for s in self.statements if s.text == statement)

    def params_of(self, statement):
        return [s.params for s in self.statements if s.text == statement]


DEFAULT_BACKEND_SHARE_MICRO = usd_to_micro(state_settings().paid_spend_share_per_ip_usd)   # what ``neo4j_backend`` is built with


def neo4j_backend(driver, *, clock=None, **settings):
    clock = clock or FakeClock()
    backend = Neo4jBackend(StateConfig.from_settings(state_settings(**settings)), StateDrivers(state=driver),
                           wall=clock.wall, clock=clock.mono)
    backend.refresh_kill_level()
    return backend, clock


def ask_neo4j(backend, clock, **kw):
    return backend.reserve(ip_hash=kw.get("ip", "ip-1"), strategy="hybrid", workspace=False,
                           estimate_micro=kw.get("estimate", 60_000), now_wall=clock.wall(), now_mono=clock.mono())


def test_a_granted_neo4j_reserve_returns_the_lease_and_sends_the_caps_and_the_machine():
    driver = Scripted({ledger.RESERVE_COUNTED: [{"id": "x"}]})
    backend, clock = neo4j_backend(driver, max_queries_per_day=7, max_spend_usd_per_day=2.5, paid_per_ip_per_day=3,
                                   max_concurrent_answers=4, machine_id="mach-9", lease_ttl_s=45.0)
    lease = ask_neo4j(backend, clock)
    assert isinstance(lease, Lease) and lease.machine_id == "mach-9" and lease.until_mono == clock.mono() + 45
    (params,) = driver.params_of(ledger.RESERVE_COUNTED)
    assert (params["max_count"], params["max_spend"], params["max_ip"], params["max_inflight"]) == (7, 2_500_000, 3, 4)
    assert params["id"] == lease.lease_id and params["machine_id"] == "mach-9" and params["ip_hash_v"] == 2
    assert params["lease_until"] == clock.wall() + 45
    assert driver.transactions.count("write") == 1                  # ONE transaction per reserve


@pytest.mark.parametrize("reason, denied", [("daily_count", Denied.DAILY_COUNT), ("daily_spend", Denied.DAILY_SPEND),
                                            ("ip_daily", Denied.IP_DAILY), ("ip_spend", Denied.IP_SPEND),
                                            ("ip_spend_inflight", Denied.IP_SPEND_INFLIGHT),
                                            ("inflight", Denied.INFLIGHT), (None, Denied.UNAVAILABLE)])
def test_a_denial_reason_from_the_ledger_maps_to_the_enum_and_an_unexplained_one_fails_closed(reason, denied):
    snapshots = {"daily_count": {"paid": 150}, "daily_spend": {"spend_micro": 10_000_000}, "ip_daily": {"ip_paid": 20},
                 "ip_spend": {"ip_spend_micro": DEFAULT_BACKEND_SHARE_MICRO},
                 "ip_spend_inflight": {"ip_spend_micro": DEFAULT_BACKEND_SHARE_MICRO,
                                       "ip_live_micro": DEFAULT_BACKEND_SHARE_MICRO},
                 "inflight": {"inflight": 2}}
    driver = Scripted({ledger.DENIAL_READ: [{"paid": 0, "spend_micro": 0, "ip_paid": 0, "inflight": 0,
                                             **snapshots.get(reason, {})}]})
    backend, clock = neo4j_backend(driver, max_concurrent_answers=2)
    assert ask_neo4j(backend, clock) is denied


def test_the_neo4j_backend_hands_the_share_to_the_ledger_as_whole_micro_dollars_and_zero_for_off():
    driver = Scripted({ledger.RESERVE_COUNTED: [{"id": "x"}]})
    backend, clock = neo4j_backend(driver, paid_spend_share_per_ip_usd=1.25)
    ask_neo4j(backend, clock)
    assert driver.params_of(ledger.RESERVE_COUNTED)[0]["max_share"] == 1_250_000
    off = Scripted({ledger.RESERVE_COUNTED: [{"id": "x"}]})
    backend, clock = neo4j_backend(off, paid_spend_share_per_ip_usd=0)
    ask_neo4j(backend, clock)
    assert off.params_of(ledger.RESERVE_COUNTED)[0]["max_share"] == 0


def test_a_granted_neo4j_reserve_warns_once_at_half_the_day_from_the_counters_the_statement_returned(caplog):
    """The day counters come back from the reserve itself, so the warning costs no extra statement."""
    rows = iter([{"id": "a", "paid": 74, "spend_micro": 4_000_000}, {"id": "b", "paid": 75, "spend_micro": 5_000_000},
                 {"id": "c", "paid": 76, "spend_micro": 5_100_000}])
    driver = Scripted({ledger.RESERVE_COUNTED: lambda params: [next(rows)]})
    backend, clock = neo4j_backend(driver)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        for _ in range(3):
            assert isinstance(ask_neo4j(backend, clock), Lease)
    half = [r.getMessage() for r in caplog.records if "state_day_half" in r.getMessage()]
    by_kind = {("count" if "kind=count" in m else "spend"): m for m in half}
    assert len(half) == 2 and sorted(by_kind) == ["count", "spend"]
    assert "used=75" in by_kind["count"] and "used_micro=5000000" in by_kind["spend"]
    assert driver.transactions.count("write") == 3                      # no extra statement for the warning


@pytest.mark.parametrize("reason, text", [("daily_count", "reason=daily_count"), ("daily_spend", "reason=daily_spend")])
def test_a_neo4j_denial_by_a_daily_cap_logs_the_pause_once_and_a_share_denial_logs_nothing(reason, text, caplog):
    snapshots = {"daily_count": {"paid": 150}, "daily_spend": {"spend_micro": 10_000_000}}
    driver = Scripted({ledger.DENIAL_READ: [{"paid": 0, "spend_micro": 0, "ip_paid": 0, "inflight": 0,
                                             **snapshots[reason]}]})
    backend, clock = neo4j_backend(driver)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        for _ in range(3):
            ask_neo4j(backend, clock)
    paused = [r.getMessage() for r in caplog.records if "state_day_paused" in r.getMessage()]
    assert len(paused) == 1 and text in paused[0]
    share = Scripted({ledger.DENIAL_READ: [{"paid": 0, "spend_micro": 0, "ip_paid": 0, "inflight": 0,
                                            "ip_spend_micro": DEFAULT_BACKEND_SHARE_MICRO}]})
    backend, clock = neo4j_backend(share)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        caplog.clear()
        assert ask_neo4j(backend, clock) is Denied.IP_SPEND
    assert not [r for r in caplog.records if "state_day_paused" in r.getMessage()]


def test_kill_is_answered_from_memory_before_the_database_is_touched():
    driver = Scripted(policy="retrieval_only")
    backend, clock = neo4j_backend(driver)
    backend.refresh_kill_level()
    driver.statements.clear()
    assert ask_neo4j(backend, clock) is Denied.KILL
    assert driver.statements == []


def test_a_database_error_during_a_neo4j_reserve_is_unavailable_never_a_grant(caplog):
    driver = Scripted(errors={ledger.RESERVE_COUNTED: [ServiceUnavailable("down")]})
    backend, clock = neo4j_backend(driver)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        assert ask_neo4j(backend, clock) is Denied.UNAVAILABLE
    assert any("state_unavailable op=reserve" in r.message for r in caplog.records)


def test_a_bug_in_the_ledger_call_is_not_swallowed_as_unavailable():
    driver = Scripted(errors={ledger.RESERVE_COUNTED: [ValueError("a bug")]})
    backend, clock = neo4j_backend(driver)
    with pytest.raises(ValueError):
        ask_neo4j(backend, clock)


def test_neo4j_reconcile_passes_the_known_cost_or_none_for_an_unknown_one():
    driver = Scripted({ledger.SETTLE_COUNTED: [{"id": "l"}]})
    backend, _ = neo4j_backend(driver)
    assert backend.reconcile("l", outcome="done", usage={"prompt_tokens": 1}, cost_micro=1_234) is True
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=None) is True
    assert backend.reconcile("l", outcome="abandoned", usage=None, cost_micro=99) is True
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=-7) is True
    costs = [p["cost_micro"] for p in driver.params_of(ledger.SETTLE_COUNTED)]
    # an integer is charged as given, 'abandoned' included (the metered charge); only None keeps the estimate
    assert costs == [1_234, None, 99, 0]


def test_neo4j_reconcile_is_false_when_the_ledger_matched_nothing_and_leaves_the_registry():
    driver = Scripted({ledger.SETTLE_COUNTED: []})
    backend, _ = neo4j_backend(driver)
    backend.registry.add("l")
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=1) is False
    assert backend.registry.active() == []


def test_a_failed_neo4j_reconcile_returns_false_at_once_because_nothing_was_charged_and_queues_the_settle():
    driver = Scripted(errors={ledger.SETTLE_COUNTED: [ServiceUnavailable("down")] * 9})
    backend, _ = neo4j_backend(driver)
    backend.registry.add("l")
    started = time.perf_counter()
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=1) is False
    assert time.perf_counter() - started < 1.0                                  # the old retries slept 6 s
    assert driver.count(ledger.SETTLE_COUNTED) == 1 and backend.pending_settles() == 1
    assert backend.registry.active() == []                                  # no more renewals for a finished stream


def test_the_queued_neo4j_settle_charges_once_on_the_next_drain():
    driver = Scripted({ledger.SETTLE_COUNTED: [{"id": "l"}]}, {ledger.SETTLE_COUNTED: [ServiceUnavailable("blip")]})
    backend, _ = neo4j_backend(driver)
    assert backend.reconcile("l", outcome="done", usage={"prompt_tokens": 2}, cost_micro=1_500) is False
    assert backend.drain_settles() == 1 and backend.pending_settles() == 0
    assert driver.count(ledger.SETTLE_COUNTED) == 2
    first, second = driver.params_of(ledger.SETTLE_COUNTED)
    assert first["cost_micro"] == second["cost_micro"] == 1_500 and second["pt"] == 2   # the same settle, retried


def test_a_queued_settle_that_a_sweep_already_closed_is_done_not_retried_forever():
    """The one-winner statement matches nothing for the loser; a queued entry treats that as finished."""
    driver = Scripted({ledger.SETTLE_COUNTED: []}, {ledger.SETTLE_COUNTED: [ServiceUnavailable("blip")]})
    backend, _ = neo4j_backend(driver)
    backend.reconcile("l", outcome="done", usage=None, cost_micro=1)
    assert backend.drain_settles() == 1 and backend.pending_settles() == 0


def test_a_queued_settle_that_keeps_failing_is_given_up_and_logged_for_the_boot_or_the_sweep(caplog):
    driver = Scripted(errors={ledger.SETTLE_COUNTED: [ServiceUnavailable("down")] * 1000})
    backend, _ = neo4j_backend(driver)
    backend.reconcile("l", outcome="done", usage=None, cost_micro=1)
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        for _ in range(settle_queue.MAX_ATTEMPTS):
            backend.drain_settles()
    assert backend.pending_settles() == 0 and driver.count(ledger.SETTLE_COUNTED) == settle_queue.MAX_ATTEMPTS
    assert any("state_settle_abandoned" in r.message and "lease=l" in r.message for r in caplog.records)


def test_the_sweep_leaves_a_lease_alone_while_its_settle_is_queued():
    answers = {ledger.EXPIRED_LEASES: []}
    driver = Scripted(answers, {ledger.SETTLE_COUNTED: [ServiceUnavailable("down")]})
    backend, clock = neo4j_backend(driver)
    backend.reconcile("queued", outcome="done", usage=None, cost_micro=1)
    backend.mark_started("live")
    backend.sweep(clock.wall() + 100)
    (scan,) = driver.params_of(ledger.EXPIRED_LEASES)
    assert sorted(scan["skip"]) == ["live", "queued"]


def test_neo4j_sweep_skips_registered_leases_and_counts_only_the_rows_it_closed():
    answers = {ledger.EXPIRED_LEASES: [{"id": "a"}, {"id": "b"}, {"id": "c"}],
               ledger.SETTLE_COUNTED: lambda params: [{"id": params["id"]}] if params["id"] != "b" else []}
    driver = Scripted(answers)
    backend, clock = neo4j_backend(driver, machine_id="me")
    backend.mark_started("live")
    assert backend.sweep(clock.wall() + 100) == 2                     # b lost a race to its own reconcile
    (scan,) = driver.params_of(ledger.EXPIRED_LEASES)
    assert scan["skip"] == ["live"] and scan["me"] == "me" and scan["now"] == clock.wall() + 100
    settled = driver.params_of(ledger.SETTLE_COUNTED)
    assert {p["outcome"] for p in settled} == {"abandoned"} and {p["cost_micro"] for p in settled} == {None}


def test_neo4j_renew_writes_lease_until_and_raises_when_the_database_fails():
    driver = Scripted({ledger.RENEW_ROW: [{"id": "l"}]})
    backend, clock = neo4j_backend(driver, lease_ttl_s=60.0)
    assert backend.renew("l", clock.wall()) is True
    assert driver.params_of(ledger.RENEW_ROW)[0]["lease_until"] == clock.wall() + 60
    failing = Scripted(errors={ledger.RENEW_ROW: [ServiceUnavailable("down")]})
    backend2, clock2 = neo4j_backend(failing)
    with pytest.raises(StateUnavailable):
        backend2.renew("l", clock2.wall())


def test_neo4j_rebuild_expires_this_machines_rows_then_syncs_the_counters_in_one_transaction():
    answers = {ledger.EXPIRE_RESERVED: [{"n": 2}], ledger.DAY_SUMS: [{"paid": 5, "spend_micro": 300_000, "foreign": 0}],
               ledger.LOCK_DAY: [{"paid": 1, "spend_micro": 1}]}
    driver = Scripted(answers)
    backend, clock = neo4j_backend(driver, machine_id="me", lease_renew_s=15.0)
    report = backend.rebuild_from_ledger()
    (expire,) = driver.params_of(ledger.EXPIRE_RESERVED)
    assert expire["me"] == "me" and expire["grace"] == 30.0 and expire["now"] == clock.wall()
    assert (report.expired, report.paid, report.spend_micro, report.counters_synced, report.delta) == (
        2, 5, 300_000, True, None)
    (write,) = driver.params_of(ledger.WRITE_DAY)
    assert (write["paid"], write["spend_micro"]) == (5, 300_000)
    # the kill cache was reset: read again before serving
    assert backend.kill_level() == "on"


def test_neo4j_rebuild_leaves_the_counters_alone_when_another_machine_holds_a_live_lease(caplog):
    answers = {ledger.EXPIRE_RESERVED: [{"n": 0}], ledger.DAY_SUMS: [{"paid": 5, "spend_micro": 300_000, "foreign": 2}],
               ledger.LOCK_DAY: [{"paid": 4, "spend_micro": 250_000}]}
    driver = Scripted(answers)
    backend, _ = neo4j_backend(driver)
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        report = backend.rebuild_from_ledger()
    assert report.counters_synced is False and report.foreign_leases == 2
    assert dict(report.delta) == {"paid": 1, "spend_micro": 50_000}
    assert driver.count(ledger.WRITE_DAY) == 0
    assert any("state_rebuild_delta" in r.message for r in caplog.records)


def test_neo4j_rebuild_is_a_boot_operation():
    backend, _ = neo4j_backend(Scripted())
    backend.mark_started("live")
    with pytest.raises(RuntimeError, match="boot"):
        backend.rebuild_from_ledger()


def test_neo4j_snapshot_reports_the_counters_the_registry_and_no_ip_hash():
    lease_row = {"id": "l1", "day": "2026-10-06", "strategy": "agent", "workspace": False, "estimate_micro": 5,
                 "machine_id": "other", "lease_until": 9.0}
    answers = {ledger.SNAPSHOT_COUNTERS: [{"paid": 3, "spend_micro": 180_000, "per_ip_max": 2}],
               ledger.SNAPSHOT_INFLIGHT: [{"inflight": 1}], ledger.SNAPSHOT_LEASES: [lease_row]}
    backend, _ = neo4j_backend(Scripted(answers))
    backend.mark_started("l1")
    snapshot = backend.snapshot()
    counters = (snapshot["paid"], snapshot["spend_micro"], snapshot["inflight"], snapshot["per_ip_max"])
    assert counters == (3, 180_000, 1, 2)
    assert snapshot["backend"] == "neo4j" and snapshot["kill"] == "off"
    assert snapshot["leases"][0]["lease_id"] == "l1" and snapshot["leases"][0]["started"] is True
    assert "ip_hash" not in repr(snapshot)
