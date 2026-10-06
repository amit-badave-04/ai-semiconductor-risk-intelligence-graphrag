"""The shared core of the state package (money, configuration, kill level, error wrapper) and the in-process backend,
against in-memory fakes of the state driver and the ledger: no server, no pandas, no sentence-transformers.

This module also hosts the fakes ``tests/test_state_contract.py`` imports (``FakeClock``, ``FakeLedger``,
``FakeStoreDriver``, ``state_settings``). ``FakeLedger`` mirrors the SEMANTICS of ``serve/state/ledger.py`` (one winner
per row, legacy rows without a status count as settled) in Python; the real Cypher is exercised by the opt-in suites.
"""

import logging
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable

from semigraph.serve.state import (
    Denied,
    Lease,
    StateConfig,
    StateDrivers,
    StateUnavailable,
    backend as backend_module,
    ledger,
    make_backend,
    micro_to_usd,
    settle_queue,
    usd_to_micro,
)
from semigraph.serve.state.inprocess import InProcessBackend

DAY = 1_790_000_000.0 - (1_790_000_000.0 % 86400) + 3600.0       # 01:00 UTC of some day D
MIDNIGHT_NEXT = DAY - 3600.0 + 86400.0


def state_settings(**overrides) -> SimpleNamespace:
    values = dict(state_backend="inprocess", max_queries_per_day=150, max_spend_usd_per_day=10.0,
                  paid_per_ip_per_day=20, max_concurrent_answers=2, kill_switch=False, kill_switch_refresh_s=10.0,
                  kill_switch_stale_s=30.0, state_op_timeout_s=1.0, state_connection_acquisition_s=0.5,
                  lease_ttl_s=60.0, lease_renew_s=15.0, machine_id="m1", ip_hash_version=2)
    return SimpleNamespace(**{**values, **overrides})


class FakeClock:
    """A wall clock and a monotonic clock that only move when told to."""

    def __init__(self, wall: float = DAY, mono: float = 1000.0):
        self._wall, self._mono = wall, mono

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds


class FakeStoreDriver:
    """Answers the four statements ``store`` runs through the state driver (policy get/set, answer get/put) from memory.
    ``fail`` (an exception instance) makes every use raise it; ``queries`` records ``(text, server-side timeout)``."""

    def __init__(self):
        self.policy: dict[str, str] = {}
        self.answers: dict[str, dict] = {}
        self.fail: Exception | None = None
        self.queries: list[tuple[str, float | None]] = []
        self._lock = threading.Lock()

    def session(self, **config):
        if self.fail is not None:
            raise self.fail
        return _FakeStoreSession(self)


class _FakeStoreSession:
    def __init__(self, driver):
        self._d = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run(self, query, **params):
        text, timeout = getattr(query, "text", query), getattr(query, "timeout", None)
        with self._d._lock:
            self._d.queries.append((text, timeout))
            if "MERGE (p:SvcPolicy" in text:
                self._d.policy[params["key"]] = params["value"]
                return []
            if "MATCH (p:SvcPolicy" in text:
                return [{"v": self._d.policy[params["key"]]}] if params["key"] in self._d.policy else []
            if "MERGE (a:SvcAnswer" in text:
                fields = ("question", "strategy", "answer", "citations", "hallucinated", "source")
                self._d.answers[params["key"]] = {k: params[k] for k in fields} | {"created_at": "now"}
                return []
            if "MATCH (a:SvcAnswer" in text:
                return [self._d.answers[params["key"]]] if params["key"] in self._d.answers else []
        raise AssertionError(f"unexpected statement: {text}")


class PolicyGate:
    """Holds ONE call of ``store.get_policy`` or ``store.set_policy`` mid-flight, deterministically (events, no sleeps).

    ``hold_next_get()`` arms the next ``get_policy``: it reads the stored value, then waits for ``release("get")``, so
    a refresh that has read ``off`` can be paused before it applies it. ``hold_next_set()`` arms the next
    ``set_policy``: it waits for ``release("set")`` BEFORE it writes. ``release()`` lets every held call go. A held
    call is never held for more than ``HOLD_LIMIT_S``."""

    HOLD_LIMIT_S = 10.0

    def __init__(self, monkeypatch):
        from semigraph.serve import store

        self._real_get, self._real_set = store.get_policy, store.set_policy
        self._armed = {"get": False, "set": False}
        self._lock = threading.Lock()
        self._released = {"get": threading.Event(), "set": threading.Event()}
        self.reached = {"get": threading.Event(), "set": threading.Event()}
        self.set_calls = 0
        monkeypatch.setattr(store, "get_policy", self._get)
        monkeypatch.setattr(store, "set_policy", self._set)

    def hold_next_get(self) -> None:
        self._armed["get"] = True

    def hold_next_set(self) -> None:
        self._armed["set"] = True

    def release(self, *kinds: str) -> None:
        for kind in kinds or self._released:
            self._released[kind].set()

    def _take(self, kind: str) -> bool:
        with self._lock:
            armed, self._armed[kind] = self._armed[kind], False
            return armed

    def _get(self, driver, key):
        value = self._real_get(driver, key)
        if self._take("get"):
            self.reached["get"].set()
            self._released["get"].wait(self.HOLD_LIMIT_S)
        return value

    def _set(self, driver, key, value):
        with self._lock:
            self.set_calls += 1
        if self._take("set"):
            self.reached["set"].set()
            self._released["set"].wait(self.HOLD_LIMIT_S)
        return self._real_set(driver, key, value)


class InThread:
    """``fn`` on a thread of its own; ``finish()`` joins it (bounded) and re-raises what it raised."""

    def __init__(self, fn):
        self.error: BaseException | None = None
        self.result = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread by finish()
            self.error = exc

    def finish(self, timeout: float = 10.0):
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the thread did not finish"
        if self.error is not None:
            raise self.error
        return self.result


