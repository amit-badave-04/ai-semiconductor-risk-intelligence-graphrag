"""The state package against a REAL Neo4j: what a fake cannot show (M5a I4a).

Opt-in (``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1``) and hard-pinned to the THROWAWAY instance ONLY
(``bolt://127.0.0.1:7898``, ``neo4j`` / ``itest-throwaway-only``): never 7699 (the real local graph), never 7687,
whatever ``NEO4J_URI`` is in the environment. The address is the IPv4 literal on purpose: ``localhost`` resolves to
``::1`` first on this machine, where nothing listens, and the state driver's 1 s connect timeout is spent on it.

The server is shared with other suites, so nothing here resets the graph: every test uses calendar days of its own (far
in the future) and a machine id of its own, and removes only the ``Svc*`` rows of those days and that machine. The two
tests that must use today's real day (the existing ``store`` readers) delete their own rows by machine id and marker.

``tests/test_state_contract.py`` holds the properties every backend promises; this file holds what needs the server
itself: the schema, the lock behaviour of the Cypher, the existing readers over the new rows, a held lock and an
unreachable server.
"""

import itertools
import os
import random
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from neo4j.exceptions import ConstraintError

from semigraph.serve import store
from semigraph.serve.state import Denied, Lease, StateDrivers, StateUnavailable, ledger, make_backend
from semigraph.serve.state.backend import day_of

THROWAWAY_URI = "bolt://127.0.0.1:7898"
THROWAWAY_USER = "neo4j"
THROWAWAY_PASSWORD = "itest-throwaway-only"  # gitleaks:allow
_blocks = itertools.count()
BASE_SETTINGS = dict(max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=0, max_concurrent_answers=1000,
                     kill_switch=False, kill_switch_refresh_s=10.0, kill_switch_stale_s=30.0, lease_ttl_s=60.0,
                     lease_renew_s=15.0, ip_hash_version=2)


@pytest.fixture(scope="module")
def db():
    if os.environ.get("RUN_NEO4J_TESTS") != "1":
        pytest.skip("Neo4j integration tests are opt-in: set RUN_NEO4J_TESTS=1")
    if os.environ.get("SEMIGRAPH_ALLOW_WIPE") != "1":
        pytest.skip("this suite writes and deletes ledger rows: set SEMIGRAPH_ALLOW_WIPE=1 too")
    from semigraph.config import Settings
    from semigraph.graph.client import get_driver

    settings = Settings(_env_file=None, neo4j_uri=THROWAWAY_URI, neo4j_user=THROWAWAY_USER,
                        neo4j_password=THROWAWAY_PASSWORD)
    try:
        admin = get_driver(settings)
    except RuntimeError as exc:
        pytest.skip(f"the throwaway Neo4j on 7898 is not reachable: {exc}")
    ledger.ensure_state_schema(admin)
    yield admin
    admin.close()


