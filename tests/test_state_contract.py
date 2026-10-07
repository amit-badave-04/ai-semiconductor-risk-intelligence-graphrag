"""The StateBackend contract, run against every backend (M5a I4a; docs/v2/M5_DECISIONS.md 2.2, docs/v2/M5A_BUILD_PLAN.md
3).

Three parameters:

- ``inprocess``     the in-process backend over an in-memory ledger fake (always runs, no server);
- ``inprocess-db``  the in-process backend writing its rows with the REAL ledger Cypher (opt-in: what ships live);
- ``neo4j``         the neo4j backend, counters and rows in Neo4j (opt-in).

The opt-in ones need ``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1`` and are HARD-PINNED to the throwaway instance
(``bolt://127.0.0.1:7898``): never the real local graph, never production, whatever ``NEO4J_URI`` says. They share that
server with other suites, so each test uses days and a machine id of its own, counts rows by those days only, and
deletes only the rows it made (``Svc*`` labels, by day); nothing here resets the graph.

The driver of a real backend is the production state driver (``make_state_driver``: a pool of 8, 0.5 s acquisition), and
the concurrent tests reach it through a gate of 4, the size of the production state limiter: more threads than the pool
would only produce acquisition timeouts that look like cap denials. Every test switches OFF the caps it is not testing.
"""

import itertools
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio
import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable, TransientError
from test_state_inprocess import (
    FakeClock,
    FakeLedger,
    FakeStoreDriver,
    InThread,
    PolicyGate,
    state_settings,
    wait_until,
)

from semigraph.serve.state import Denied, KillNotStored, Lease, StateDrivers, StateUnavailable, ledger, make_backend
from semigraph.serve.state.backend import KILL_LEVELS, day_of
from semigraph.serve.state.maintenance import MaintenanceThread

THROWAWAY_URI = "bolt://127.0.0.1:7898"
THROWAWAY_USER = "neo4j"
THROWAWAY_PASSWORD = "itest-throwaway-only"  # gitleaks:allow
STATE_LIMITER = 4                   # the production state limiter's size
# contract tests check atomicity, not latency: a loaded laptop must not look like an outage
OP_TIMEOUT_S = 5.0
OPEN = dict(max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=0, max_concurrent_answers=1000)
KINDS = ["inprocess", "inprocess-db", "neo4j"]
_tests_run = itertools.count()


# ------------------------------------------------------------------------------------------ the real server

@pytest.fixture(scope="session")
def throwaway_db():
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
    yield SimpleNamespace(settings=settings, admin=admin)
    admin.close()


class FlakyDriver:
    """The real state driver, except that the next ``fail_sessions`` sessions raise ServiceUnavailable."""

    def __init__(self, inner):
        self._inner, self.fail_sessions, self._lock = inner, 0, threading.Lock()

    def session(self, **config):
        with self._lock:
            if self.fail_sessions > 0:
                self.fail_sessions -= 1
                raise ServiceUnavailable("injected failure")
        return self._inner.session(**config)

    def close(self):
        self._inner.close()


# --------------------------------------------------------------------------------------------- the harness

class Harness:
    def __init__(self, kind, db):
        self.kind, self.db = kind, db
        index = 25_000 + (os.getpid() % 1000) * 200 + 5 * next(_tests_run)            # a block of days of its own
        self.days = [day_of((index + n) * 86400) for n in range(5)]
        self.start_wall = index * 86400 + 3600.0
        self.clock = FakeClock(wall=self.start_wall, mono=1000.0)
        self.machine = f"contract-{uuid.uuid4().hex[:8]}"
        self.fake = FakeLedger() if kind == "inprocess" else None
        self.gate = threading.BoundedSemaphore(1000 if kind == "inprocess" else STATE_LIMITER)
        self.store = FakeStoreDriver() if kind == "inprocess" else None
        self.state_driver = self.store
        self._saved_policy = None
        if db is not None:
            from semigraph.graph.client import make_state_driver
            from semigraph.serve import store

            self.state_driver = FlakyDriver(make_state_driver(SimpleNamespace(
                neo4j_uri=THROWAWAY_URI, neo4j_user=THROWAWAY_USER, neo4j_password=THROWAWAY_PASSWORD,
                neo4j_database=db.settings.neo4j_database, state_op_timeout_s=OP_TIMEOUT_S,
                state_connection_acquisition_s=0.5)))
            self._saved_policy = store.get_policy(db.admin, "kill_switch")

    # ---- building backends and asking ----------------------------------------------------------------------

    def make(self, *, machine_id=None, refresh=True, **overrides):
        settings = state_settings(**{**OPEN, "machine_id": machine_id or self.machine,
                                     "state_op_timeout_s": OP_TIMEOUT_S,
                                     "state_backend": "neo4j" if self.kind == "neo4j" else "inprocess", **overrides})
        kwargs = dict(wall=self.clock.wall, clock=self.clock.mono)
        if self.fake is not None:
            kwargs["ledger"] = self.fake
        backend = make_backend(settings, StateDrivers(state=self.state_driver), **kwargs)
        if refresh:
            backend.refresh_kill_level()
        return backend

    def ask(self, backend, *, ip="ip-1", estimate=60_000, strategy="hybrid", workspace=False):
        return backend.reserve(ip_hash=ip, strategy=strategy, workspace=workspace, estimate_micro=estimate,
                               now_wall=self.clock.wall(), now_mono=self.clock.mono())

    def run_threads(self, count, fn):
        """``fn(i)`` in ``count`` threads released together, each behind the state-limiter gate; results in order."""
        results, errors = [None] * count, []
        barrier = threading.Barrier(count)

        def worker(i):
            try:
                barrier.wait(timeout=60)
                with self.gate:
                    results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        if errors:
            raise errors[0]
        return results

    # ---- reading and planting the ledger ---------------------------------------------------------------------

    def rows(self):
        if self.fake is not None:
            return [dict(r) for r in self.fake.rows.values() if r["day"] in self.days]
        return [r["p"] for r in ledger.run_read(self.db.admin, "MATCH (q:SvcQuery) WHERE q.day IN $days "
                                                "RETURN properties(q) AS p", timeout_s=30, days=self.days)]

    def add_row(self, **fields):
        row = {"id": str(uuid.uuid4()), "day": self.days[0], "cached": False, "status": "settled",
               "ts": self.start_wall,
               "ip_hash": "ip-1", "ip_hash_v": 2, "machine_id": "someone-else", "estimate_micro": 60_000,
               "cost_micro": 1_000, "lease_until": self.start_wall + 60, "strategy": "hybrid", "workspace": False}
        row.update(fields)
        row = {k: v for k, v in row.items() if v is not None}
        if self.fake is not None:
            self.fake.rows[row["id"]] = row
        else:
            ledger.run_write(self.db.admin, "CREATE (q:SvcQuery $props)", timeout_s=30, props=row)
        return row["id"]

    def counter(self, backend, day):
        """``(paid, spend_micro)`` of one day's counter, wherever this backend keeps it."""
        if self.kind == "neo4j":
            rows = ledger.run_read(self.db.admin, "MATCH (c:SvcDayCounter {day: $day}) RETURN c.paid AS paid, "
                                   "c.spend_micro AS spend", timeout_s=30, day=day)
            return (rows[0]["paid"], rows[0]["spend"]) if rows else (0, 0)
        counters = backend._counters.get(day)                                             # noqa: SLF001
        return (counters.paid, counters.spend_micro) if counters else (0, 0)

    def fail_next_durable_calls(self, n):
        if self.fake is not None:
            self.fake.fail_reserve = self.fake.fail_settle = self.fake.fail_renew = n
        else:
            self.state_driver.fail_sessions = n

    def outage(self, down):
        """The database goes away (every statement, the kill-level read and write included) or comes back."""
        if self.store is not None:
            self.store.fail = ServiceUnavailable("injected outage") if down else None
        else:
            self.state_driver.fail_sessions = 10**9 if down else 0

    def stored_level(self):
        """The kill level the database holds (None: never set)."""
        if self.store is not None:
            return self.store.policy.get("kill_switch")
        from semigraph.serve import store

        return store.get_policy(self.db.admin, "kill_switch")

    def write_level_elsewhere(self, level):
        """Another machine (or the CLI) writes the level straight to the database, behind this backend's back."""
        if self.store is not None:
            self.store.policy["kill_switch"] = level
        else:
            from semigraph.serve import store

            store.set_policy(self.db.admin, "kill_switch", level)

    def maintenance_step(self, backend):
        """One tick of the maintenance thread on this harness's clocks: what runs every few seconds in production."""
        MaintenanceThread(backend, state_settings(), clock=self.clock.mono, wall=self.clock.wall).tick()

    def close(self):
        if self.db is None:
            return
        from semigraph.serve import store

        ledger.run_write(self.db.admin, "MATCH (n) WHERE (n:SvcQuery OR n:SvcDayCounter OR n:SvcIpDay) "
                         "AND n.day IN $days "
                         "DETACH DELETE n", timeout_s=30, days=self.days)
        if self._saved_policy is None:
            ledger.run_write(self.db.admin, "MATCH (p:SvcPolicy {key: 'kill_switch'}) DELETE p", timeout_s=30)
        else:
            store.set_policy(self.db.admin, "kill_switch", self._saved_policy)
        self.state_driver.close()