class FakeLedger:
    """The ledger module's functions, in memory. ``fail_reserve`` / ``fail_settle`` / ``fail_renew``: raise
    ``ServiceUnavailable`` for the next n calls of that kind. ``settle_calls`` counts every settle attempt."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_reserve = self.fail_settle = self.fail_renew = 0
        self.settle_calls = 0
        self._lock = threading.Lock()

    def _maybe_fail(self, attribute: str) -> None:
        if getattr(self, attribute):
            setattr(self, attribute, getattr(self, attribute) - 1)
            raise ServiceUnavailable("the database is down")

    def reserve_row(self, driver, *, lease_id, day, ip_hash, ip_hash_v, strategy, workspace, estimate_micro, now_wall,
                    lease_until, machine_id, timeout_s):
        with self._lock:
            self._maybe_fail("fail_reserve")
            self.rows.setdefault(lease_id, {
                "id": lease_id, "day": day, "ts": now_wall, "status": "reserved", "cached": False, "strategy": strategy,
                "workspace": workspace, "ip_hash": ip_hash, "ip_hash_v": ip_hash_v, "estimate_micro": estimate_micro,
                "lease_until": lease_until, "machine_id": machine_id})

    def settle_row(self, driver, lease_id, *, outcome, usage, actual_micro, now_wall, timeout_s):
        with self._lock:
            self.settle_calls += 1
            self._maybe_fail("fail_settle")
            row = self.rows.get(lease_id)
            if row is None or row["status"] != "reserved":
                return False
            row.update(status="settled", outcome=outcome, settled_at=now_wall, cost_micro=actual_micro,
                       cost_usd=round(actual_micro / 1_000_000, 6), **(usage or {}))
            return True

    def renew_row(self, driver, lease_id, *, lease_until, timeout_s):
        with self._lock:
            self._maybe_fail("fail_renew")
            row = self.rows.get(lease_id)
            if row is None or row["status"] != "reserved":
                return False
            row["lease_until"] = lease_until
            return True

    def expire_reserved(self, driver, *, now_wall, grace_s, machine_id, timeout_s):
        n = 0
        with self._lock:
            for row in self.rows.values():
                expired = row.get("lease_until") is None or row["lease_until"] < now_wall - grace_s
                if row.get("status") == "reserved" and (expired or row["machine_id"] == machine_id):
                    row.update(status="settled", outcome="abandoned_restart", settled_at=now_wall,
                               cost_micro=row["estimate_micro"], cost_usd=row["estimate_micro"] / 1_000_000)
                    n += 1
        return n

    def day_sums(self, driver, *, day, now_wall, machine_id, ip_hash_v, timeout_s):
        paid = spend = foreign = 0
        per_ip: dict[str, int] = {}
        events: list[tuple[str, float]] = []
        with self._lock:
            for row in self.rows.values():
                if row["day"] != day or row.get("cached"):
                    continue
                status = row.get("status")
                if status is None or status == "settled":
                    legacy = round((row.get("cost_usd") or 0.0) * 1e6)
                    micro = row["cost_micro"] if row.get("cost_micro") is not None else legacy
                elif status == "reserved" and row["lease_until"] >= now_wall:
                    micro = row["estimate_micro"]
                    foreign += row["machine_id"] != machine_id
                else:
                    continue
                paid, spend = paid + 1, spend + micro
                if ip_hash_v is not None and row.get("ip_hash") and row.get("ip_hash_v") == ip_hash_v:
                    per_ip[row["ip_hash"]] = per_ip.get(row["ip_hash"], 0) + 1
                    events.append((row["ip_hash"], row["ts"]))
        return ledger.DaySums(paid, spend, foreign, per_ip, tuple(sorted(events)))


def build_inprocess(settings=None, *, fake_ledger=None, store=None, clock=None, **backend_kwargs):
    settings = settings or state_settings()
    fake_ledger, store = fake_ledger or FakeLedger(), store or FakeStoreDriver()
    clock = clock or FakeClock()
    backend = InProcessBackend(StateConfig.from_settings(settings), StateDrivers(state=store), ledger=fake_ledger,
                               wall=clock.wall, clock=clock.mono, **backend_kwargs)
    backend.refresh_kill_level()
    return backend, fake_ledger, store, clock


def ask(backend, clock, *, ip="ip-1", strategy="hybrid", workspace=False, estimate=60_000):
    return backend.reserve(ip_hash=ip, strategy=strategy, workspace=workspace, estimate_micro=estimate,
                           now_wall=clock.wall(), now_mono=clock.mono())


# ---------------------------------------------------------------------------------------------------- money

@pytest.mark.parametrize("usd, micro", [
    (0, 0), (0.06, 60_000), (10.0, 10_000_000), (0.0000005, 1), (0.0000004, 0), (0.0000015, 2), (0.0000025, 3),
    (1.2345675, 1_234_568), (Decimal("0.0000005"), 1), (3, 3_000_000), (0.1 + 0.2, 300_000), (1e-6, 1),
])
def test_usd_to_micro_rounds_half_up_on_the_micro(usd, micro):
    assert usd_to_micro(usd) == micro


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), Decimal("-1")])
def test_usd_to_micro_refuses_negative_and_non_finite_prices(bad):
    with pytest.raises(ValueError):
        usd_to_micro(bad)


@pytest.mark.parametrize("bad", ["0.06", None, True, [1]])
def test_usd_to_micro_refuses_non_numbers(bad):
    with pytest.raises(TypeError):
        usd_to_micro(bad)


def test_usd_to_micro_reads_a_float_subclass_through_its_plain_value():
    class Price(float):
        def __repr__(self):
            return f"Price({float(self)})"

    assert usd_to_micro(Price(0.06)) == 60_000


def test_micro_to_usd_is_exact_to_the_micro():
    assert micro_to_usd(60_000) == 0.06 and micro_to_usd(1) == 0.000001 and micro_to_usd(0) == 0.0


# ------------------------------------------------------------------------------------------- configuration

def test_state_config_reads_every_setting_and_converts_the_spend_cap_to_micro():
    config = StateConfig.from_settings(state_settings())
    assert (config.max_queries_per_day, config.max_spend_micro, config.paid_per_ip_per_day) == (150, 10_000_000, 20)
    assert config.machine_id == "m1" and config.ip_hash_version == 2


def test_state_config_names_a_missing_setting():
    settings = state_settings()
    del settings.lease_ttl_s
    with pytest.raises(ValueError, match="settings.lease_ttl_s"):
        StateConfig.from_settings(settings)


@pytest.mark.parametrize("field, value", [("max_queries_per_day", -1), ("max_spend_usd_per_day", -0.5),
                                          ("paid_per_ip_per_day", -1), ("max_concurrent_answers", -1),
                                          ("state_op_timeout_s", 0), ("lease_ttl_s", -1), ("kill_switch_stale_s", 0),
                                          ("lease_renew_s", 0), ("kill_switch_refresh_s", 0), ("machine_id", "")])
def test_state_config_refuses_values_that_would_disable_a_bound_by_accident(field, value):
    with pytest.raises(ValueError):
        StateConfig.from_settings(state_settings(**{field: value}))


def test_a_settings_object_without_the_pepper_version_is_accepted():
    settings = state_settings()
    del settings.ip_hash_version
    assert StateConfig.from_settings(settings).ip_hash_version is None


def test_make_backend_picks_the_named_backend_and_refuses_an_unknown_one():
    backend = make_backend(state_settings(), StateDrivers(state=FakeStoreDriver()))
    assert type(backend).__name__ == "InProcessBackend"
    assert type(make_backend(state_settings(state_backend="neo4j"), StateDrivers(state=FakeStoreDriver()))).__name__ \
        == "Neo4jBackend"
    with pytest.raises(ValueError, match="state_backend"):
        make_backend(state_settings(state_backend="valkey"), StateDrivers(state=FakeStoreDriver()))


# ------------------------------------------------------------------------------------- the error wrapper

def test_a_driver_error_becomes_state_unavailable_and_a_programming_error_does_not():
    backend, _, store, _ = build_inprocess()
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.cache_get("k", 24)
    store.fail = ValueError("a bug")
    with pytest.raises(ValueError):
        backend.cache_get("k", 24)


def test_an_os_error_and_a_timeout_are_driver_errors():
    backend, _, store, _ = build_inprocess()
    for error in (ConnectionResetError("reset"), TimeoutError("slow")):
        store.fail = error
        with pytest.raises(StateUnavailable):
            backend.cache_get("k", 24)


def test_a_server_error_is_logged_by_its_code_never_by_its_message(caplog):
    """A constraint violation's message quotes the offending node's properties, an address hash among them."""
    backend, _, store, _ = build_inprocess()
    store.fail = ClientError("Node(1) already exists with label SvcIpDay and properties ip_hash = SECRET-HASH")
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        with pytest.raises(StateUnavailable):
            backend.cache_get("k", 24)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "SECRET-HASH" not in logged and "ClientError" in logged and "Neo." in logged


def test_a_call_slower_than_a_second_is_logged_state_slow(caplog):
    ticks = iter([0.0, 1.5])
    backend = InProcessBackend(StateConfig.from_settings(state_settings()), StateDrivers(state=FakeStoreDriver()),
                               ledger=FakeLedger(), perf=lambda: next(ticks))
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        backend.cache_get("k", 24)
    assert any("state_slow" in record.message and "op=cache_get" in record.message for record in caplog.records)


def test_a_fast_call_is_not_logged_slow(caplog):
    ticks = iter([0.0, 0.2])
    backend = InProcessBackend(StateConfig.from_settings(state_settings()), StateDrivers(state=FakeStoreDriver()),
                               ledger=FakeLedger(), perf=lambda: next(ticks))
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        backend.cache_get("k", 24)
    assert not [r for r in caplog.records if "state_slow" in r.message]


def test_bounded_driver_puts_a_server_side_timeout_on_every_statement_store_runs():
    backend, _, store, _ = build_inprocess(state_settings(state_op_timeout_s=0.7))
    backend.cache_get("k", 24)
    backend.cache_put(question="q", strategy="hybrid", answer="a", citations=[], hallucinated=[])
    backend.set_kill_level("on")
    # the policy read of build_inprocess, the cache read, the cache write and the policy write
    assert len(store.queries) == 4 and {timeout for _, timeout in store.queries} == {0.7}


def test_cache_get_and_put_reuse_store_semantics():
    backend, _, store, _ = build_inprocess()
    assert backend.cache_get("missing", 24) is None
    backend.cache_put(question="What changed?", strategy="hybrid", answer="A", citations=["c1"], hallucinated=[])
    assert len(store.answers) == 1
    key = next(iter(store.answers))
    assert backend.cache_get(key, 24)["answer"] == "A"


def test_cache_put_is_best_effort(caplog):
    backend, _, store, _ = build_inprocess()
    store.fail = ServiceUnavailable("down")
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        backend.cache_put(question="q", strategy="hybrid", answer="a", citations=[], hallucinated=[])
    assert any("not cached" in record.message for record in caplog.records)


# ------------------------------------------------------------------------------------------ the kill level

def test_the_kill_level_is_on_until_it_has_been_read():
    store = FakeStoreDriver()
    config = StateConfig.from_settings(state_settings())
    backend = InProcessBackend(config, StateDrivers(state=store), ledger=FakeLedger())
    assert backend.kill_level() == "on" and backend.kill_age_s() is None
    assert backend.refresh_kill_level() == "off"
    assert backend.kill_level() == "off"


def test_a_stored_level_is_read_back_and_an_unknown_one_fails_closed(caplog):
    backend, _, store, _ = build_inprocess()
    for stored in ("on", "retrieval_only", "off"):
        store.policy["kill_switch"] = stored
        assert backend.refresh_kill_level() == stored
    store.policy["kill_switch"] = "maintenance"
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        assert backend.refresh_kill_level() == "on"


def test_a_cached_level_older_than_the_stale_bound_reads_as_on():
    backend, _, _, clock = build_inprocess()
    clock.advance(29)
    assert backend.kill_level() == "off"
    clock.advance(2)
    assert backend.kill_level() == "on"


def test_the_env_override_forces_on_whatever_the_database_says():
    backend, _, store, _ = build_inprocess(state_settings(kill_switch=True))
    store.policy["kill_switch"] = "off"
    backend.refresh_kill_level()
    assert backend.kill_level() == "on"


def test_set_kill_level_writes_the_policy_node_and_the_memory_cache():
    backend, _, store, _ = build_inprocess()
    backend.set_kill_level("retrieval_only")
    assert store.policy["kill_switch"] == "retrieval_only" and backend.kill_level() == "retrieval_only"
    backend.set_kill_level("off")
    assert store.policy["kill_switch"] == "off" and backend.kill_level() == "off"


def test_set_kill_level_rejects_a_level_it_does_not_know():
    backend, *_ = build_inprocess()
    with pytest.raises(ValueError):
        backend.set_kill_level("paused")


def test_a_tightening_applies_in_memory_even_when_the_database_write_fails_and_is_retried_by_the_refresh():
    backend, _, store, _ = build_inprocess()
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.set_kill_level("on")
    assert backend.kill_level() == "on"                      # applied anyway
    store.fail = None
    assert store.policy.get("kill_switch") is None
    backend.refresh_kill_level()                             # the maintenance thread's next tick writes it
    assert store.policy["kill_switch"] == "on" and backend.kill_level() == "on"


def test_a_pending_tightening_is_not_overwritten_by_an_older_stored_value():
    backend, _, store, _ = build_inprocess()
    store.policy["kill_switch"] = "off"
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.set_kill_level("retrieval_only")
    store.fail = None
    backend.refresh_kill_level()
    assert backend.kill_level() == "retrieval_only"


def test_a_relaxation_that_could_not_be_stored_never_opens_the_gate():
    backend, _, store, _ = build_inprocess()
    backend.set_kill_level("on")
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.set_kill_level("off")
    assert backend.kill_level() == "on"


def test_a_failed_refresh_leaves_the_cache_to_age_towards_on():
    backend, _, store, clock = build_inprocess()
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.refresh_kill_level()
    assert backend.kill_level() == "off"                      # still fresh enough
    clock.advance(31)
    assert backend.kill_level() == "on"


def test_a_refresh_held_after_its_read_applies_that_read_when_nothing_changed_in_between(monkeypatch):
    """The control for the race tests: the gate itself does not stop a refresh from doing its job."""
    backend, _, store, _ = build_inprocess()
    store.policy["kill_switch"] = "retrieval_only"
    gate = PolicyGate(monkeypatch)
    gate.hold_next_get()
    refresh = InThread(backend.refresh_kill_level)
    assert gate.reached["get"].wait(10)
    assert backend.kill_level() == "off"                      # not applied yet
    gate.release()
    assert refresh.finish() == "retrieval_only" and backend.kill_level() == "retrieval_only"


def test_a_refresh_that_read_while_a_relaxation_was_being_written_does_not_undo_it_when_it_lands(monkeypatch):
    """The refresh begins after the set did (so its generation matches), reads the old stored ``on``, and applies it
    only after the set has stored and applied ``off``: the completion of a set must invalidate it as well."""
    backend, _, store, _ = build_inprocess()
    backend.set_kill_level("on")
    gate = PolicyGate(monkeypatch)
    gate.hold_next_set()
    relax = InThread(lambda: backend.set_kill_level("off"))
    assert gate.reached["set"].wait(10)
    gate.hold_next_get()
    refresh = InThread(backend.refresh_kill_level)
    try:
        assert gate.reached["get"].wait(10)                  # the refresh has read "on" from the database
        gate.release("set")
        relax.finish()
        assert backend.kill_level() == "off"
    finally:
        gate.release()
    refresh.finish()
    assert backend.kill_level() == "off" and store.policy["kill_switch"] == "off"


def test_the_kill_level_is_answered_at_once_while_a_database_write_is_in_flight(monkeypatch):
    """The hot path of every paid ask touches only the memory lock, never the lock that serialises the writes."""
    backend, *_ = build_inprocess()
    backend.set_kill_level("on")
    gate = PolicyGate(monkeypatch)
    gate.hold_next_set()
    relax = InThread(lambda: backend.set_kill_level("off"))
    try:
        assert gate.reached["set"].wait(10)
        reader = InThread(backend.kill_level)
        assert reader.finish(timeout=2.0) == "on"                # the relaxation is not applied before it is stored
    finally:
        gate.release()
    relax.finish()
    assert backend.kill_level() == "off"


def test_a_pending_tightening_written_by_a_refresh_never_overwrites_a_later_relaxation(monkeypatch):
    backend, _, store, _ = build_inprocess()
    store.fail = ServiceUnavailable("down")
    with pytest.raises(StateUnavailable):
        backend.set_kill_level("on")                          # applied in memory, its database write pending
    store.fail = None
    gate = PolicyGate(monkeypatch)
    gate.hold_next_set()
    refresh = InThread(backend.refresh_kill_level)            # starts by writing the pending "on", held before it lands
    relax = None
    try:
        assert gate.reached["set"].wait(10)
        relax = InThread(lambda: backend.set_kill_level("off"))
        wait_until(lambda: backend._kill_gen >= 2)                                       # noqa: SLF001
    finally:
        gate.release()
    refresh.finish()
    relax.finish()
    assert backend.kill_level() == "off" and store.policy["kill_switch"] == "off"        # memory and database agree


def test_a_set_that_a_later_set_superseded_before_it_wrote_skips_its_write():
    backend, _, store, _ = build_inprocess()
    backend.refresh_kill_level()
    with backend._writer_lock:                                                          # noqa: SLF001
        first = InThread(lambda: backend.set_kill_level("on"))
        wait_until(lambda: backend._kill_gen >= 1)                                       # noqa: SLF001
        second = InThread(lambda: backend.set_kill_level("retrieval_only"))
        wait_until(lambda: backend._kill_gen >= 2)                                       # noqa: SLF001
    first.finish()
    second.finish()
    assert backend.kill_level() == "retrieval_only" and store.policy["kill_switch"] == "retrieval_only"


def wait_until(condition, timeout: float = 10.0) -> None:
    """Spin (yielding the GIL) until ``condition()``: it waits for a state the test has set up, not for a duration."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the awaited state was never reached"
        time.sleep(0.001)