class Env:
    """One test's private slice of the shared server: days, a machine id, drivers, and its own cleanup."""

    def __init__(self, admin):
        from semigraph.graph.client import make_state_driver

        self.admin, self.machine = admin, f"it-{uuid.uuid4().hex[:8]}"
        first = 30_000 + (os.getpid() % 1000) * 200 + 5 * next(_blocks)
        self.days = [day_of((first + n) * 86400) for n in range(3)]
        self.wall = first * 86400 + 3600.0
        self._make_driver = make_state_driver
        self._drivers = []
        self._policy = store.get_policy(admin, "kill_switch")

    def driver(self, op_timeout=5.0, acquisition=0.5, uri=THROWAWAY_URI):
        driver = self._make_driver(SimpleNamespace(
            neo4j_uri=uri, neo4j_user=THROWAWAY_USER, neo4j_password=THROWAWAY_PASSWORD, neo4j_database="neo4j",
            state_op_timeout_s=op_timeout, state_connection_acquisition_s=acquisition))
        self._drivers.append(driver)
        return driver

    def backend(self, kind="neo4j", *, machine=None, op_timeout=5.0, wall=None, driver=None, sleep=None, refresh=True,
                **overrides):
        settings = SimpleNamespace(**{**BASE_SETTINGS, "state_backend": kind, "machine_id": machine or self.machine,
                                      "state_op_timeout_s": op_timeout, **overrides})
        kwargs = {name: value for name, value in (("wall", wall), ("sleep", sleep)) if value is not None}
        backend = make_backend(settings, StateDrivers(state=driver or self.driver(op_timeout)), **kwargs)
        if refresh:
            backend.refresh_kill_level()
        return backend

    def ask(self, backend, *, ip="ip-1", estimate=60_000, now_wall=None):
        return backend.reserve(ip_hash=ip, strategy="hybrid", workspace=False, estimate_micro=estimate,
                               now_wall=now_wall or self.wall, now_mono=time.monotonic())

    def rows(self, days=None):
        return [r["p"] for r in ledger.run_read(self.admin, "MATCH (q:SvcQuery) WHERE q.day IN $days "
                                                "RETURN properties(q) AS p", timeout_s=30, days=days or self.days)]

    def counter(self, day):
        rows = ledger.run_read(self.admin, "MATCH (c:SvcDayCounter {day: $d}) "
                               "RETURN c.paid AS paid, c.spend_micro AS spend", timeout_s=30, d=day)
        return (rows[0]["paid"], rows[0]["spend"]) if rows else None

    def close(self):
        ledger.run_write(self.admin, "MATCH (n) WHERE (n:SvcQuery OR n:SvcDayCounter OR n:SvcIpDay) AND n.day IN $days "
                         "DETACH DELETE n", timeout_s=30, days=self.days)
        ledger.run_write(self.admin, "MATCH (q:SvcQuery) WHERE q.machine_id = $m DETACH DELETE q", timeout_s=30,
                         m=self.machine)
        if self._policy is None:
            ledger.run_write(self.admin, "MATCH (p:SvcPolicy {key: 'kill_switch'}) DELETE p", timeout_s=30)
        else:
            store.set_policy(self.admin, "kill_switch", self._policy)
        for driver in self._drivers:
            driver.close()


@pytest.fixture
def env(db):
    environment = Env(db)
    yield environment
    environment.close()


def micro_of(row):
    """What one ledger row counts for in today's spend: a reserved row its estimate, a settled one its cost, a legacy
    one
    its dollar cost in whole micro-dollars."""
    if row.get("status") == "reserved":
        return row.get("estimate_micro", 0)
    return row["cost_micro"] if "cost_micro" in row else round((row.get("cost_usd") or 0.0) * 1_000_000)


def threads(count, fn, *, limit=4):
    """``fn(i)`` in ``count`` threads released together, at most ``limit`` inside at once (the state limiter)."""
    gate, barrier, results, errors = threading.BoundedSemaphore(limit), threading.Barrier(count), [None] * count, []

    def worker(i):
        try:
            barrier.wait(timeout=60)
            with gate:
                results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=120)
    if errors:
        raise errors[0]
    return results


# -------------------------------------------------------------------------------------------------- schema

def test_the_schema_is_created_idempotently_and_enforces_its_constraints(env):
    ledger.ensure_state_schema(env.admin)
    ledger.ensure_state_schema(env.admin)
    names = {r["name"] for r in ledger.run_read(env.admin, "SHOW CONSTRAINTS YIELD name RETURN name", timeout_s=30)}
    assert {"svc_query_id_unique", "svc_day_counter_unique", "svc_ip_day_unique"} <= names
    indexes = {r["name"] for r in ledger.run_read(env.admin, "SHOW INDEXES YIELD name RETURN name", timeout_s=30)}
    assert {"svc_query_status", "svc_query_day_status"} <= indexes
    day = env.days[0]
    ledger.run_write(env.admin, "CREATE (:SvcDayCounter {day: $d, paid: 0, spend_micro: 0})", timeout_s=30, d=day)
    with pytest.raises(ConstraintError):
        ledger.run_write(env.admin, "CREATE (:SvcDayCounter {day: $d})", timeout_s=30, d=day)
    ledger.run_write(env.admin, "CREATE (:SvcIpDay {day: $d, ip_hash: 'x', paid: 0})", timeout_s=30, d=day)
    with pytest.raises(ConstraintError):
        ledger.run_write(env.admin, "CREATE (:SvcIpDay {day: $d, ip_hash: 'x', paid: 0})", timeout_s=30, d=day)
    # another hash is fine
    ledger.run_write(env.admin, "CREATE (:SvcIpDay {day: $d, ip_hash: 'y', paid: 0})", timeout_s=30, d=day)