@pytest.fixture(params=KINDS)
def harness(request):
    db = None if request.param == "inprocess" else request.getfixturevalue("throwaway_db")
    h = Harness(request.param, db)
    yield h
    h.close()


@pytest.fixture
def neo4j_harness(request):
    db = request.getfixturevalue("throwaway_db")
    h = Harness("neo4j", db)
    yield h
    h.close()


def sums_of(rows, day, *, cost="cost_micro"):
    """The ledger's own answer, computed here in Python from the rows: (paid, spend_micro, {ip: count})."""
    mine = [r for r in rows if r["day"] == day and not r.get("cached")]
    per_ip: dict[str, int] = {}
    for row in mine:
        if row.get("ip_hash"):
            per_ip[row["ip_hash"]] = per_ip.get(row["ip_hash"], 0) + 1
    return len(mine), sum(r[cost] for r in mine), per_ip


# -------------------------------------------------------------------------------------- admission under load

def test_200_threads_reserving_against_a_cap_of_150_get_exactly_150_and_150_durable_rows(harness):
    backend = harness.make(max_queries_per_day=150)
    results = harness.run_threads(200, lambda i: harness.ask(backend, ip=f"ip-{i}"))
    granted = [r for r in results if isinstance(r, Lease)]
    assert len(granted) == 150
    assert [r for r in results if not isinstance(r, Lease)] == [Denied.DAILY_COUNT] * 50
    rows = harness.rows()
    assert len(rows) == 150 and {r["status"] for r in rows} == {"reserved"}
    assert {r["id"] for r in rows} == {lease.lease_id for lease in granted}
    assert backend.snapshot()["paid"] == 150


def test_a_spend_cap_of_one_dollar_with_a_six_cent_estimate_grants_16(harness):
    backend = harness.make(max_spend_usd_per_day=1.0)
    results = harness.run_threads(40, lambda i: harness.ask(backend, ip=f"ip-{i}", estimate=60_000))
    assert sum(isinstance(r, Lease) for r in results) == 16
    assert [r for r in results if not isinstance(r, Lease)] == [Denied.DAILY_SPEND] * 24
    assert backend.snapshot()["spend_micro"] == 16 * 60_000 and len(harness.rows()) == 16


def test_the_21st_ask_of_one_ip_is_denied_while_another_hash_passes(harness):
    backend = harness.make(paid_per_ip_per_day=20)
    assert all(isinstance(harness.ask(backend, ip="one"), Lease) for _ in range(20))
    assert harness.ask(backend, ip="one") is Denied.IP_DAILY
    assert isinstance(harness.ask(backend, ip="another"), Lease)
    assert backend.snapshot()["per_ip_max"] == 20


@pytest.mark.parametrize("caps, estimate, same_ip, granted, denial", [
    pytest.param(dict(max_spend_usd_per_day=1.0), 250_000, False, 4, Denied.DAILY_SPEND, id="spend-4-x-0.25-of-1.00"),
    pytest.param(dict(max_queries_per_day=150), 1, False, 150, Denied.DAILY_COUNT, id="count-150th-granted"),
    pytest.param(dict(paid_per_ip_per_day=20), 1, True, 20, Denied.IP_DAILY, id="per-ip-20th-granted"),
])
def test_an_ask_that_lands_exactly_on_a_cap_is_granted_and_the_next_one_is_denied(
        harness, caps, estimate, same_ip, granted, denial):
    """The boundary of each cap, one ask at a time: ``spend + estimate > cap`` (not ``>=``) and ``paid < cap`` are the
    comparisons that decide whether the last slot is granted."""
    backend = harness.make(**caps)
    results = [harness.ask(backend, ip="one-ip" if same_ip else f"ip-{n}", estimate=estimate)
               for n in range(granted + 1)]
    assert all(isinstance(r, Lease) for r in results[:granted]), results[:granted]
    assert results[granted] is denial
    snapshot = backend.snapshot()
    assert snapshot["paid"] == granted and snapshot["spend_micro"] == granted * estimate
    assert len(harness.rows()) == granted                                   # the denied ask wrote nothing