# ------------------------------------------------------------------------------------- reserve: the order

def test_a_granted_reserve_returns_a_lease_and_writes_the_durable_row_before_returning():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, ip="ip-9", strategy="agent", workspace=True, estimate=123_456)
    assert isinstance(lease, Lease)
    assert (lease.day, lease.ip_hash, lease.strategy, lease.workspace, lease.estimate_micro, lease.machine_id) == (
        backend_module.day_of(clock.wall()), "ip-9", "agent", True, 123_456, "m1")
    assert lease.until_mono == clock.mono() + 60
    row = fake.rows[lease.lease_id]
    assert row["status"] == "reserved" and row["estimate_micro"] == 123_456 and row["lease_until"] == clock.wall() + 60
    assert row["ip_hash_v"] == 2 and row["machine_id"] == "m1" and row["cached"] is False


def test_the_lease_day_is_the_utc_day_of_now_wall():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock)
    assert lease.day == backend_module.day_of(DAY)


@pytest.mark.parametrize("caps, denial", [
    # every cap is saturated by the first ask; switching the earlier ones off shows the next in the order
    (dict(max_queries_per_day=1, max_spend_usd_per_day=0.05, paid_per_ip_per_day=1, max_concurrent_answers=1),
     Denied.DAILY_COUNT),
    (dict(max_queries_per_day=0, max_spend_usd_per_day=0.05, paid_per_ip_per_day=1, max_concurrent_answers=1),
     Denied.DAILY_SPEND),
    (dict(max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=1, max_concurrent_answers=1),
     Denied.IP_DAILY),
    (dict(max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=0, max_concurrent_answers=1),
     Denied.INFLIGHT),
])
def test_the_checks_run_in_the_documented_order(caps, denial):
    backend, _, _, clock = build_inprocess(state_settings(**caps))
    assert isinstance(ask(backend, clock, estimate=40_000), Lease)
    assert ask(backend, clock, estimate=40_000) is denial