def test_concurrent_first_reserves_of_a_new_day_make_one_counter_and_one_ip_node(env):
    backend = env.backend()
    results = threads(8, lambda i: env.ask(backend, ip="same-ip"))
    assert all(isinstance(r, Lease) for r in results)
    assert env.counter(env.days[0]) == (8, 8 * 60_000)
    ip_nodes = ledger.run_read(env.admin, "MATCH (i:SvcIpDay {day: $d}) RETURN i.ip_hash AS ip, i.paid AS paid",
                               timeout_s=30, d=env.days[0])
    assert ip_nodes == [{"ip": "same-ip", "paid": 8}]


def test_no_lock_property_is_left_on_a_ledger_row(env):
    backend = env.backend()
    lease = env.ask(backend)
    backend.renew(lease.lease_id, env.wall)
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=5)
    # the second one matches nothing
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=5)
    (row,) = env.rows()
    assert "_lock" not in row


# ------------------------------------------------------------------------ the existing readers, the new rows

def test_the_existing_store_readers_still_count_the_rows_the_state_package_writes(env):
    """Today's real day (the readers take no day): measured as a difference, rows removed by machine id afterwards."""
    before_paid = store.paid_queries_today(env.admin)
    before_summary = store.ledger_summary(env.admin)["today"]
    backend = env.backend("inprocess", wall=time.time)
    first = backend.reserve(ip_hash="reader-test", strategy="hybrid", workspace=False, estimate_micro=60_000,
                            now_wall=time.time(), now_mono=time.monotonic())
    second = backend.reserve(ip_hash="reader-test", strategy="hybrid", workspace=True, estimate_micro=70_000,
                             now_wall=time.time(), now_mono=time.monotonic())
    assert isinstance(first, Lease) and isinstance(second, Lease)
    backend.reconcile(first.lease_id, outcome="done", usage={"prompt_tokens": 5, "completion_tokens": 2},
                      cost_micro=12_345)
    assert store.paid_queries_today(env.admin) == before_paid + 2                  # a reserved row counts as a paid ask
    summary = store.ledger_summary(env.admin)["today"]
    assert summary["paid"] == before_summary["paid"] + 2
    # one settled at 12,345 micro, one still reserved with no dollar cost yet: the old reader sees cost_usd = 0.012345
    assert summary["cost_usd"] == pytest.approx(before_summary["cost_usd"] + 0.012345, abs=1e-4)