def test_50_threads_making_10_reserves_each_against_a_cap_of_20_never_overshoot(harness):
    backend = harness.make(max_queries_per_day=20)

    def ten(i):
        return [harness.ask(backend, ip=f"ip-{i}-{n}") for n in range(10)]

    results = [r for batch in harness.run_threads(50, ten) for r in batch]
    assert sum(isinstance(r, Lease) for r in results) == 20 and len(results) == 500
    assert len(harness.rows()) == 20 and backend.snapshot()["paid"] == 20


def test_the_inflight_cap_denies_the_third_and_a_reconcile_frees_it(harness):
    backend = harness.make(max_concurrent_answers=2)
    first, second = harness.ask(backend, ip="a"), harness.ask(backend, ip="b")
    assert isinstance(first, Lease) and isinstance(second, Lease)
    assert harness.ask(backend, ip="c") is Denied.INFLIGHT
    assert backend.reconcile(first.lease_id, outcome="done", usage=None, cost_micro=100) is True
    assert isinstance(harness.ask(backend, ip="c"), Lease)
    assert harness.ask(backend, ip="d") is Denied.INFLIGHT


def test_zero_concurrency_allows_nothing_and_zero_for_the_other_caps_allows_everything(harness):
    assert harness.ask(harness.make(max_concurrent_answers=0)) is Denied.INFLIGHT
    backend = harness.make(max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=0)
    assert all(isinstance(harness.ask(backend, estimate=10**9), Lease) for _ in range(30))


def test_a_failed_row_write_rolls_the_counters_back(harness):
    backend = harness.make(max_queries_per_day=1, max_concurrent_answers=1)
    harness.fail_next_durable_calls(1)
    assert harness.ask(backend) is Denied.UNAVAILABLE
    assert (backend.snapshot()["paid"], backend.snapshot()["inflight"], backend.snapshot()["spend_micro"]) == (0, 0, 0)
    assert harness.rows() == []
    assert isinstance(harness.ask(backend), Lease)                       # the slot was not consumed by the failure
    assert len(harness.rows()) == 1


# --------------------------------------------------------------------------------------------- reconcile

def test_a_failed_settle_is_queued_not_slept_on_and_lands_on_the_next_drain(harness):
    backend = harness.make()
    lease = harness.ask(backend, estimate=60_000)
    harness.fail_next_durable_calls(1)
    started = time.perf_counter()
    backend.reconcile(lease.lease_id, outcome="done", usage={"prompt_tokens": 3}, cost_micro=1_500)
    assert time.perf_counter() - started < 1.0                                # the old retries slept 6 s
    assert backend.pending_settles() == 1 and harness.rows()[0]["status"] == "reserved"
    assert backend.drain_settles() == 1 and backend.pending_settles() == 0
    (row,) = harness.rows()
    assert (row["status"], row["outcome"], row["cost_micro"], row["prompt_tokens"]) == ("settled", "done", 1_500, 3)
    assert backend.snapshot()["spend_micro"] == 1_500                          # the counters equal the ledger


def test_a_double_reconcile_charges_once(harness):
    backend = harness.make()
    lease = harness.ask(backend, estimate=60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage={"prompt_tokens": 9, "completion_tokens": 4},
                             cost_micro=1_500) is True
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1_500) is False
    assert backend.snapshot()["spend_micro"] == 1_500
    (row,) = harness.rows()
    assert (row["status"], row["outcome"], row["cost_micro"], row["prompt_tokens"]) == ("settled", "done", 1_500, 9)
    assert row["cost_usd"] == 0.0015                                       # the dollar column the old readers sum


def test_a_reconcile_with_cost_none_keeps_the_estimate(harness):
    backend = harness.make()
    lease = harness.ask(backend, estimate=60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=None) is True
    assert backend.snapshot()["spend_micro"] == 60_000 and harness.rows()[0]["cost_micro"] == 60_000


def test_an_abandoned_outcome_charges_the_estimate_whatever_cost_comes_with_it(harness):
    backend = harness.make()
    lease = harness.ask(backend, estimate=60_000)
    backend.reconcile(lease.lease_id, outcome="abandoned", usage=None, cost_micro=5)
    assert backend.snapshot()["spend_micro"] == 60_000 and harness.rows()[0]["outcome"] == "abandoned"


def test_a_cost_above_the_estimate_raises_the_spend_and_a_negative_one_is_floored(harness):
    backend = harness.make(max_concurrent_answers=5)
    over, negative = harness.ask(backend, ip="a", estimate=60_000), harness.ask(backend, ip="b", estimate=60_000)
    backend.reconcile(over.lease_id, outcome="done", usage=None, cost_micro=90_000)
    backend.reconcile(negative.lease_id, outcome="done", usage=None, cost_micro=-5)
    assert backend.snapshot()["spend_micro"] == 90_000 + 0


def test_a_reconcile_of_an_unknown_lease_charges_nothing(harness):
    backend = harness.make()
    assert backend.reconcile("no-such-lease", outcome="done", usage=None, cost_micro=100) is False
    assert backend.snapshot()["spend_micro"] == 0


# ---------------------------------------------------------------------------------------- renew and sweep

def test_renew_writes_lease_until_on_the_row(harness):
    backend = harness.make(lease_ttl_s=60.0)
    lease = harness.ask(backend)
    harness.clock.advance(40)
    assert backend.renew(lease.lease_id, harness.clock.wall()) is True
    assert harness.rows()[0]["lease_until"] == harness.clock.wall() + 60
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    assert backend.renew(lease.lease_id, harness.clock.wall()) is False
    assert backend.renew("no-such-lease", harness.clock.wall()) is False