def test_kill_is_checked_before_every_cap():
    backend, _, store, clock = build_inprocess(state_settings(max_queries_per_day=1))
    ask(backend, clock)
    backend.set_kill_level("on")
    assert ask(backend, clock) is Denied.KILL
    backend.set_kill_level("retrieval_only")
    assert ask(backend, clock) is Denied.KILL                        # paid asks are off at both restrictive levels


def test_a_denied_reserve_writes_nothing_and_changes_no_counter():
    backend, fake, _, clock = build_inprocess(state_settings(max_queries_per_day=1))
    ask(backend, clock)
    before = (len(fake.rows), backend.snapshot()["paid"], backend.snapshot()["spend_micro"])
    assert ask(backend, clock) is Denied.DAILY_COUNT
    assert (len(fake.rows), backend.snapshot()["paid"], backend.snapshot()["spend_micro"]) == before


def test_zero_caps_are_off_except_concurrency_where_zero_allows_nothing():
    backend, _, _, clock = build_inprocess(state_settings(max_queries_per_day=0, max_spend_usd_per_day=0,
                                                          paid_per_ip_per_day=0, max_concurrent_answers=1000))
    assert all(isinstance(ask(backend, clock, estimate=10**9), Lease) for _ in range(50))
    none_allowed, _, _, clock2 = build_inprocess(state_settings(max_concurrent_answers=0))
    assert ask(none_allowed, clock2) is Denied.INFLIGHT


def test_the_spend_cap_is_a_cap_on_spend_plus_estimate():
    backend, _, _, clock = build_inprocess(state_settings(max_spend_usd_per_day=1.0, max_concurrent_answers=100))
    granted = 0
    while isinstance(ask(backend, clock, ip=f"ip-{granted}", estimate=60_000), Lease):
        granted += 1
    assert granted == 16
    assert backend.snapshot()["spend_micro"] == 16 * 60_000


