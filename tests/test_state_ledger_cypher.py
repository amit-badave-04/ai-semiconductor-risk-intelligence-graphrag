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
from collections import namedtuple

import pytest
from neo4j.exceptions import ServiceUnavailable
from test_state_inprocess import FakeClock, state_settings

from semigraph.serve.state import Denied, Lease, StateConfig, StateDrivers, StateUnavailable, ledger
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


def neo4j_backend(driver, *, clock=None, sleeps=None, **settings):
    clock = clock or FakeClock()
    backend = Neo4jBackend(StateConfig.from_settings(state_settings(**settings)), StateDrivers(state=driver),
                           wall=clock.wall, clock=clock.mono, sleep=(sleeps if sleeps is not None else []).append)
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
                                            ("ip_daily", Denied.IP_DAILY), ("inflight", Denied.INFLIGHT),
                                            (None, Denied.UNAVAILABLE)])
def test_a_denial_reason_from_the_ledger_maps_to_the_enum_and_an_unexplained_one_fails_closed(reason, denied):
    snapshots = {"daily_count": {"paid": 150}, "daily_spend": {"spend_micro": 10_000_000}, "ip_daily": {"ip_paid": 20},
                 "inflight": {"inflight": 2}}
    driver = Scripted({ledger.DENIAL_READ: [{"paid": 0, "spend_micro": 0, "ip_paid": 0, "inflight": 0,
                                             **snapshots.get(reason, {})}]})
    backend, clock = neo4j_backend(driver, max_concurrent_answers=2)
    assert ask_neo4j(backend, clock) is denied


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


def test_neo4j_reconcile_passes_the_known_cost_or_none_for_unknown_and_abandoned():
    driver = Scripted({ledger.SETTLE_COUNTED: [{"id": "l"}]})
    backend, _ = neo4j_backend(driver)
    assert backend.reconcile("l", outcome="done", usage={"prompt_tokens": 1}, cost_micro=1_234) is True
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=None) is True
    assert backend.reconcile("l", outcome="abandoned", usage=None, cost_micro=99) is True
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=-7) is True
    costs = [p["cost_micro"] for p in driver.params_of(ledger.SETTLE_COUNTED)]
    assert costs == [1_234, None, None, 0]


def test_neo4j_reconcile_is_false_when_the_ledger_matched_nothing_and_leaves_the_registry():
    driver = Scripted({ledger.SETTLE_COUNTED: []})
    backend, _ = neo4j_backend(driver)
    backend.registry.add("l")
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=1) is False
    assert backend.registry.active() == []


def test_neo4j_reconcile_retries_three_times_two_seconds_apart_then_logs_and_returns_false(caplog):
    sleeps: list[float] = []
    errors = {ledger.SETTLE_COUNTED: [ServiceUnavailable("down")] * 9}
    driver = Scripted(errors=errors)
    backend, _ = neo4j_backend(driver, sleeps=sleeps)
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        assert backend.reconcile("l", outcome="done", usage=None, cost_micro=1) is False
    assert sleeps == [2.0, 2.0, 2.0] and driver.count(ledger.SETTLE_COUNTED) == 4
    assert any("state_settle_failed" in r.message for r in caplog.records)


def test_neo4j_reconcile_that_succeeds_on_a_retry_charges_once():
    sleeps: list[float] = []
    driver = Scripted({ledger.SETTLE_COUNTED: [{"id": "l"}]}, {ledger.SETTLE_COUNTED: [ServiceUnavailable("blip")]})
    backend, _ = neo4j_backend(driver, sleeps=sleeps)
    assert backend.reconcile("l", outcome="done", usage=None, cost_micro=1) is True
    assert sleeps == [2.0] and driver.count(ledger.SETTLE_COUNTED) == 2


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