def test_a_sweep_charges_the_estimate_of_an_expired_unstarted_lease_and_frees_the_slot(harness):
    backend = harness.make(max_concurrent_answers=1)
    lease = harness.ask(backend, estimate=60_000)
    assert harness.ask(backend, ip="other") is Denied.INFLIGHT
    assert backend.sweep(harness.clock.wall() + 30) == 0                    # not yet expired
    harness.clock.advance(61)
    backend.refresh_kill_level()                                            # the maintenance thread's 10 s refresh
    assert backend.sweep(harness.clock.wall()) == 1
    (row,) = harness.rows()
    assert (row["status"], row["outcome"], row["cost_micro"]) == ("settled", "abandoned", 60_000)
    assert backend.snapshot()["spend_micro"] == 60_000 and backend.snapshot()["inflight"] == 0
    assert isinstance(harness.ask(backend, ip="other"), Lease)
    # nothing more to charge
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1) is False


def test_a_sweep_leaves_a_started_lease_alone_however_late_it_runs(harness):
    backend = harness.make()
    lease = harness.ask(backend)
    backend.mark_started(lease.lease_id)
    harness.clock.advance(10_000)
    assert backend.sweep(harness.clock.wall()) == 0
    assert harness.rows()[0]["status"] == "reserved"


def test_reconcile_racing_sweep_charges_exactly_once(harness):
    backend = harness.make(max_concurrent_answers=100)
    leases = [harness.ask(backend, ip=f"ip-{n}", estimate=60_000) for n in range(30)]
    harness.clock.advance(61)
    now = harness.clock.wall()
    def reconcile_or_sweep(i):
        if i % 2 == 0:
            return backend.reconcile(leases[i // 2].lease_id, outcome="done", usage=None, cost_micro=1_000)
        return backend.sweep(now)

    results = harness.run_threads(60, reconcile_or_sweep)
    reconciled = sum(1 for i, r in enumerate(results) if i % 2 == 0 and r is True)
    swept = sum(r for i, r in enumerate(results) if i % 2 == 1)
    assert reconciled + swept == 30                                          # every lease charged exactly once
    rows = harness.rows()
    assert {r["status"] for r in rows} == {"settled"}
    assert sum(r["outcome"] == "done" for r in rows) == reconciled
    assert backend.snapshot()["spend_micro"] == reconciled * 1_000 + swept * 60_000
    assert sum(r["cost_micro"] for r in rows) == reconciled * 1_000 + swept * 60_000


# --------------------------------------------------------------------------------------------- kill level

def test_kill_levels_and_a_stale_cache_give_kill(harness):
    backend = harness.make()
    assert isinstance(harness.ask(backend), Lease)
    for level in ("on", "retrieval_only"):
        backend.set_kill_level(level)
        assert backend.kill_level() == level and harness.ask(backend, ip="x") is Denied.KILL
    backend.set_kill_level("off")
    assert isinstance(harness.ask(backend, ip="y"), Lease)
    harness.clock.advance(31)                                               # no refresh for longer than the stale bound
    assert harness.ask(backend, ip="z") is Denied.KILL
    backend.refresh_kill_level()
    assert isinstance(harness.ask(backend, ip="z"), Lease)


@pytest.mark.parametrize("before, flip", [("off", "on"), ("off", "retrieval_only"), ("on", "off"),
                                          ("retrieval_only", "off")])
def test_an_admin_flip_is_not_undone_by_a_refresh_that_had_already_read_the_old_level(
        harness, monkeypatch, before, flip):
    """A refresh reads the level, the admin sets another (stored, applied at once), and only then does the refresh apply
    what it read. It must not: memory would disagree with the database for up to one refresh period."""
    backend = harness.make()
    backend.set_kill_level(before)
    gate = PolicyGate(monkeypatch)
    gate.hold_next_get()
    refresh = InThread(backend.refresh_kill_level)
    try:
        assert gate.reached["get"].wait(10), "the refresh never reached its read"
        backend.set_kill_level(flip)
    finally:
        gate.release()
    refresh.finish()
    assert backend.kill_level() == flip
    assert backend.refresh_kill_level() == flip                               # and the database holds it too


def test_a_refresh_that_finished_before_the_flip_is_overtaken_by_it(harness):
    backend = harness.make()
    assert backend.refresh_kill_level() == "off"
    backend.set_kill_level("on")
    assert backend.kill_level() == "on" and harness.ask(backend, ip="x") is Denied.KILL


def test_two_concurrent_sets_leave_memory_and_database_agreeing_on_the_later_one(harness, monkeypatch):
    """The first set is held just before its database write; the second starts, bumps its generation and (once the
    writes are serialised) waits for it. Whatever order the writes then land in, the later set must win in both."""
    backend = harness.make()
    gate = PolicyGate(monkeypatch)
    gate.hold_next_set()
    first = InThread(lambda: backend.set_kill_level("on"))
    second = None
    try:
        assert gate.reached["set"].wait(10), "the first set never reached its write"
        second = InThread(lambda: backend.set_kill_level("off"))
        wait_until(lambda: gate.set_calls >= 2 or getattr(backend, "_kill_gen", 0) >= 2)
    finally:
        gate.release()
    first.finish()
    second.finish()
    assert backend.kill_level() == "off"
    assert backend.refresh_kill_level() == "off"                              # the database says the same


def test_an_unread_kill_level_and_the_env_override_both_give_kill(harness):
    unread = harness.make(refresh=False)
    assert harness.ask(unread) is Denied.KILL
    overridden = harness.make(kill_switch=True)
    assert harness.ask(overridden) is Denied.KILL
    assert harness.rows() == []                                              # a killed ask writes nothing


def test_a_level_set_through_one_backend_is_read_by_another(harness):
    first, second = harness.make(), harness.make(machine_id=harness.machine + "-b")
    first.set_kill_level("retrieval_only")
    assert second.refresh_kill_level() == "retrieval_only"


@pytest.mark.parametrize("level, cache", [("on", "fresh"), ("on", "stale"), ("on", "unread"),
                                          ("retrieval_only", "fresh")])
def test_a_tightening_set_during_an_outage_holds_and_is_stored_when_the_database_is_back(harness, cache, level):
    """The admin's kill reaches a database that is down. A level that reads ``on`` only because it is stale or was never
    read must not make the set look like 'nothing to tighten': the level holds in memory AND its write is queued, so the
    next refresh after the recovery stores it instead of reading the old ``off`` back and reopening paid asks."""
    backend = harness.make(refresh=cache != "unread")
    if cache == "stale":
        harness.clock.advance(31)                                   # past kill_switch_stale_s: on by age alone
    harness.outage(True)
    with pytest.raises(KillNotStored) as failed:
        backend.set_kill_level(level)
    assert failed.value.held is True                                # the error says it holds and will be retried
    assert backend.kill_level() == level and backend._pending_kill == level                # noqa: SLF001
    assert harness.ask(backend) is Denied.KILL
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == level and backend._pending_kill is None                # noqa: SLF001
    assert backend.kill_level() == level and harness.ask(backend, ip="after") is Denied.KILL
    assert harness.rows() == []                                                 # no paid ask slipped through


@pytest.mark.parametrize("level, age", [("on", "fresh"), ("on", "stale"), ("retrieval_only", "fresh")])
def test_a_set_equal_to_the_cached_level_during_an_outage_is_queued_and_beats_a_stored_value_that_differs(
        harness, level, age):
    """The write failed and the level is as tight as the cache says: ``stored: false`` promises a retry, so it must be
    queued. The database is made to differ (another writer relaxed it during the outage): a refresh that read it back
    without the queued write would reopen paid asks, which is what makes this test fail without the fix."""
    backend = harness.make()
    backend.set_kill_level(level)                                   # the cache holds ``level``, and so does the database
    if age == "stale":
        harness.clock.advance(31)                                   # the cached ``on`` also reads ``on`` by age alone
    harness.write_level_elsewhere("off")
    harness.outage(True)
    with pytest.raises(KillNotStored) as failed:
        backend.set_kill_level(level)                               # equal to the raw cached level
    assert failed.value.held is True and backend._pending_kill == level                    # noqa: SLF001
    assert backend.kill_level() == level and harness.ask(backend) is Denied.KILL
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == level and backend._pending_kill is None                # noqa: SLF001
    assert backend.kill_level() == level and harness.ask(backend, ip="after") is Denied.KILL


def test_a_set_of_off_is_never_held_or_queued_even_when_the_cache_already_reads_off(harness):
    """``off`` relaxes whatever the cache says: it is stored first or it is nothing. Queuing it would let the next
    refresh write ``off`` over a level another machine stored, with nobody having confirmed it."""
    backend = harness.make()                                        # the cache reads ``off``, fresh
    harness.write_level_elsewhere("on")                             # the database holds ``on``; this backend has not read it
    harness.outage(True)
    with pytest.raises(KillNotStored) as failed:
        backend.set_kill_level("off")
    assert failed.value.held is False and backend._pending_kill is None                    # noqa: SLF001
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == "on" and backend.kill_level() == "on"
    assert harness.ask(backend, ip="after") is Denied.KILL


@pytest.mark.parametrize("cache", ["stale", "unread"])
def test_retrieval_only_set_on_a_cache_that_reads_on_does_not_lower_the_level_or_overwrite_the_database(harness, cache):
    """A cache that is stale or was never read says ``on`` whatever it holds. A set of ``retrieval_only`` is a
    relaxation of THAT, so it needs a successful write: a failed one must neither lower the level the gates apply nor
    queue ``retrieval_only`` over the ``on`` that is stored."""
    backend = harness.make(refresh=cache != "unread")
    if cache == "stale":
        harness.clock.advance(31)
    harness.write_level_elsewhere("on")
    harness.outage(True)
    with pytest.raises(KillNotStored) as failed:
        backend.set_kill_level("retrieval_only")
    assert failed.value.held is False and backend._pending_kill is None                    # noqa: SLF001
    assert backend.kill_level() == "on" and harness.ask(backend) is Denied.KILL
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == "on" and backend.kill_level() == "on"


def _cache_cases():
    for cache in ("fresh", "stale"):
        for raw in KILL_LEVELS:
            for level in KILL_LEVELS:
                yield cache, raw, level
    for level in KILL_LEVELS:
        yield "unread", "off", level                                # an unread cache holds nothing: one raw level only


@pytest.mark.parametrize("cache, raw, level", list(_cache_cases()))
def test_a_set_that_fails_during_an_outage_never_lowers_the_effective_level_and_never_queues_off(
        harness, cache, raw, level):
    """Every cache state x every cached level x every requested level. A failed write may keep the level the gates
    apply or raise it, never lower it; it holds exactly when the level is not ``off`` and at least as tight as the level
    the gates apply now (the cached level, or ``on`` when that is stale or unread); and only what holds is queued."""
    backend = harness.make(refresh=cache != "unread")
    if cache != "unread":
        backend.set_kill_level(raw)
    if cache == "stale":
        harness.clock.advance(31)
    effective_before = KILL_LEVELS.index(backend.kill_level())
    stored_before = harness.stored_level()
    holds = level != "off" and KILL_LEVELS.index(level) >= effective_before
    harness.outage(True)
    with pytest.raises(KillNotStored) as failed:
        backend.set_kill_level(level)
    assert failed.value.held is holds
    assert KILL_LEVELS.index(backend.kill_level()) >= effective_before
    assert backend._pending_kill == (level if holds else None)                              # noqa: SLF001
    if backend.kill_level() != "off":
        assert harness.ask(backend) is Denied.KILL
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == (level if holds else stored_before)                    # what was not held is not written


@pytest.mark.parametrize("held, relax", [("on", "off"), ("on", "retrieval_only"), ("retrieval_only", "off")])
def test_a_relaxation_set_during_an_outage_does_not_take_effect_and_is_not_retried(harness, held, relax):
    backend = harness.make()
    backend.set_kill_level(held)
    harness.outage(True)
    with pytest.raises(StateUnavailable):
        backend.set_kill_level(relax)
    assert backend.kill_level() == held and backend._pending_kill is None                  # noqa: SLF001
    assert harness.ask(backend) is Denied.KILL
    harness.outage(False)
    harness.maintenance_step(backend)
    assert harness.stored_level() == held and backend.kill_level() == held
    assert harness.ask(backend, ip="after") is Denied.KILL


def test_hold_kill_level_applies_a_tightening_in_memory_without_waiting_for_a_database_write(harness):
    """The admin route calls it on the event loop BEFORE it waits for the admin limiter, so an emergency kill is not
    delayed by an earlier admin call stuck on a silent connection (it holds the writer lock for up to 120 s)."""
    backend = harness.make()
    stored_before = harness.stored_level()
    with backend._writer_lock:                                      # noqa: SLF001 - the stuck write
        held = InThread(lambda: backend.hold_kill_level("on"))
        assert held.finish(timeout=2.0) is True                     # it never waited for the lock
        assert backend.kill_level() == "on" and harness.ask(backend) is Denied.KILL     # in force at once
    assert harness.stored_level() == stored_before                  # nothing was written: memory only
    assert backend._pending_kill == "on"                                                  # noqa: SLF001
    harness.maintenance_step(backend)                               # the queued write lands on the next tick
    assert harness.stored_level() == "on" and backend._pending_kill is None                # noqa: SLF001


def test_hold_kill_level_refuses_what_relaxes_and_changes_nothing(harness):
    backend = harness.make()
    backend.set_kill_level("on")
    generation = backend._kill_gen                                                        # noqa: SLF001
    assert backend.hold_kill_level("retrieval_only") is False
    assert backend.hold_kill_level("off") is False
    assert backend.kill_level() == "on" and backend._pending_kill is None                  # noqa: SLF001
    assert backend._kill_gen == generation                          # a refusal does not supersede an earlier set  # noqa: SLF001
    with pytest.raises(ValueError):
        backend.hold_kill_level("paused")


# ------------------------------------------------------------------------------------------------- the log

SENTINEL = "SENTINEL-the-question-an-analyst-typed"


class BrokenDriver:
    """A state driver whose every use raises ``error`` (a driver that quotes what the statement carried)."""

    def __init__(self, error):
        self.error = error

    def session(self, **config):
        raise self.error


class UntouchableDriver:
    """A state driver that fails the test the moment anything uses it."""

    def session(self, **config):
        raise AssertionError("the state driver was used")


@pytest.mark.parametrize("kind", ["inprocess", "neo4j"])
def test_kill_level_and_mark_started_are_memory_only_on_every_backend(kind):
    """The async callers take these (and ``hold_kill_level``, the admin flip's first step) on the event loop, with no thread hop and no state slot (the protocol says
    so): a backend that touched its driver in any of them would block the loop when the database goes silent."""
    backend = make_backend(state_settings(**OPEN, state_backend=kind), StateDrivers(state=UntouchableDriver()))
    assert backend.kill_level() == "on"                                    # never read: closed, without a read
    backend.mark_started("a-lease")
    backend._kill_level, backend._kill_read_at = "off", backend._clock()                  # noqa: SLF001
    assert backend.kill_level() == "off"
    backend.mark_started("a-lease")
    assert ("a-lease" in backend.registry) == (kind == "neo4j")           # the in-process one registers only its own leases
    backend._forget_kill_level()                                                          # noqa: SLF001
    assert backend.hold_kill_level("retrieval_only") is False             # unread reads ``on``: this relaxes it
    assert backend.hold_kill_level("on") is True and backend.kill_level() == "on"


@pytest.mark.parametrize("kind", ["inprocess", "neo4j"])
@pytest.mark.parametrize("error", [
    ServiceUnavailable(SENTINEL), OSError(SENTINEL), TimeoutError(SENTINEL),
    Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message=SENTINEL),
], ids=lambda error: type(error).__name__)
def test_a_state_failure_is_logged_by_class_and_server_code_and_never_by_message(kind, error, caplog):
    """A driver's message can quote the parameters of a statement: the text of a question, an address hash. The log
    line names the exception class and, for a server error, its code, and nothing else."""
    caplog.set_level("DEBUG")
    settings = state_settings(**OPEN, state_backend=kind)
    backend = make_backend(settings, StateDrivers(state=BrokenDriver(error)))
    with pytest.raises(StateUnavailable) as raised:
        backend.cache_get("key", 24)
    assert SENTINEL not in caplog.text and SENTINEL not in str(raised.value)
    (record,) = [r for r in caplog.records if "state_unavailable" in r.getMessage()]
    assert f"error={type(error).__name__}" in record.getMessage()
    assert getattr(error, "code", None) is None or f"code={error.code}" in record.getMessage()


# ----------------------------------------------------------------------------------------------- the day

def test_a_stream_crossing_midnight_reconciles_into_its_own_day_and_the_new_day_starts_at_zero(harness):
    harness.clock = FakeClock(wall=harness.start_wall - 3600 + 86400 - 10, mono=1000.0)       # 23:59:50 of day 0
    backend = harness.make(max_concurrent_answers=5)
    first, second = harness.ask(backend, ip="a", estimate=60_000), harness.ask(backend, ip="b", estimate=60_000)
    harness.clock.advance(30)                                                                  # day 1
    backend.refresh_kill_level()
    assert (first.day, second.day) == (harness.days[0], harness.days[0])
    snapshot = backend.snapshot()
    assert snapshot["day"] == harness.days[1] and (snapshot["paid"], snapshot["spend_micro"]) == (0, 0)
    today = harness.ask(backend, ip="c", estimate=10_000)
    assert today.day == harness.days[1]
    backend.reconcile(first.lease_id, outcome="done", usage=None, cost_micro=1_000)
    assert harness.counter(backend, harness.days[0]) == (2, 1_000 + 60_000)                    # D's own counter took it
    # day 1 untouched by the settle
    assert harness.counter(backend, harness.days[1]) == (1, 10_000)
    snapshot = backend.snapshot()
    assert (snapshot["paid"], snapshot["spend_micro"]) == (1, 10_000)


# --------------------------------------------------------------------------------------- the boot rebuild

def test_kill_then_boot_leaves_the_snapshot_equal_to_the_ledger(harness):
    first = harness.make(max_concurrent_answers=50)
    leases = [harness.ask(first, ip=f"ip-{n % 4}", estimate=50_000 + n) for n in range(12)]
    costs = {0: 1_000, 1: 2_000, 2: None, 3: 70_000}
    for n, cost in costs.items():
        first.reconcile(leases[n].lease_id, outcome="done", usage={"prompt_tokens": n}, cost_micro=cost)
    first.reconcile(leases[4].lease_id, outcome="abandoned", usage=None, cost_micro=None)
    for lease in leases[5:8]:
        first.mark_started(lease.lease_id)
    # the process dies here: leases 5..11 are still reserved; nothing is cleaned up
    second = harness.make(max_concurrent_answers=50)
    report = second.rebuild_from_ledger()
    second.refresh_kill_level()
    rows = harness.rows()
    assert {r["status"] for r in rows} == {"settled"}                                  # nothing is left reserved
    assert sum(r.get("outcome") == "abandoned_restart" for r in rows) == 7 == report.expired
    paid, spend, per_ip = sums_of(rows, harness.days[0])
    snapshot = second.snapshot()
    assert paid == 12 and (snapshot["paid"], snapshot["spend_micro"]) == (paid, spend)
    assert snapshot["per_ip_max"] == max(per_ip.values()) == 3 and dict(report.per_ip) == per_ip
    assert snapshot["inflight"] == 0 and (report.paid, report.spend_micro) == (paid, spend)
    expected = (1_000 + 2_000 + (50_000 + 2) + 70_000 + (50_000 + 4)) + sum(50_000 + n for n in range(5, 12))
    assert spend == expected


def test_the_boot_rebuild_counts_a_legacy_ledger_row_that_has_no_status(harness):
    for n in range(3):                                         # what store.log_query wrote before I4
        harness.add_row(status=None, cost_micro=None, cost_usd=0.012345, ip_hash_v=None, estimate_micro=None,
                        lease_until=None, machine_id=None, ts=None, created_at="2026-10-05T10:00:00+00:00")
    harness.add_row(cached=True, cost_micro=0)                                     # a cached answer is not a paid ask
    harness.add_row(day=harness.days[1], cost_micro=999)                           # another day
    backend = harness.make(max_queries_per_day=4)
    report = backend.rebuild_from_ledger()
    backend.refresh_kill_level()
    assert (report.paid, report.spend_micro) == (3, 3 * 12_345)
    # unsalted legacy hashes never seed windows
    assert dict(report.per_ip) == {}
    assert isinstance(harness.ask(backend, ip="fresh"), Lease)                      # the 4th of today
    # the deploy day's count did not reset
    assert harness.ask(backend, ip="fresh-2") is Denied.DAILY_COUNT


def test_the_boot_rebuild_seeds_per_ip_counts_and_windows_from_the_current_pepper_only(harness):
    for n in range(3):
        harness.add_row(ip_hash="aa", ts=harness.start_wall - 100 + n)
    harness.add_row(ip_hash="bb", ts=harness.start_wall - 5)
    harness.add_row(ip_hash="cc", ip_hash_v=1)                                     # another pepper: never seeded
    harness.add_row(ip_hash=None, ip_hash_v=0)                                     # a nulled legacy row
    backend = harness.make(paid_per_ip_per_day=3)
    report = backend.rebuild_from_ledger()
    backend.refresh_kill_level()
    assert dict(report.per_ip) == {"aa": 3, "bb": 1}
    assert harness.ask(backend, ip="aa") is Denied.IP_DAILY and isinstance(harness.ask(backend, ip="bb"), Lease)
    events = report.window_events(window_s=3600)
    assert sorted(events) == ["aa", "bb"] and len(events["aa"]) == 3
    assert events["bb"] == [report.now_mono - 5]


def test_the_boot_rebuild_closes_this_machines_rows_and_long_dead_ones_but_not_another_machines_live_lease(harness):
    live_foreign = harness.add_row(status="reserved", machine_id="elsewhere", cost_micro=None,
                                   lease_until=harness.start_wall + 30)
    dead_foreign = harness.add_row(status="reserved", machine_id="elsewhere", cost_micro=None,
                                   lease_until=harness.start_wall - 31)
    recent_foreign = harness.add_row(status="reserved", machine_id="elsewhere", cost_micro=None,
                                     lease_until=harness.start_wall - 29)
    own = harness.add_row(status="reserved", machine_id=harness.machine, cost_micro=None,
                          lease_until=harness.start_wall + 30)
    older_day = harness.add_row(status="reserved", machine_id="elsewhere", cost_micro=None, day=harness.days[1],
                                lease_until=harness.start_wall - 86400)                # no day filter: closed as well
    backend = harness.make()
    report = backend.rebuild_from_ledger()
    by_id = {r["id"]: r for r in harness.rows()}
    assert by_id[live_foreign]["status"] == "reserved" and by_id[recent_foreign]["status"] == "reserved"
    for closed in (dead_foreign, own, older_day):
        assert (by_id[closed]["status"], by_id[closed]["outcome"], by_id[closed]["cost_micro"]) == (
            "settled", "abandoned_restart", 60_000)
    assert report.expired == 3 and report.foreign_leases == 1
    # live-foreign, dead-foreign, own (day 0)
    assert report.paid == 3


# ----------------------------------------------------------------------------- more than one machine (neo4j)

def test_two_instances_on_one_database_b_boot_leaves_a_live_lease_alone_and_a_reconcile_still_lands(neo4j_harness):
    h = neo4j_harness
    a = h.make(machine_id=h.machine + "-A", max_concurrent_answers=2)
    lease = h.ask(a, estimate=60_000)
    a.mark_started(lease.lease_id)
    b = h.make(machine_id=h.machine + "-B", max_concurrent_answers=2)
    report = b.rebuild_from_ledger()
    assert report.foreign_leases == 1 and report.expired == 0 and report.counters_synced is False
    assert h.rows()[0]["status"] == "reserved"                                       # not expired by B's boot
    b.refresh_kill_level()                                                            # the boot forgot the cached level
    # in-flight is global: one of two used
    assert isinstance(h.ask(b, ip="b-1"), Lease)
    assert b.snapshot()["inflight"] == 2 and h.ask(b, ip="b-2") is Denied.INFLIGHT
    assert a.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1_000) is True
    assert {r["id"]: r["status"] for r in h.rows()}[lease.lease_id] == "settled"
    # A's settle and B's lease, one counter
    assert h.counter(b, h.days[0])[1] == 1_000 + 60_000