@pytest.mark.parametrize("bad", [-1, 1.5, "10"])
def test_an_estimate_that_is_not_a_non_negative_whole_number_of_micro_is_refused(bad):
    backend, _, _, clock = build_inprocess()
    with pytest.raises(ValueError):
        backend.reserve(ip_hash="ip", strategy="hybrid", workspace=False, estimate_micro=bad, now_wall=clock.wall(),
                        now_mono=clock.mono())


def test_a_failed_row_write_rolls_the_counters_back_and_denies_as_unavailable():
    backend, fake, _, clock = build_inprocess(state_settings(max_queries_per_day=1, max_concurrent_answers=1))
    fake.fail_reserve = 1
    assert ask(backend, clock) is Denied.UNAVAILABLE
    snapshot = backend.snapshot()
    assert (snapshot["paid"], snapshot["spend_micro"], snapshot["inflight"], snapshot["per_ip_max"]) == (0, 0, 0, 0)
    assert fake.rows == {}
    assert isinstance(ask(backend, clock), Lease)                      # the slot was not lost


# ------------------------------------------------------------------------------------------------ reconcile

def test_reconcile_with_the_real_cost_adjusts_spend_by_the_difference_and_settles_the_row():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage={"prompt_tokens": 7, "completion_tokens": 3},
                             cost_micro=1_500) is True
    assert backend.snapshot()["spend_micro"] == 1_500 and backend.snapshot()["inflight"] == 0
    row = fake.rows[lease.lease_id]
    assert (row["status"], row["outcome"], row["cost_micro"], row["prompt_tokens"]) == ("settled", "done", 1_500, 7)


def test_reconcile_with_an_unknown_cost_keeps_the_estimate():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=None) is True
    assert backend.snapshot()["spend_micro"] == 60_000 and fake.rows[lease.lease_id]["cost_micro"] == 60_000


def test_an_abandoned_outcome_charges_the_estimate_whatever_cost_is_passed():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    backend.reconcile(lease.lease_id, outcome="abandoned", usage=None, cost_micro=5)
    assert backend.snapshot()["spend_micro"] == 60_000 and fake.rows[lease.lease_id]["outcome"] == "abandoned"


def test_an_actual_cost_above_the_estimate_raises_the_spend():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=90_000)
    assert backend.snapshot()["spend_micro"] == 90_000


def test_a_second_reconcile_and_an_unknown_lease_charge_nothing():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=100) is True
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=100) is False
    assert backend.reconcile("no-such-lease", outcome="done", usage=None, cost_micro=100) is False
    assert backend.snapshot()["spend_micro"] == 100


def test_a_negative_cost_is_floored_not_subtracted():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=-5)
    assert backend.snapshot()["spend_micro"] == 0


# ----------------------------------------------------------------------------------- the settle retry queue

class TickingLedger(FakeLedger):
    """Every settle attempt takes ``cost`` seconds of the fake clock."""

    def __init__(self, clock, cost):
        super().__init__()
        self._clock, self._cost = clock, cost

    def settle_row(self, *args, **kwargs):
        self._clock.advance(self._cost)
        return super().settle_row(*args, **kwargs)


def queue_many(backend, fake, clock, count, **settings):
    """``count`` leases reconciled while the ledger refuses every settle: all of them end up in the retry queue."""
    leases = [ask(backend, clock, ip=f"ip-{n}", estimate=60_000) for n in range(count)]
    fake.fail_settle = 10**6
    for lease in leases:
        backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    return leases


def many(**overrides):
    return state_settings(max_concurrent_answers=100, paid_per_ip_per_day=0, max_queries_per_day=0, **overrides)


def test_a_failed_settle_returns_at_once_charges_the_counters_and_queues_the_row_write():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    fake.fail_settle = 1
    started = time.perf_counter()
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1) is True
    assert time.perf_counter() - started < 1.0                                    # the old retries slept 6 s
    assert fake.settle_calls == 1 and backend.pending_settles() == 1
    assert fake.rows[lease.lease_id]["status"] == "reserved"                       # the durable write is pending
    snapshot = backend.snapshot()
    assert (snapshot["spend_micro"], snapshot["inflight"]) == (1, 0)               # charged, and the slot is free


def test_the_queued_settle_lands_on_the_next_drain_and_the_queue_empties():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    fake.fail_settle = 1
    backend.reconcile(lease.lease_id, outcome="done", usage={"prompt_tokens": 4}, cost_micro=1_500)
    assert backend.drain_settles() == 1 and backend.pending_settles() == 0
    row = fake.rows[lease.lease_id]
    assert (row["status"], row["outcome"], row["cost_micro"], row["prompt_tokens"]) == ("settled", "done", 1_500, 4)
    assert fake.settle_calls == 2
    assert backend.drain_settles() == 0 and fake.settle_calls == 2                  # nothing queued: nothing attempted


def test_a_drain_stops_at_the_first_failure_because_the_store_is_down_and_keeps_the_order():
    backend, fake, _, clock = build_inprocess(many())
    leases = queue_many(backend, fake, clock, 3)
    assert fake.settle_calls == 3 and backend.pending_settles() == 3
    assert backend.drain_settles() == 0 and fake.settle_calls == 4 and backend.pending_settles() == 3
    fake.fail_settle = 0
    assert backend.drain_settles() == 3 and backend.pending_settles() == 0
    assert all(fake.rows[lease.lease_id]["status"] == "settled" for lease in leases)


def test_a_drain_attempts_a_bounded_number_of_entries_per_tick():
    backend, fake, _, clock = build_inprocess(many())
    queue_many(backend, fake, clock, settle_queue.DRAIN_PER_TICK + 5)
    fake.fail_settle = 0
    assert backend.drain_settles() == settle_queue.DRAIN_PER_TICK and backend.pending_settles() == 5
    assert backend.drain_settles() == 5 and backend.pending_settles() == 0


def test_a_queued_settle_is_given_up_after_the_attempt_limit_and_logged_never_lost_silently(caplog):
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock)
    fake.fail_settle = 10**6
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)                # attempt 1, inline
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        for _ in range(settle_queue.MAX_ATTEMPTS - 2):
            backend.drain_settles()
        assert backend.pending_settles() == 1 and not [r for r in caplog.records if r.levelno >= logging.ERROR]
        backend.drain_settles()                                                                 # the last attempt
    assert backend.pending_settles() == 0 and fake.settle_calls == settle_queue.MAX_ATTEMPTS
    gave_up = [r.message for r in caplog.records if "state_settle_abandoned" in r.message]
    assert len(gave_up) == 1 and lease.lease_id in gave_up[0] and "attempts" in gave_up[0]
    assert fake.rows[lease.lease_id]["status"] == "reserved"                    # the next boot charges its estimate


def test_a_queued_settle_older_than_the_age_limit_is_given_up_without_another_attempt(caplog):
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock)
    fake.fail_settle = 10**6
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    clock.advance(settle_queue.MAX_AGE_S + 1)
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        assert backend.drain_settles() == 0
    assert backend.pending_settles() == 0 and fake.settle_calls == 1
    assert any("state_settle_abandoned" in r.message and lease.lease_id in r.message for r in caplog.records)


