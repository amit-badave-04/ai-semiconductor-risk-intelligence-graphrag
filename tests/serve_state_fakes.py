"""Test doubles for the paid-ask state of the HTTP layer (M5a I4, ``semigraph.serve.state``).

* :class:`FakeStateBackend` implements the ``StateBackend`` protocol with scriptable behaviour: every ``Denied`` a
  reserve can answer, a ``StateUnavailable`` or any other exception from any method, a slow call, a stale kill level. It
  records every call in order (``calls``), the thread it ran on (``threads``) and the settled asks (``settled``), which
  the route and stream tests read where they used to read the ledger rows ``store.log_query`` wrote. Its answer cache is
  a dict, so a second identical ask is a cache hit exactly as with the real backends.
* :class:`InMemoryLedger` is the ``serve.state.ledger`` module's row functions over a dict: handed to the REAL
  ``InProcessBackend`` (``ledger=``), it lets a test run the real admission logic (in-flight cap, per-address cap, daily
  caps) with no database. ``tests/serve_async_app.py`` runs the real backend this way under a real uvicorn.
* :func:`install_state` puts the state attributes the routes read on an ``app.state``; :func:`fresh_drain` swaps the
  process-wide ``DRAIN`` for a new one for the length of a test (a leaked count or a ``begin()`` would otherwise make
  every later test of the process wait or refuse).

Nothing here talks to a database or the network.
"""

import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from semigraph.serve import drain, store
from semigraph.serve.state import Denied, Lease, StateUnavailable
from semigraph.serve.state.backend import KILL_LEVELS
from semigraph.serve.state.ledger import DaySums

DAY = "2026-10-06"
# the route's per-type estimates
ESTIMATE_MICRO = {"hybrid": 60_000, "vector": 40_000, "agent": 200_000, "workspace": 70_000}


class FakeStateBackend:
    """A ``StateBackend`` whose answers the test scripts. ``kill`` is the level ``kill_level()`` reports (a stale or
    unread level reads ``on``: set it so). ``deny`` queues the denials the next reserves return (then the reserves are
    granted); ``errors`` maps a method name to an exception raised by every call of it (``errors["reserve"] =
    StateUnavailable()``). ``delays`` maps a method name to seconds each call sleeps (they run on worker threads).
    ``max_inflight`` makes the fake deny INFLIGHT itself once that many leases are open."""

    def __init__(self, *, kill: str = "off", max_inflight: int | None = None):
        self._lock = threading.Lock()
        self.kill = kill
        self.max_inflight = max_inflight
        self.deny: deque[Denied] = deque()
        self.errors: dict[str, BaseException] = {}
        self.delays: dict[str, float] = {}
        self.cache: dict[str, dict] = {}
        self.calls: list[tuple] = []                       # (name, ...): the call order across every method
        self.threads: dict[str, set[int]] = {}
        self.leases: dict[str, Lease] = {}                 # open (not yet settled) leases
        self.started: set[str] = set()
        self.granted: list[Lease] = []
        self.reserve_kwargs: list[dict] = []
        self.puts: list[dict] = []                         # the keyword arguments of every cache_put
        self.on_call = None                                # ``on_call(name)`` runs at the start of every method
        self.settled: list[dict] = []                      # one record per reconcile that charged
        self.reconcile_calls: list[dict] = []              # every reconcile call, charged or not
        self.kill_sets: list[str] = []
        self._ids = 0

    # ---- scripting

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def inflight(self) -> int:
        return len(self.leases)

    def _enter(self, name: str, *detail) -> None:
        with self._lock:
            self.calls.append((name, *detail))
            self.threads.setdefault(name, set()).add(threading.get_ident())
        if self.on_call is not None:
            self.on_call(name)
        delay = self.delays.get(name)
        if delay:
            time.sleep(delay)
        error = self.errors.get(name)
        if error is not None:
            raise error

    # ---- the protocol

    def reserve(self, *, ip_hash, strategy, workspace, estimate_micro, now_wall, now_mono):
        self._enter("reserve", strategy, workspace, estimate_micro)
        with self._lock:
            self.reserve_kwargs.append({"ip_hash": ip_hash, "strategy": strategy, "workspace": workspace,
                                        "estimate_micro": estimate_micro})
            if self.deny:
                return self.deny.popleft()
            if self.kill != "off":
                return Denied.KILL
            if self.max_inflight is not None and len(self.leases) >= self.max_inflight:
                return Denied.INFLIGHT
            self._ids += 1
            lease = Lease(f"lease-{self._ids}", DAY, ip_hash, strategy, workspace, estimate_micro,
                          now_mono + 60.0, "m1")
            self.leases[lease.lease_id] = lease
            self.granted.append(lease)
            return lease

    def mark_started(self, lease_id):
        self._enter("mark_started", lease_id)
        self.started.add(lease_id)

    def renew(self, lease_id, now_wall):
        self._enter("renew", lease_id)
        return lease_id in self.leases

    def reconcile(self, lease_id, *, outcome, usage, cost_micro):
        record = {"lease_id": lease_id, "outcome": outcome, "usage": usage, "cost_micro": cost_micro}
        with self._lock:
            self.reconcile_calls.append(record)
        self._enter("reconcile", lease_id, outcome)
        with self._lock:
            lease = self.leases.pop(lease_id, None)
            if lease is None:
                return False
            self.started.discard(lease_id)
            self.settled.append({**record, "strategy": lease.strategy, "workspace": lease.workspace,
                                 "ip_hash": lease.ip_hash, "estimate_micro": lease.estimate_micro})
            return True

    def sweep(self, now):
        self._enter("sweep")
        return 0

    def rebuild_from_ledger(self):
        self._enter("rebuild_from_ledger")
        raise NotImplementedError("a lifespan test supplies its own rebuild report")

    def kill_level(self):
        self._enter("kill_level")
        return self.kill

    def hold_kill_level(self, level):
        """Memory only, like the real one (no ``_enter``, so no injected delay or error reaches it): a level other than
        ``off`` that is at least as tight as the level in force applies at once and reports True."""
        if level not in KILL_LEVELS:
            raise ValueError(level)
        with self._lock:
            self.calls.append(("hold_kill_level", level))
            holds = level != KILL_LEVELS[0] and KILL_LEVELS.index(level) >= KILL_LEVELS.index(self.kill)
            if holds:
                self.kill = level
        return holds

    def set_kill_level(self, level):
        """A level that tightens holds in memory even when the store write then fails (the error is raised); one that
        relaxes is applied only when the write succeeded. The fake has ONE level, ``kill``, and models no staleness: it
        ranks against that level, which the real backend does not (it ranks against the cached level as last read or
        set, however old: a level that is ``on`` only because it is stale or unread must not make a set of ``on`` look
        like a no-op). So this double cannot show that; ``tests/test_state_contract.py`` and the real-backend test in
        ``tests/test_serve_state_wiring.py`` do."""
        if level not in KILL_LEVELS:
            raise ValueError(level)
        tightening = KILL_LEVELS.index(level) > KILL_LEVELS.index(self.kill)
        try:
            self._enter("set_kill_level", level)
        except StateUnavailable:
            if tightening:
                self.kill = level
            raise
        self.kill_sets.append(level)
        self.kill = level

    def cache_get(self, key, ttl_hours):
        self._enter("cache_get", key)
        return self.cache.get(key)

    def cache_put(self, **kw):
        self._enter("cache_put", kw.get("question"))
        self.puts.append(dict(kw))
        self.cache[store.cache_key(kw["question"], kw["strategy"], kw.get("snapshot_id", ""))] = {
            "answer": kw["answer"], "source": "live", "citations": kw["citations"], "hallucinated": kw["hallucinated"]}

    def snapshot(self):
        self._enter("snapshot")
        return {"backend": "fake", "day": DAY, "paid": len(self.granted), "spend_micro": 0, "inflight": self.inflight,
                "per_ip_max": 0, "kill": self.kill, "kill_age_s": 0.0, "leases": []}

    def refresh_kill_level(self):
        self._enter("refresh_kill_level")
        return self.kill