def test_a_second_instances_sweep_leaves_the_first_instances_leases_alone(neo4j_harness):
    h = neo4j_harness
    a = h.make(machine_id=h.machine + "-A")
    b = h.make(machine_id=h.machine + "-B")
    renewed, unrenewed = h.ask(a, ip="a1"), h.ask(a, ip="a2")
    a.mark_started(renewed.lease_id)
    h.clock.advance(50)
    assert a.renew(renewed.lease_id, h.clock.wall()) is True
    # the unrenewed one expired 40 s ago
    h.clock.advance(50)
    assert b.sweep(h.clock.wall()) == 0                                              # B only ever closes its own
    assert {r["id"]: r["status"] for r in h.rows()} == {renewed.lease_id: "reserved", unrenewed.lease_id: "reserved"}
    assert a.sweep(h.clock.wall()) == 1                                              # A closes its own unrenewed lease
    assert {r["id"]: r["status"] for r in h.rows()}[renewed.lease_id] == "reserved"


# ------------------------------------------------------------------------ bounded: a stalled database

class StalledDriver:
    """A database that never answers: every statement blocks for ``stall_s`` unless it carries a server-side timeout, in
    which case the 'server' cuts it at that timeout and raises (what a real server does). It answers the kill-level read
    normally until ``stalled`` is set, so a backend can be made ready (level read) before the database 'goes down'."""

    def __init__(self, stall_s=5.0):
        self.stall_s, self.stalled = stall_s, True

    def session(self, **config):
        return StalledSession(self)


class StalledSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def _stall(self, timeout):
        stall_s = self._driver.stall_s
        time.sleep(stall_s if timeout is None else min(stall_s, timeout))
        raise TransientError("the transaction timed out on the server")

    def execute_write(self, fn, *args, **kwargs):
        self._stall(getattr(fn, "timeout", None))

    execute_read = execute_write

    def run(self, query, **params):
        if not self._driver.stalled:
            return [{"v": "off"}]                                         # the kill-level read of a healthy database
        self._stall(getattr(query, "timeout", None))


def stalled_backend(kind, op_timeout):
    settings = state_settings(**OPEN, state_backend="neo4j" if kind == "neo4j" else "inprocess",
                              state_op_timeout_s=op_timeout)
    driver = StalledDriver()
    driver.stalled = False
    backend = make_backend(settings, StateDrivers(state=driver))
    backend.refresh_kill_level()                                         # serving has started: the level is cached
    driver.stalled = True
    return backend


def timed(op):
    started = time.perf_counter()
    try:
        outcome = op()
    except StateUnavailable:
        outcome = StateUnavailable
    return outcome, time.perf_counter() - started


@pytest.mark.parametrize("kind", ["inprocess", "neo4j"])
def test_100_operations_against_a_stalled_driver_each_fail_within_the_bound_on_a_bounded_set_of_threads(kind):
    """Against a FAKE database that honours the server-side timeout it is given: this proves the backends hand every
    statement a timeout and add no waiting of their own, on a thread count the limiter bounds. It says nothing about the
    real driver and server: their measured bounds (about 1.0 s against a dead server, about 1.1 s on a held lock with
    the deployed 100 ms transaction-monitor interval) are in ``tests/integration/test_state_neo4j.py``."""
    op_timeout, limiter_size = 0.1, 4
    backend = stalled_backend(kind, op_timeout)
    clock = FakeClock()
    ops = [lambda: backend.reserve(ip_hash="a", strategy="hybrid", workspace=False, estimate_micro=1,
                                   now_wall=clock.wall(), now_mono=clock.mono()),
           lambda: backend.cache_get("k", 24), backend.refresh_kill_level,
           lambda: backend.snapshot() if kind == "neo4j" else backend.cache_get("k2", 24),
           lambda: backend.renew("l", clock.wall()) if kind == "neo4j" else backend.cache_get("k3", 24)]
    expected = [Denied.UNAVAILABLE] + [StateUnavailable] * 4
    baseline, peak, results = threading.active_count(), [0], []
    limiter = anyio.CapacityLimiter(limiter_size)

    async def run():
        async def one(n):
            outcome, elapsed = await anyio.to_thread.run_sync(timed, ops[n % len(ops)], limiter=limiter)
            results.append((n % len(ops), outcome, elapsed))
            peak[0] = max(peak[0], threading.active_count())

        async with anyio.create_task_group() as group:
            for n in range(100):
                group.start_soon(one, n)

    anyio.run(run)
    assert len(results) == 100
    for index, outcome, elapsed in results:
        assert outcome is expected[index] or outcome == expected[index], (index, outcome)
        assert elapsed < op_timeout + 0.1, f"op {index} took {elapsed:.2f} s against a bound of {op_timeout} s"
    assert peak[0] - baseline <= limiter_size + 1                  # the worker threads, plus the maintenance thread