def test_a_row_the_old_log_query_wrote_is_counted_by_the_boot_rebuild_with_its_window_time(env):
    marker = f"legacy-{uuid.uuid4().hex[:8]}"
    store.log_query(env.admin, ip_hash=marker, strategy="hybrid", cached=False,
                    usage={"prompt_tokens": 7, "completion_tokens": 3}, cost_usd=0.0123, ip_hash_v=2)
    try:
        backend = env.backend("inprocess", wall=time.time)
        report = backend.rebuild_from_ledger()
        mine = [r for r in ledger.run_read(env.admin, "MATCH (q:SvcQuery {ip_hash: $m}) RETURN properties(q) AS p",
                                           timeout_s=30, m=marker)]
        assert len(mine) == 1 and "status" not in mine[0]["p"]                      # the legacy shape: no status
        today = day_of(time.time())
        rows = ledger.run_read(env.admin, "MATCH (q:SvcQuery {day: $d, cached: false}) WHERE q.status IS NULL OR "
                               "q.status = 'settled' OR (q.status = 'reserved' AND q.lease_until >= $now) RETURN "
                               "properties(q) AS p", timeout_s=30, d=today, now=time.time())
        assert (report.paid, report.spend_micro) == (len(rows), sum(micro_of(r["p"]) for r in rows))
        events = [(ip, ts) for ip, ts in report.ip_events if ip == marker]
        assert len(events) == 1 and abs(events[0][1] - time.time()) < 120             # created_at stands in for ts
        assert report.per_ip[marker] == 1
    finally:
        ledger.run_write(env.admin, "MATCH (q:SvcQuery {ip_hash: $m}) DETACH DELETE q", timeout_s=30, m=marker)


def test_a_reserved_row_with_no_lease_until_is_closed_by_the_boot(env):
    ledger.run_write(env.admin, "CREATE (:SvcQuery {id: $id, day: $d, status: 'reserved', cached: false, "
                     "estimate_micro: 42000, machine_id: 'someone', ts: $ts})", timeout_s=30, id=str(uuid.uuid4()),
                     d=env.days[0], ts=env.wall)
    backend = env.backend("inprocess", wall=lambda: env.wall)
    report = backend.rebuild_from_ledger()
    (row,) = env.rows()
    assert (row["status"], row["outcome"], row["cost_micro"]) == ("settled", "abandoned_restart", 42_000)
    assert report.expired == 1 and report.spend_micro == 42_000


def test_a_neo4j_backend_settles_a_row_the_in_process_backend_wrote(env):
    """A rollback from one backend to the other must not wedge: the counter that never existed is created at zero."""
    lease = env.ask(env.backend("inprocess"))
    neo4j = env.backend("neo4j")
    assert neo4j.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1_000) is True
    assert env.rows()[0]["status"] == "settled" and env.counter(env.days[0]) == (0, 0)   # floored, never negative


# ------------------------------------------------------------------------------------------- concurrency

def test_two_instances_racing_for_the_last_slot_grant_exactly_one(env):
    first = env.backend(machine=env.machine + "-a", max_queries_per_day=1)
    second = env.backend(machine=env.machine + "-b", max_queries_per_day=1)
    results = threads(20, lambda i: env.ask(first if i % 2 else second, ip=f"ip-{i}"))
    assert sum(isinstance(r, Lease) for r in results) == 1
    assert [r for r in results if not isinstance(r, Lease)] == [Denied.DAILY_COUNT] * 19
    assert len(env.rows()) == 1


def test_forty_threads_of_one_ip_against_a_cap_of_20_grant_exactly_20(env):
    backend = env.backend(paid_per_ip_per_day=20)
    results = threads(40, lambda i: env.ask(backend, ip="one-ip"))
    assert sum(isinstance(r, Lease) for r in results) == 20
    assert [r for r in results if not isinstance(r, Lease)] == [Denied.IP_DAILY] * 20