class InMemoryLedger:
    """The row functions of ``serve.state.ledger`` that ``InProcessBackend`` uses, over a dict (see the module
    docstring). ``rows`` is keyed by lease id; a settled row carries ``outcome``, ``usage``, ``cost_micro`` and
    ``cost_usd``. ``on_settle(row)`` runs after every settle (the real-uvicorn app journals through it). ``delay_s``
    slows every write."""

    def __init__(self, *, delay_s: float = 0.0):
        self._lock = threading.Lock()
        self.rows: dict[str, dict] = {}
        self.delay_s = delay_s
        self.on_reserve = None
        self.on_settle = None
        self.fail_reserve: BaseException | None = None

    def reserve_row(self, driver, *, lease_id, day, ip_hash, ip_hash_v, strategy, workspace, estimate_micro, now_wall,
                    lease_until, machine_id, timeout_s):
        time.sleep(self.delay_s)
        if self.fail_reserve is not None:
            raise self.fail_reserve
        with self._lock:
            self.rows.setdefault(lease_id, {"id": lease_id, "day": day, "ip_hash": ip_hash, "ip_hash_v": ip_hash_v,
                                            "strategy": strategy, "workspace": workspace, "cached": False,
                                            "status": "reserved", "estimate_micro": estimate_micro,
                                            "lease_until": lease_until, "machine_id": machine_id, "outcome": None,
                                            "usage": None, "cost_micro": None, "cost_usd": None})
        if self.on_reserve is not None:
            self.on_reserve(self.rows[lease_id])

    def settle_row(self, driver, lease_id, *, outcome, usage, actual_micro, now_wall, timeout_s):
        time.sleep(self.delay_s)
        with self._lock:
            row = self.rows.get(lease_id)
            if row is None or row["status"] != "reserved":
                return False
            row.update(status="settled", outcome=outcome, usage=usage, cost_micro=actual_micro,
                       cost_usd=round(actual_micro / 1_000_000, 6))
            snapshot = dict(row)
        if self.on_settle is not None:
            self.on_settle(snapshot)
        return True

    def renew_row(self, driver, lease_id, *, lease_until, timeout_s):
        with self._lock:
            row = self.rows.get(lease_id)
            if row is None or row["status"] != "reserved":
                return False
            row["lease_until"] = lease_until
            return True

    def expire_reserved(self, driver, *, now_wall, grace_s, machine_id, timeout_s):
        closed = 0
        with self._lock:
            for row in self.rows.values():
                expired = row["lease_until"] < now_wall - grace_s
                if row["status"] == "reserved" and (row["machine_id"] == machine_id or expired):
                    row.update(status="settled", outcome="abandoned_restart", cost_micro=row["estimate_micro"])
                    closed += 1
        return closed

    def day_sums(self, driver, *, day, now_wall, machine_id, ip_hash_v, timeout_s):
        with self._lock:
            todays = [r for r in self.rows.values() if r["day"] == day]
        per_ip: dict[str, int] = {}
        for row in todays:
            per_ip[row["ip_hash"]] = per_ip.get(row["ip_hash"], 0) + 1
        return DaySums(paid=len(todays), spend_micro=sum(r["cost_micro"] or r["estimate_micro"] for r in todays),
                       foreign_leases=0, per_ip=per_ip, ip_events=())