@pytest.mark.parametrize("kind", ["inprocess", "neo4j"])
def test_with_the_production_timeout_an_operation_against_a_stalled_driver_fails_within_1_1_seconds(kind):
    backend = stalled_backend(kind, 1.0)
    clock = FakeClock()
    ops = [lambda: backend.reserve(ip_hash="a", strategy="hybrid", workspace=False, estimate_micro=1,
                                   now_wall=clock.wall(), now_mono=clock.mono()),
           lambda: backend.cache_get("k", 24), backend.refresh_kill_level]
    with ThreadPoolExecutor(max_workers=len(ops)) as pool:
        for outcome, elapsed in pool.map(timed, ops):
            assert elapsed < 1.1
            assert outcome in (Denied.UNAVAILABLE, StateUnavailable)


def test_a_statement_without_a_server_side_timeout_would_not_be_bounded():
    """The control: the stalled fake really does block when nothing bounds it, so the tests above bound something."""
    driver = StalledDriver(stall_s=0.3)
    started = time.perf_counter()
    with pytest.raises(TransientError), driver.session() as session:
        session.execute_write(lambda tx: None)
    assert time.perf_counter() - started >= 0.3


def server_monitor_interval_s(admin) -> float:
    """The server's ``db.transaction.monitor.check.interval`` in seconds (it prints as ``100ms`` or ``2s``)."""
    rows = ledger.run_read(admin, "SHOW SETTINGS YIELD name, value WHERE name = "
                           "'db.transaction.monitor.check.interval' RETURN value", timeout_s=30)
    text = str(rows[0]["value"]).strip().lower() if rows else "2s"
    return float(text[:-2]) / 1000 if text.endswith("ms") else float(text.rstrip("s"))


def test_the_server_cuts_a_long_query_at_its_transaction_timeout(neo4j_harness):
    """The server enforces a transaction timeout when its transaction monitor next looks (every
    ``db.transaction.monitor.check.interval``: 2 s by default, 100 ms in deploy/neo4j/fly.toml), so the cut comes
    between the timeout and the timeout plus one period."""
    from neo4j.exceptions import ClientError

    period = server_monitor_interval_s(neo4j_harness.db.admin)
    started = time.perf_counter()
    with pytest.raises((ClientError, TransientError)):
        ledger.run_read(neo4j_harness.state_driver, "UNWIND range(1, 2000000000) AS x RETURN count(x) AS n",
                        timeout_s=0.2)
    # cut by the server, not run to the end
    assert time.perf_counter() - started < 0.2 + period + 0.5