def test_mixed_operations_from_two_instances_never_fail_and_leave_the_counters_equal_to_the_ledger(env):
    """Reserves, reconciles (known cost, unknown cost, abandoned), renewals and sweeps at once: no deadlock may surface
    as
    an unavailable store, and every counter must end up equal to what the ledger says."""
    backends = [env.backend(machine=f"{env.machine}-{n}", max_concurrent_answers=40, op_timeout=5.0) for n in "ab"]
    live, lock, unavailable = [], threading.Lock(), []
    deadline = time.monotonic() + 4.0

    def step(i):
        backend, local = backends[i % 2], random.Random(i)
        while time.monotonic() < deadline:
            action = local.random()
            if action < 0.45:
                estimate = local.choice((50_000, 60_000, 90_000))
                result = env.ask(backend, ip=f"ip-{local.randrange(6)}", estimate=estimate)
                if isinstance(result, Lease):
                    with lock:
                        live.append((backend, result))
                elif result is Denied.UNAVAILABLE:
                    unavailable.append("reserve")
            elif action < 0.8:
                with lock:
                    mine = [item for item in live if item[0] is backend]
                    picked = mine[local.randrange(len(mine))] if mine else None
                    if picked:
                        live.remove(picked)
                if picked:
                    cost = local.choice((None, 0, 1_000, 55_000, 120_000))
                    picked[0].reconcile(picked[1].lease_id, outcome=local.choice(("done", "done", "abandoned")),
                                        usage={"prompt_tokens": 1}, cost_micro=cost)
            elif action < 0.9:
                backend.sweep(env.wall)                                           # nothing has expired: a no-op scan
            else:
                with lock:
                    mine = [item for item in live if item[0] is backend]
                if mine:
                    backend.renew(mine[local.randrange(len(mine))][1].lease_id, env.wall)

    try:
        threads(4, step)
    except StateUnavailable as exc:
        pytest.fail(f"a state operation surfaced as unavailable: {exc}")
    for backend, lease in live:
        backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=lease.estimate_micro // 2)
    assert unavailable == []
    rows = env.rows()
    assert {r["status"] for r in rows} == {"settled"}
    per_ip: dict[str, int] = {}
    for row in rows:
        per_ip[row["ip_hash"]] = per_ip.get(row["ip_hash"], 0) + 1
    assert env.counter(env.days[0]) == (len(rows), sum(r["cost_micro"] for r in rows))
    stored = {r["ip"]: r["paid"] for r in ledger.run_read(
        env.admin, "MATCH (i:SvcIpDay {day: $d}) RETURN i.ip_hash AS ip, i.paid AS paid", timeout_s=30, d=env.days[0])}
    assert stored == per_ip


# -------------------------------------------------------------------------- the bound, with a real server

# A server-side transaction timeout is enforced by the server's transaction monitor, which looks at the running
# transactions every ``db.transaction.monitor.check.interval`` (2 s by default): a 1.0 s timeout is cut between 1 s and
# 3 s, not at 1 s. Measured on Neo4j Community 2026.07.1 against a held day-counter lock, reserve returning unavailable
# after (max over 4 runs): default interval 0.2 s -> 1.24 s, 0.5 s -> 1.23 s, 1.0 s -> 2.00 s; interval 100 ms -> 0.27
# s, 0.57 s, 1.06 s. So the plan's "1.1 s" holds for a server configured with a short interval; the tests below assert
# what the default server delivers (timeout + one monitor period + slack) and say so.
MONITOR_PERIOD_S = 2.0


def test_a_lock_held_by_another_transaction_makes_a_reserve_unavailable_within_the_server_bound(env):
    backend = env.backend(op_timeout=1.0)
    day = env.days[0]
    with env.admin.session() as holder:
        tx = holder.begin_transaction()
        tx.run("MERGE (c:SvcDayCounter {day: $d}) ON CREATE SET c.paid = 0, c.spend_micro = 0 "
               "SET c._lock = true", d=day)
        try:
            started = time.perf_counter()
            result = env.ask(backend)
            elapsed = time.perf_counter() - started
        finally:
            tx.rollback()
    assert result is Denied.UNAVAILABLE
    assert elapsed < 1.0 + MONITOR_PERIOD_S + 0.5, f"a reserve blocked on a held lock took {elapsed:.2f} s"
    again = env.ask(backend)                                                        # the lock is gone: it works, once
    assert isinstance(again, Lease) and env.counter(day) == (1, 60_000) and len(env.rows()) == 1