def test_a_full_queue_refuses_the_new_entry_with_an_error_and_never_evicts_an_old_one(caplog):
    backend, fake, _, clock = build_inprocess(many(), settle_queue_max=2)
    leases = [ask(backend, clock, ip=f"ip-{n}") for n in range(3)]
    fake.fail_settle = 10**6
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        for lease in leases:
            assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1) is True
    assert backend.pending_settles() == 2
    refused = [r.message for r in caplog.records if "state_settle_dropped" in r.message]
    assert len(refused) == 1 and leases[2].lease_id in refused[0] and "queue_full" in refused[0]
    fake.fail_settle = 0
    assert backend.drain_settles() == 2
    assert [fake.rows[lease.lease_id]["status"] for lease in leases] == ["settled", "settled", "reserved"]


def test_flush_settles_writes_what_it_can_within_the_budget_and_logs_what_is_left(caplog):
    clock = FakeClock()
    ledger_fake = TickingLedger(clock, cost=1.0)
    backend, fake, _, _ = build_inprocess(many(), fake_ledger=ledger_fake, clock=clock)
    queue_many(backend, fake, clock, 5)
    fake.fail_settle = 0
    with caplog.at_level(logging.ERROR, logger="semigraph.serve.state"):
        flushed = backend.flush_settles(budget_s=2.5)
    # attempts at t=0, 1 and 2; at t=3 the budget is out
    assert flushed == 3 and backend.pending_settles() == 2
    assert any("state_settle_unflushed" in r.message and "count=2" in r.message for r in caplog.records)


def test_flush_settles_stops_at_the_first_failure():
    backend, fake, _, clock = build_inprocess(many())
    queue_many(backend, fake, clock, 3)
    calls_before = fake.settle_calls
    assert backend.flush_settles(budget_s=60.0) == 0
    assert fake.settle_calls == calls_before + 1 and backend.pending_settles() == 3


def test_a_boot_charges_the_estimate_of_a_row_whose_settle_never_landed():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    fake.fail_settle = 10**6
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    fake.fail_settle = 0
    reborn, *_ = build_inprocess(fake_ledger=fake, clock=clock)                       # the process restarted
    report = reborn.rebuild_from_ledger()
    assert fake.rows[lease.lease_id]["outcome"] == "abandoned_restart" and report.spend_micro == 60_000


# ----------------------------------------------------------------------------------------- renew and sweep

def test_renew_writes_lease_until_on_the_row_and_extends_the_wall_clock_expiry():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock)
    clock.advance(40)
    assert backend.renew(lease.lease_id, clock.wall()) is True
    assert fake.rows[lease.lease_id]["lease_until"] == clock.wall() + 60
    assert backend.sweep(clock.wall() + 30) == 0                 # 40 + 60 - 30: still alive


def test_renew_of_a_settled_or_unknown_lease_is_false():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock)
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    assert backend.renew(lease.lease_id, clock.wall()) is False
    assert backend.renew("nope", clock.wall()) is False


def test_a_failed_renew_raises_state_unavailable_for_the_maintenance_thread_to_log():
    backend, fake, _, clock = build_inprocess()
    lease = ask(backend, clock)
    fake.fail_renew = 1
    with pytest.raises(StateUnavailable):
        backend.renew(lease.lease_id, clock.wall())


def test_sweep_charges_the_estimate_of_an_expired_unregistered_lease_and_frees_the_slot():
    backend, fake, _, clock = build_inprocess(state_settings(max_concurrent_answers=1))
    lease = ask(backend, clock, estimate=60_000)
    assert ask(backend, clock) is Denied.INFLIGHT
    assert backend.sweep(clock.wall() + 59) == 0
    assert backend.sweep(clock.wall() + 61) == 1
    row = fake.rows[lease.lease_id]
    assert (row["status"], row["outcome"], row["cost_micro"]) == ("settled", "abandoned", 60_000)
    assert backend.snapshot()["spend_micro"] == 60_000 and backend.snapshot()["inflight"] == 0
    assert isinstance(ask(backend, clock), Lease)


def test_sweep_leaves_a_started_lease_to_the_maintenance_renewals():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock)
    backend.mark_started(lease.lease_id)
    assert backend.sweep(clock.wall() + 10_000) == 0
    assert backend.snapshot()["inflight"] == 1
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    assert lease.lease_id not in backend.registry                 # a finished lease leaves the registry


def test_mark_started_of_a_finished_lease_does_not_re_register_it():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock)
    backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1)
    backend.mark_started(lease.lease_id)
    assert backend.registry.active() == []


def test_a_reconcile_after_a_sweep_charges_nothing_more():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock, estimate=60_000)
    backend.sweep(clock.wall() + 100)
    assert backend.reconcile(lease.lease_id, outcome="done", usage=None, cost_micro=1) is False
    assert backend.snapshot()["spend_micro"] == 60_000


# ----------------------------------------------------------------------------------------------- the day

def test_a_stream_crossing_midnight_reconciles_into_its_own_day_and_the_new_day_starts_at_zero():
    backend, _, _, clock = build_inprocess(state_settings(max_concurrent_answers=5),
                                           clock=FakeClock(wall=MIDNIGHT_NEXT - 10))      # 23:59:50 of day D
    first = ask(backend, clock, estimate=60_000)
    second = ask(backend, clock, ip="ip-2", estimate=60_000)
    clock.advance(30)                                                                         # day D+1
    backend.refresh_kill_level()                                    # what the maintenance thread does every 10 s
    assert backend.snapshot()["day"] != first.day
    assert (backend.snapshot()["paid"], backend.snapshot()["spend_micro"]) == (0, 0)
    today = ask(backend, clock, ip="ip-3", estimate=10_000)
    assert today.day == backend.snapshot()["day"] != first.day
    backend.reconcile(first.lease_id, outcome="done", usage=None, cost_micro=1_000)
    # D's settle left today alone
    assert (backend.snapshot()["paid"], backend.snapshot()["spend_micro"]) == (1, 10_000)
    counter = backend._counters[first.day]                                                   # noqa: SLF001
    # D's own counter took the delta
    assert (counter.paid, counter.spend_micro) == (2, 1_000 + 60_000)
    assert second.day == first.day


def test_a_day_counter_is_kept_while_a_lease_references_it_and_dropped_after():
    clock = FakeClock(wall=MIDNIGHT_NEXT - 10)
    backend, _, _, clock = build_inprocess(clock=clock)
    old = ask(backend, clock)
    clock.advance(30)
    backend.refresh_kill_level()
    ask(backend, clock, ip="ip-2")
    assert old.day in backend._counters                                       # noqa: SLF001
    backend.reconcile(old.lease_id, outcome="done", usage=None, cost_micro=1)
    assert old.day not in backend._counters                                   # noqa: SLF001 - no lease left on that day