def install_state(app, *, backend: FakeStateBackend | None = None, estimates: dict[str, int] | None = None,
                  cache_rate: float = 10_000.0):
    """The state attributes ``routes`` reads, on ``app.state``. ``cache_rate`` is generous so a test that is not about
    the cache budget never meets it. Returns the backend."""
    from semigraph.serve.guard import TokenBucket

    backend = backend or FakeStateBackend()
    app.state.state = backend
    app.state.state_store_driver = SimpleNamespace(name="bounded-state-driver")
    app.state.estimates = dict(ESTIMATE_MICRO if estimates is None else estimates)
    app.state.cache_budget = TokenBucket(cache_rate)
    return backend


class RouteSettings:
    """The settings ``routes`` and ``PaidStream`` read, with the M5a I4 additions, as a plain class a test can change
    (``app.state.settings.agent_enabled = True``)."""

    kill_switch = False
    max_queries_per_day = 150
    max_spend_usd_per_day = 10.0
    paid_per_ip_per_day = 20
    turnstile_secret_key = ""
    turnstile_site_key = ""
    is_production = False
    max_question_chars = 200
    llm_request_timeout_s = 5
    llm_answer_max_tokens = 100
    answer_cache_ttl_hours = 24
    rate_limit_questions = 5
    rate_limit_window_seconds = 600
    max_concurrent_answers = 2
    state_op_timeout_s = 1.0
    admin_token = "secret-token"
    client_ip_header = ""
    free_rate_limit_questions = 10
    stats_cache_seconds = 0
    turnstile_required = False
    read_rate_limit_per_minute = 50
    llm_model = "anthropic/claude-sonnet-5"
    answer_model = "anthropic/claude-sonnet-5"
    escalation_model = ""
    agent_enabled = False
    uploads_enabled = False
    freshness_enabled = False
    embed_slots = 1
    db_thread_limit = 4
    send_timeout_s = 30
    loop_lag_warn_ms = 100


def build_route_app(*, backend: FakeStateBackend | None = None, settings=None, estimates: dict[str, int] | None = None,
                    cache_rate: float = 10_000.0, routers=None):
    """A FastAPI app with the real ``routes.router`` (and ``routers``), the state attributes of :func:`install_state`,
    the windows ``main.lifespan`` builds and a lifespan that makes the limiters INSIDE the running loop. ``with
    TestClient(app)`` keeps that loop for the whole test, so ``client.portal.call`` shares it."""
    import asyncio
    import contextlib

    from fastapi import FastAPI

    from semigraph.serve import routes
    from semigraph.serve.guard import RateLimiter
    from semigraph.serve.limiters import make_limiters

    settings = settings or RouteSettings()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.limiters = make_limiters(settings)
        app.state.loop = asyncio.get_running_loop()
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    for router in routers or ():
        app.include_router(router)
    app.state.settings = settings
    app.state.driver = object()
    app.state.embedder = type("E", (), {"name": "fake-embedder"})()
    app.state.graph_stats = {"nodes": {"Company": 26}, "relationships": 1}
    app.state.rate_limiter = RateLimiter(settings.rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(settings.free_rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.read_rate_limiter = RateLimiter(settings.read_rate_limit_per_minute, 60)
    install_state(app, backend=backend, estimates=estimates, cache_rate=cache_rate)
    return app


@pytest.fixture
def fresh_drain(monkeypatch):
    """A new process-wide ``DRAIN`` for one test (``routes`` and ``stream_runtime`` look it up at call time)."""
    new = drain.Drain()
    monkeypatch.setattr(drain, "DRAIN", new)
    yield new
    assert new.active == 0, f"the test left {new.active} stream(s) counted on the drain"