def test_a_lock_held_by_another_transaction_makes_a_settle_unavailable_within_the_bound_and_charges_nothing(env):
    backend = env.backend(op_timeout=1.0)
    lease = env.ask(backend)
    with env.admin.session() as holder:
        tx = holder.begin_transaction()
        tx.run("MATCH (c:SvcDayCounter {day: $d}) SET c._lock = true", d=env.days[0])
        try:
            sleeps = []
            # no real 2 s waits between attempts
            slow = env.backend(op_timeout=1.0, sleep=sleeps.append)
            started = time.perf_counter()
            charged = slow.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
            elapsed = time.perf_counter() - started
        finally:
            tx.rollback()
    # four bounded attempts, then give up
    assert charged is False and len(sleeps) == 3
    assert elapsed < 4 * (1.0 + MONITOR_PERIOD_S) + 0.5
    assert env.rows()[0]["status"] == "reserved" and env.counter(env.days[0]) == (1, 60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1) is True       # the lock is gone


def listener(*, close_at_once):
    """A local TCP listener that is not Neo4j: it either closes every connection at once (a refused-like failure that is
    instant on every OS) or keeps them open and never answers (a black hole)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    held = []

    def accept():
        while True:
            try:
                connection = server.accept()[0]
            except OSError:
                return
            if close_at_once:
                connection.close()
            else:
                held.append(connection)                     # accepted, never read from, never answered

    threading.Thread(target=accept, daemon=True).start()
    return server


@pytest.mark.parametrize("target", ["closed-port", "instant-close", "blackhole"])
def test_an_unreachable_server_is_bounded_as_measured_on_2026_10_06(env, target):
    """Not 1 s. Measured with the production settings (1 s operation timeout, 0.5 s acquisition, 1 s connect) on Windows
    with the neo4j driver 6.2.0 (the retry loop is the same code in 6.3.0):

    - an auto-commit read (the cache read, the kill-level read) gives up in about 0.5 s (the acquisition timeout);
    - a managed transaction (reserve, renew, sweep, snapshot, settle) does NOT stop at ``max_transaction_retry_time``:
      the driver starts that timer AFTER the first failed attempt, so it always retries at least once, after a delay of
      1 s +-20% (then 2 s +-20% if the failures were instant). Against a listener that closes at once: 1.0-1.2 s half
      the time and 2.4-3.4 s the other half (30 runs: median 1.23, p90 3.06, max 3.38). Against a black hole or a
      closed port on Windows (each attempt costs the 0.5 s acquisition timeout): 1.8-2.2 s.

    Candidate driver settings measured against the same targets (not applied: the plan fixes
    ``max_transaction_retry_time = state_op_timeout_s``): ``max_transaction_retry_time=0.2`` with
    ``initial_retry_delay=0.05`` gave 0.35-0.44 s (instant close) and 1.05-1.07 s (black hole).

    The bounds asserted here are above the measured worst cases (3.4 s; 4.2 s from a 0.2 s attempt time): a regression
    guard against an unbounded wait, not a promise of 1.1 s."""
    close_at_once = target == "instant-close"
    server = listener(close_at_once=close_at_once) if target != "closed-port" else None
    uri = f"bolt://127.0.0.1:{server.getsockname()[1]}" if server else "bolt://127.0.0.1:1"
    try:
        dead = env.driver(op_timeout=1.0, uri=uri)
        backend = env.backend("neo4j", driver=dead, refresh=False)
        # serving had started: the kill level was read before the server went away
        backend._kill_level, backend._kill_read_at = "off", backend._clock()
        for label, call, bound in (
                ("cache_get", lambda: backend.cache_get("k", 24), 1.5),
                ("kill refresh", backend.refresh_kill_level, 1.5),
                ("reserve", lambda: env.ask(backend), 5.0),
                ("renew", lambda: backend.renew("l", env.wall), 5.0)):
            started = time.perf_counter()
            try:
                outcome = call()
            except StateUnavailable:
                outcome = Denied.UNAVAILABLE
            elapsed = time.perf_counter() - started
            assert outcome is Denied.UNAVAILABLE, label
            assert elapsed < bound, f"{label} took {elapsed:.2f} s against {target}"
    finally:
        if server:
            server.close()