def test_per_ip_counts_are_per_day_and_per_hash():
    backend, _, _, clock = build_inprocess(state_settings(paid_per_ip_per_day=2, max_concurrent_answers=10))
    assert [type(ask(backend, clock, ip="a")).__name__ for _ in range(3)] == ["Lease", "Lease", "Denied"]
    assert isinstance(ask(backend, clock, ip="b"), Lease)
    clock.advance(86400)
    backend.refresh_kill_level()
    assert isinstance(ask(backend, clock, ip="a"), Lease)


# ---------------------------------------------------------------------------------------------- snapshot

def test_snapshot_reports_the_counters_the_leases_and_the_kill_state_but_no_ip_hash():
    backend, _, _, clock = build_inprocess()
    lease = ask(backend, clock, ip="secret-ip-hash", strategy="agent", estimate=50_000)
    backend.mark_started(lease.lease_id)
    snapshot = backend.snapshot()
    assert snapshot["paid"] == 1 and snapshot["spend_micro"] == 50_000 and snapshot["inflight"] == 1
    assert snapshot["per_ip_max"] == 1 and snapshot["kill"] == "off" and snapshot["backend"] == "inprocess"
    assert snapshot["kill_age_s"] == 0.0 and snapshot["day"] == lease.day
    (entry,) = snapshot["leases"]
    assert entry["lease_id"] == lease.lease_id and entry["started"] is True and entry["strategy"] == "agent"
    assert "secret-ip-hash" not in repr(snapshot)


# ----------------------------------------------------------------------------------------- boot rebuild

def seed_row(fake, **fields):
    row = {"id": fields.pop("id", f"row-{len(fake.rows)}"), "day": backend_module.day_of(DAY), "cached": False,
           "status": "settled", "ts": DAY, "ip_hash": "ip-1", "ip_hash_v": 2, "machine_id": "old",
           "estimate_micro": 60_000,
           "cost_micro": 1_000, "lease_until": DAY + 60}
    row.update(fields)
    fake.rows[row["id"]] = {k: v for k, v in row.items() if v is not None}
    return row["id"]


def test_the_boot_rebuild_charges_a_row_a_dead_process_left_reserved_its_estimate():
    fake = FakeLedger()
    seed_row(fake, id="orphan", status="reserved", machine_id="m1", estimate_micro=60_000, cost_micro=None)
    backend, *_ = build_inprocess(fake_ledger=fake)
    report = backend.rebuild_from_ledger()
    assert fake.rows["orphan"]["outcome"] == "abandoned_restart" and fake.rows["orphan"]["cost_micro"] == 60_000
    assert (report.expired, report.paid, report.spend_micro) == (1, 1, 60_000)
    assert backend.snapshot()["paid"] == 1 and backend.snapshot()["spend_micro"] == 60_000


def test_the_boot_rebuild_counts_rows_written_before_the_state_columns_existed():
    fake = FakeLedger()
    for n in range(3):                                         # what store.log_query wrote: no status, a dollar cost
        seed_row(fake, id=f"legacy-{n}", status=None, cost_micro=None, cost_usd=0.012345, ip_hash_v=None)
    seed_row(fake, id="cached", cached=True, cost_micro=0)     # a cached answer is not a paid ask
    seed_row(fake, id="yesterday", day="1999-12-31", cost_micro=999)
    backend, *_ = build_inprocess(fake_ledger=fake)
    report = backend.rebuild_from_ledger()
    assert (report.paid, report.spend_micro) == (3, 3 * 12_345)
    assert backend.snapshot()["spend_micro"] == 37_035


def test_the_boot_rebuild_respects_another_machines_live_lease_and_expires_a_long_dead_one():
    fake = FakeLedger()
    clock = FakeClock()
    seed_row(fake, id="live-b", status="reserved", machine_id="other", lease_until=clock.wall() + 30, cost_micro=None)
    seed_row(fake, id="dead-c", status="reserved", machine_id="other", lease_until=clock.wall() - 31, cost_micro=None)
    seed_row(fake, id="recent", status="reserved", machine_id="other", lease_until=clock.wall() - 29, cost_micro=None)
    backend, *_ = build_inprocess(fake_ledger=fake, clock=clock)
    report = backend.rebuild_from_ledger()
    assert fake.rows["live-b"]["status"] == "reserved" and fake.rows["recent"]["status"] == "reserved"
    assert fake.rows["dead-c"]["outcome"] == "abandoned_restart"        # older than twice lease_renew_s (30 s)
    assert report.expired == 1 and report.foreign_leases == 1          # only live-b is another machine's LIVE lease
    # live-b at its estimate, dead-c charged its estimate
    assert (report.paid, report.spend_micro) == (2, 120_000)


def test_the_boot_rebuild_returns_per_ip_counts_and_window_events_for_the_current_pepper_only():
    fake = FakeLedger()
    seed_row(fake, id="a1", ip_hash="aa", ts=DAY - 100)
    seed_row(fake, id="a2", ip_hash="aa", ts=DAY - 50)
    seed_row(fake, id="b1", ip_hash="bb", ts=DAY - 10)
    seed_row(fake, id="old-pepper", ip_hash="cc", ip_hash_v=1)
    seed_row(fake, id="nulled", ip_hash=None, ip_hash_v=0)
    backend, _, _, clock = build_inprocess(fake_ledger=fake)
    report = backend.rebuild_from_ledger()
    assert dict(report.per_ip) == {"aa": 2, "bb": 1}
    assert backend.snapshot()["per_ip_max"] == 2
    events = report.window_events(window_s=3600)
    assert sorted(events) == ["aa", "bb"]
    assert events["aa"] == [clock.mono() - 100, clock.mono() - 50]        # in the limiter's monotonic clock
    assert report.window_events(window_s=60) == {"aa": [clock.mono() - 50], "bb": [clock.mono() - 10]}
    assert report.window_events(window_s=20) == {"bb": [clock.mono() - 10]}


def test_the_rebuilt_counters_enforce_the_per_ip_cap_straight_away():
    fake = FakeLedger()
    for n in range(20):
        seed_row(fake, id=f"r{n}", ip_hash="aa", ts=DAY - n)
    backend, _, _, clock = build_inprocess(fake_ledger=fake)
    backend.rebuild_from_ledger()
    # serving starts after the maintenance thread's first read
    backend.refresh_kill_level()
    assert ask(backend, clock, ip="aa") is Denied.IP_DAILY
    assert isinstance(ask(backend, clock, ip="bb"), Lease)


def test_the_rebuild_resets_the_kill_cache_so_serving_starts_closed_until_the_first_read():
    backend, *_ = build_inprocess()
    assert backend.kill_level() == "off"
    backend.rebuild_from_ledger()
    assert backend.kill_level() == "on"


def test_the_rebuild_is_a_boot_operation_and_refuses_to_run_over_live_leases():
    backend, _, _, clock = build_inprocess()
    ask(backend, clock)
    with pytest.raises(RuntimeError, match="boot"):
        backend.rebuild_from_ledger()


def test_a_rebuild_that_cannot_reach_the_database_raises_state_unavailable():
    class Down(FakeLedger):
        def expire_reserved(self, *a, **k):
            raise ServiceUnavailable("down")

    backend, *_ = build_inprocess(fake_ledger=Down())
    with pytest.raises(StateUnavailable):
        backend.rebuild_from_ledger()


# ------------------------------------------------------------------------------- threads (no server)

def test_concurrent_reserves_never_exceed_the_caps_and_every_grant_has_a_row():
    backend, fake, _, clock = build_inprocess(state_settings(max_queries_per_day=150, max_concurrent_answers=1000,
                                                             paid_per_ip_per_day=0, max_spend_usd_per_day=0))
    results: list = []
    barrier = threading.Barrier(200)

    def worker(n):
        barrier.wait()
        results.append(ask(backend, clock, ip=f"ip-{n}"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = [r for r in results if isinstance(r, Lease)]
    assert len(granted) == 150 and len(fake.rows) == 150
    assert [r for r in results if not isinstance(r, Lease)] == [Denied.DAILY_COUNT] * 50
    assert backend.snapshot()["paid"] == 150


# ------------------------------------------------------------------------------------- the state driver

def driver_settings(**overrides):
    values = dict(neo4j_uri="bolt://localhost:1", neo4j_user="neo4j", neo4j_password="not-a-secret",  # gitleaks:allow
                  neo4j_database="neo4j", state_op_timeout_s=1.0, state_connection_acquisition_s=0.5)
    return SimpleNamespace(**{**values, **overrides})


def test_make_state_driver_is_a_second_bounded_driver_pinned_to_the_configured_database(monkeypatch):
    from semigraph.graph import client

    captured = {}

    class Recorded:
        def close(self):
            captured["closed"] = True

    def fake_driver(uri, **kwargs):
        captured.update(uri=uri, **kwargs)
        return Recorded()

    monkeypatch.setattr(client.GraphDatabase, "driver", fake_driver)
    driver = client.make_state_driver(driver_settings(neo4j_database="sgtest", state_op_timeout_s=0.8,
                                                      state_connection_acquisition_s=0.25))
    assert isinstance(driver, client.DatabaseDriver) and driver.database == "sgtest"
    assert captured["uri"] == "bolt://localhost:1" and captured["auth"] == ("neo4j", "not-a-secret")
    assert captured["max_connection_pool_size"] == 8
    # the configured 0.25 s is below the 0.37 s that half the 0.8 s budget leaves: it stays the ceiling
    assert captured["connection_acquisition_timeout"] == captured["connection_timeout"] == 0.25
    assert captured["max_transaction_retry_time"] == 0.2 and captured["initial_retry_delay"] == 0.05
    # every acquire proves the pooled connection alive first, inside the attempt timeout: a connection whose server
    # went silent is dropped in 0.47 s instead of being read from until the server's 120 s receive-timeout hint
    assert captured["liveness_check_timeout"] == 0


def test_the_attempt_timeout_is_the_configured_one_capped_so_two_attempts_and_a_delay_fit_the_budget():
    from semigraph.graph import client

    assert client.state_attempt_timeout_s(1.0, 0.5) == pytest.approx(0.47)               # the production defaults
    assert client.state_attempt_timeout_s(1.0, 0.2) == 0.2                                 # a lower setting is kept
    assert client.state_attempt_timeout_s(0.1, 0.5) == client.MIN_ATTEMPT_S               # never below the floor
    for budget in (0.5, 0.8, 1.0, 2.0):
        attempt = client.state_attempt_timeout_s(budget, 10.0)
        longest_delay = client.STATE_RETRY_DELAY_S * client.RETRY_DELAY_JITTER
        assert 2 * attempt + longest_delay <= budget + 1e-9


def test_make_state_driver_gives_a_managed_transaction_at_most_the_operation_budget_on_paper(monkeypatch):
    """Two connection attempts plus the longest first retry delay, from the settings the driver is built with."""
    from semigraph.graph import client

    captured = {}
    monkeypatch.setattr(client.GraphDatabase, "driver", lambda uri, **kwargs: captured.update(kwargs) or object())
    client.make_state_driver(driver_settings())
    worst = 2 * captured["connection_acquisition_timeout"] + captured["initial_retry_delay"] * client.RETRY_DELAY_JITTER
    assert worst <= 1.0 + 1e-9 and captured["connection_acquisition_timeout"] == pytest.approx(0.47)


def test_make_state_driver_does_not_connect_at_construction(monkeypatch):
    from semigraph.graph import client

    def forbidden(self, *a, **k):
        raise AssertionError("make_state_driver must not open a connection")

    monkeypatch.setattr("neo4j.Driver.verify_connectivity", forbidden)
    driver = client.make_state_driver(driver_settings())
    driver.close()


def test_make_state_driver_keywords_are_the_ones_the_installed_driver_accepts():
    """An unknown keyword is a ConfigurationError at construction, so building the real driver proves the names; the
    pool values it then reports prove they were applied (private attributes: skipped if a driver release renames
    them)."""
    from semigraph.graph import client

    driver = client.make_state_driver(driver_settings(state_op_timeout_s=0.9, state_connection_acquisition_s=0.4))
    try:
        pool = getattr(driver.wrapped, "_pool", None)
        if pool is None:
            pytest.skip("the driver keeps no _pool attribute")
        assert pool.pool_config.max_connection_pool_size == 8 and pool.pool_config.connection_timeout == 0.4
        assert pool.workspace_config.connection_acquisition_timeout == 0.4
        assert pool.workspace_config.max_transaction_retry_time == 0.2
        assert pool.workspace_config.initial_retry_delay == 0.05
        assert pool.pool_config.liveness_check_timeout == 0
    finally:
        driver.close()


def test_make_state_driver_falls_back_to_named_defaults_when_the_settings_do_not_have_the_state_fields_yet():
    from semigraph.graph import client

    settings = driver_settings()
    del settings.state_op_timeout_s, settings.state_connection_acquisition_s
    driver = client.make_state_driver(settings)
    driver.close()
    assert client.STATE_OP_TIMEOUT_DEFAULT_S == 1.0 and client.STATE_ACQUISITION_DEFAULT_S == 0.5


# ------------------------------------------------------------------------------- the server side of the bound

def test_the_neo4j_app_sets_the_transaction_monitor_interval_under_the_name_the_server_knows():
    """The server enforces a transaction timeout only when its transaction monitor looks, every
    ``db.transaction.monitor.check.interval`` (2 s by default): the state bound of about 1.1 s needs 100 ms. The
    official image turns ``_`` into ``.`` and ``__`` into ``_`` in an ``NEO4J_`` variable, and the key has no
    underscore in it, so the variable has single underscores only: with a double one the key would be unknown, which
    strict config validation refuses at boot."""
    import tomllib
    from pathlib import Path

    config = tomllib.loads((Path(__file__).resolve().parents[1] / "deploy/neo4j/fly.toml").read_text(encoding="utf-8"))
    settings = {name.removeprefix("NEO4J_").replace("__", "\0").replace("_", ".").replace("\0", "_"): value
                for name, value in config["env"].items() if name.startswith("NEO4J_")}
    assert settings["db.transaction.monitor.check.interval"] == "100ms"
