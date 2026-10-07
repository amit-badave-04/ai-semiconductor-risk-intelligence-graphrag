"""serve/main.py lifespan: the agent import at boot (fail fast), the async answer path's runtime, the paid-ask state
(M5a I4: built, rebuilt from the ledger, the windows seeded, the maintenance thread started and stopped in order) and
the tracer's lifecycle.

Neo4j and the embedder are never touched: ``bootstrap`` is replaced by a stub that records that it ran, and the state
seams of ``main`` (``build_state``, ``MaintenanceThread``) by fakes. ``Boot.order`` records the events the lifespan
tests have always pinned; the state's own events go to ``Boot.state_order`` and, in the ordering tests, to ``order`` too
(``boot_in_order``). The agent package is a stub too: a temporary ``semigraph.agent`` whose ``stream_async`` module (the
module the ask route serves agent questions with) raises the way a missing langgraph would."""

import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture

from semigraph.config import Settings
from semigraph.serve import drain, main, routes, workspace_routes
from semigraph.serve.state import RebuildReport, StateUnavailable
from semigraph.serve.state.backend import BoundedDriver

pytestmark = pytest.mark.usefixtures("fresh_drain")

AGENT_STREAM = "semigraph.agent.stream_async"


def a_report(*, now_wall: float | None = None, ip_events=(), paid=0, spend_micro=0, expired=0) -> RebuildReport:
    now_wall = time.time() if now_wall is None else now_wall
    return RebuildReport(day="2026-10-06", paid=paid, spend_micro=spend_micro, per_ip={}, expired=expired,
                         foreign_leases=0, counters_synced=None, delta=None, ip_events=tuple(ip_events),
                         now_wall=now_wall, now_mono=time.monotonic())


class FakeBackend:
    """The state backend the lifespan builds: its rebuild answers ``report`` (or raises what ``rebuild_errors``
    queues)."""

    def __init__(self, boot, report):
        self._boot, self.report, self.rebuild_errors = boot, report, []
        self.registry = None

    def rebuild_from_ledger(self):
        self._boot.note_state("state.rebuild")
        if self.rebuild_errors:
            raise self.rebuild_errors.pop(0)
        return self.report


class FakeMaintenance:
    """``MaintenanceThread(backend, settings)``: records its start and stop."""

    boot = None
    start_error = None

    def __init__(self, backend, settings):
        self.backend, self.settings = backend, settings

    def start(self):
        type(self).boot.note_state("maintenance.start")
        if type(self).start_error is not None:
            raise type(self).start_error

    def stop(self, timeout=5.0):
        type(self).boot.note_state("maintenance.stop")
        return True

    def is_alive(self):
        return True


class Boot:
    """The stand-ins for bootstrap, the driver and the state; ``order`` records what happened in which order."""

    def __init__(self, in_order: bool = False):
        self.order: list[str] = []
        self.state_order: list[str] = []
        self._in_order = in_order
        self.driver = types.SimpleNamespace(close=lambda: self.order.append("driver.close"))
        self.state_driver = types.SimpleNamespace(close=lambda: self.note_state("state_driver.close"))
        self.backend = FakeBackend(self, a_report())

    def note_state(self, event: str) -> None:
        self.state_order.append(event)
        if self._in_order:
            self.order.append(event)

    def bootstrap(self, settings):
        self.order.append("bootstrap")
        return self.driver, types.SimpleNamespace(name="fake-embedder"), {"nodes": {}}, None, frozenset()

    def build_state(self, settings):
        self.note_state("state.build")
        return self.state_driver, self.backend


def install_boot(monkeypatch, boot: Boot) -> Boot:
    monkeypatch.setattr(main, "bootstrap", boot.bootstrap)
    monkeypatch.setattr(main, "build_state", boot.build_state)
    monkeypatch.setattr(main, "MaintenanceThread", FakeMaintenance)
    monkeypatch.setattr(main, "REBUILD_RETRY_SLEEP_S", 0)
    FakeMaintenance.boot, FakeMaintenance.start_error = boot, None
    return boot


@pytest.fixture
def boot(monkeypatch):
    return install_boot(monkeypatch, Boot())


@pytest.fixture
def boot_in_order(monkeypatch):
    """The same stand-ins, with the state's events interleaved into ``order``."""
    return install_boot(monkeypatch, Boot(in_order=True))


def use_settings(monkeypatch, **over):
    settings = Settings(_env_file=None, **over)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return settings


def stub_agent_package(monkeypatch, tmp_path, source: str) -> None:
    """A temporary ``semigraph.agent`` package whose ``stream_async`` module is ``source``. The module's entry in
    ``sys.modules`` is set and then deleted: it is empty while the test runs, and whatever the boot check (or the route)
    imports under that name is dropped again afterwards, with the real module put back if it was loaded (a bare
    ``delitem`` of an absent key records nothing, so the stub would stay in ``sys.modules`` for every later test)."""
    (tmp_path / "stream_async.py").write_text(source, encoding="utf-8")
    package = types.ModuleType("semigraph.agent")
    package.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, "semigraph.agent", package)
    monkeypatch.setitem(sys.modules, AGENT_STREAM, None)
    monkeypatch.delitem(sys.modules, AGENT_STREAM)


@pytest.fixture
def raising_agent(monkeypatch, tmp_path):
    """``semigraph.agent.stream_async`` exists on disk but fails to import, as when langgraph is not installed."""
    stub_agent_package(monkeypatch, tmp_path, "raise ImportError(\"No module named 'langgraph'\")\n")


@pytest.fixture
def working_agent(monkeypatch, tmp_path):
    stub_agent_package(monkeypatch, tmp_path, "aagent_answer_stream = object()\n")


# ---------------------------------------------------------------- requirement 5: fail fast

def test_an_enabled_agent_that_cannot_be_imported_stops_the_service_at_boot_before_the_database(monkeypatch, boot, raising_agent, caplog):
    use_settings(monkeypatch, agent_enabled=True)
    with caplog.at_level("ERROR", logger="semigraph.serve.main"):
        with pytest.raises(ImportError, match="langgraph"):
            with TestClient(main.create_app()):
                pytest.fail("the service must not start")
    assert boot.order == []                                                    # bootstrap (the 90 s Neo4j retry) never ran
    assert any("AGENT_ENABLED" in r.getMessage() and "agent" in r.getMessage() for r in caplog.records)   # an actionable line


def test_a_disabled_agent_is_never_imported(monkeypatch, boot, raising_agent):
    use_settings(monkeypatch, agent_enabled=False)
    with TestClient(main.create_app()) as client:
        assert client.app.state.settings.agent_enabled is False
    assert AGENT_STREAM not in sys.modules               # the stub would have raised: it was never attempted
    assert boot.order == ["bootstrap", "driver.close"]


def test_an_enabled_agent_that_imports_cleanly_boots_and_stays_imported(monkeypatch, boot, working_agent):
    use_settings(monkeypatch, agent_enabled=True)
    with TestClient(main.create_app()):
        assert AGENT_STREAM in sys.modules
    assert boot.order == ["bootstrap", "driver.close"]


def test_the_boot_check_imports_the_module_the_route_serves_agent_questions_with(monkeypatch, boot, working_agent):
    """M5a I2: the boot check must exercise the very import a served agent question makes, or a missing langgraph would
    still fail on the first question instead of at boot."""
    use_settings(monkeypatch, agent_enabled=True)
    assert main.AGENT_MODULE == AGENT_STREAM
    with TestClient(main.create_app()):
        booted = sys.modules[AGENT_STREAM]
    assert routes._stream_fn("agent") is booted.aagent_answer_stream


# ---------------------------------------------------------------- the tracer is created once, stored, and shut down before the driver

class FakeTracer:
    enabled = True

    def __init__(self, order, fail=False):
        self.order, self.fail = order, fail

    def shutdown(self):
        self.order.append("tracer.shutdown")
        if self.fail:
            raise RuntimeError("flush failed")


def test_the_tracer_is_created_once_from_the_settings_stored_on_the_app_and_shut_down_before_the_driver(monkeypatch, boot):
    settings, made = use_settings(monkeypatch), []
    tracer = FakeTracer(boot.order)

    def get_tracer(s):
        made.append(s)
        boot.order.append("get_tracer")
        return tracer
    monkeypatch.setattr(main.tracing, "get_tracer", get_tracer)
    with TestClient(main.create_app()) as client:
        assert client.app.state.tracer is tracer
    assert made == [settings]
    assert boot.order == ["bootstrap", "get_tracer", "tracer.shutdown", "driver.close"]


def test_a_tracer_that_fails_at_shutdown_never_keeps_the_driver_open(monkeypatch, boot):
    use_settings(monkeypatch)
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: FakeTracer(boot.order, fail=True))
    with TestClient(main.create_app()):
        pass
    assert boot.order[-2:] == ["tracer.shutdown", "driver.close"]


def test_the_lifespan_builds_the_async_path_runtime_and_stops_the_lag_monitor(monkeypatch, boot):
    """M5a I2: the embedder is wrapped (one bound + a vector cache), the named limiters exist, the loop monitor runs."""
    from semigraph.serve.embed import LimitedEmbedder
    from semigraph.serve.limiters import Limiters

    use_settings(monkeypatch, embed_slots=2, db_thread_limit=9, loop_lag_warn_ms=50)
    with TestClient(main.create_app()) as client:
        st = client.app.state
        assert isinstance(st.embedder, LimitedEmbedder) and st.embedder.name == "fake-embedder"   # attributes pass through
        assert isinstance(st.limiters, Limiters)
        assert st.limiters.embed.total_tokens == 2 and st.limiters.db.total_tokens == 9
        assert st.loop_lag.warn_ms == 50
    # shut down cleanly: the driver still closed and no monitor task is left running
    assert boot.order[-1] == "driver.close"


def test_the_in_flight_cap_is_the_backends_so_the_lifespan_builds_no_limiter_or_slot_for_it(monkeypatch, boot):
    """M5a I4: the cap on paid answers in flight is ``max_concurrent_answers`` inside ``state.reserve`` (a pre-stream
    429); ``answer_limiter`` (I2) and ``answer_slots`` (the sync path) are both gone."""
    use_settings(monkeypatch, max_concurrent_answers=1)
    with TestClient(main.create_app()) as client:
        st = client.app.state
        assert not hasattr(st, "answer_limiter") and not hasattr(st, "answer_slots")
        assert st.state is boot.backend and st.settings.max_concurrent_answers == 1


def test_without_langfuse_keys_the_app_gets_the_no_op_tracer(monkeypatch, boot):
    use_settings(monkeypatch)
    with TestClient(main.create_app()) as client:
        assert client.app.state.tracer.enabled is False


# ---------------------------------------------------------------- R2: tracer shutdown is bounded, the driver closes regardless

class HungTracer:
    """A tracer whose ``shutdown`` never returns, as a hung Langfuse endpoint would. Deliberately never released: the test
    must prove teardown moves on WITHOUT waiting for it, not that it completes late."""

    enabled = True

    def shutdown(self):
        threading.Event().wait()  # a fresh Event nothing ever sets: blocks the calling (daemon) thread forever


def test_a_hung_tracer_does_not_delay_shutdown_past_the_bound_and_the_driver_still_closes(monkeypatch, boot):
    use_settings(monkeypatch)
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: HungTracer())
    monkeypatch.setattr(main, "TRACER_SHUTDOWN_TIMEOUT_S", 0.2)
    started = time.monotonic()
    with TestClient(main.create_app()):
        pass
    elapsed = time.monotonic() - started
    assert elapsed < 2.0                                    # bounded well under the hang, not the real (infinite) wait
    assert boot.order[-1] == "driver.close"                 # closed even though the tracer's shutdown thread never finished


# ---------------------------------------------------------------- M4: background services start only when their flag is on, stop first

def test_with_both_m4_flags_off_no_monitor_runs_uploads_are_not_ready_but_the_ttl_sweeper_still_runs(monkeypatch, boot):
    """docs/v2/M4_PLAN.md 15.4: the TTL sweeper runs whenever a driver exists, so the soft rollback (UPLOADS_ENABLED=false)
    still deletes existing workspaces on schedule; uploads themselves stay unavailable."""
    use_settings(monkeypatch)
    with TestClient(main.create_app()) as client:
        st = client.app.state
        assert st.freshness_monitor is None and st.upload_sweeper is not None and st.uploads_ready is False
        assert isinstance(st.upload_slots, type(threading.BoundedSemaphore(1)))
        # counted on the drain while it runs
        assert isinstance(st.upload_slots, workspace_routes.DrainCountedSlot)
        assert st.upload_slots.acquire(blocking=False) and not st.upload_slots.acquire(blocking=False)   # exactly one
        assert drain.DRAIN.active == 1
        st.upload_slots.release()
        assert drain.DRAIN.active == 0
        assert st.workspace_create_limiter.max_events == 3 and st.workspace_create_limiter.window == 86400
        assert st.upload_limiter.max_events == 10 and st.upload_limiter.window == 3600
    assert boot.order == ["bootstrap", "driver.close"]


def test_the_background_services_stop_before_the_driver_closes_even_when_the_tracer_fails(monkeypatch, boot):
    use_settings(monkeypatch)
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: FakeTracer(boot.order, fail=True))
    monkeypatch.setattr(main.monitor, "stop", lambda app: boot.order.append("monitor.stop"))
    monkeypatch.setattr(main.jobs, "stop", lambda app: boot.order.append("jobs.stop"))
    with TestClient(main.create_app()):
        pass
    assert boot.order == ["bootstrap", "monitor.stop", "jobs.stop", "tracer.shutdown", "driver.close"]


def test_a_failing_background_stop_never_keeps_the_driver_open(monkeypatch, boot):
    use_settings(monkeypatch)

    def boom(app):
        raise RuntimeError("stop failed")
    monkeypatch.setattr(main.monitor, "stop", boom)
    with TestClient(main.create_app()):
        pass
    assert boot.order[-1] == "driver.close"


# ---------------------------------------------------------------- M5a I4: the paid-ask state

def test_the_state_is_built_rebuilt_and_the_maintenance_thread_started_before_serving_and_stopped_in_order(
        monkeypatch, boot_in_order):
    use_settings(monkeypatch)
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: FakeTracer(boot_in_order.order))
    monkeypatch.setattr(main.monitor, "stop", lambda app: boot_in_order.order.append("monitor.stop"))
    monkeypatch.setattr(main.jobs, "stop", lambda app: boot_in_order.order.append("jobs.stop"))
    with TestClient(main.create_app()) as client:
        assert boot_in_order.order == ["bootstrap", "state.build", "state.rebuild", "maintenance.start"]
        assert client.app.state.maintenance.boot is boot_in_order
    assert boot_in_order.order[4:] == ["maintenance.stop", "monitor.stop", "jobs.stop", "tracer.shutdown",
                                       "state_driver.close", "driver.close"]


def test_the_lifespan_puts_the_state_on_the_app_with_its_estimates_cache_budget_and_bounded_store_driver(
        monkeypatch, boot):
    settings = use_settings(monkeypatch, cache_read_budget_per_s=7, state_op_timeout_s=0.8)
    with TestClient(main.create_app()) as client:
        st = client.app.state
        assert st.state is boot.backend and st.state_driver is boot.state_driver
        assert isinstance(st.state_store_driver, BoundedDriver) and st.state_store_driver._driver is boot.state_driver
        assert st.state_store_driver._timeout_s == 0.8
        assert st.cache_budget.rate == 7
        assert set(st.estimates) == {"hybrid", "vector", "agent", "workspace"}
        assert all(type(v) is int and v > 0 for v in st.estimates.values())
        assert st.estimates == {k: v["micro"] for k, v in main.estimate.boot_estimates(settings).items()}


def test_the_paid_windows_are_seeded_from_the_rebuilt_ledger_so_a_restart_gives_nobody_a_fresh_one(monkeypatch, boot):
    use_settings(monkeypatch, rate_limit_questions=5, rate_limit_window_seconds=600)
    now = time.time()
    boot.backend.report = a_report(now_wall=now, paid=4, ip_events=[
        ("addr-a", now - 30), ("addr-a", now - 20), ("addr-a", now - 10), ("addr-b", now - 5000), ("addr-c", now - 1)])
    with TestClient(main.create_app()) as client:
        window = client.app.state.rate_limiter
        assert [window.allow("addr-a") for _ in range(3)] == [True, True, False]     # three of five already used
        assert window.allow("addr-b") and window.allow("addr-c")                     # an old event does not count
        assert [window.allow("addr-c") for _ in range(4)] == [True, True, True, False]


def test_a_rebuild_that_fails_at_first_is_retried_and_the_service_boots(monkeypatch, boot, caplog):
    use_settings(monkeypatch)
    boot.backend.rebuild_errors = [StateUnavailable("the database is still starting")] * 2
    with caplog.at_level("WARNING", logger="semigraph.serve.main"):
        with TestClient(main.create_app()):
            pass
    assert boot.state_order.count("state.rebuild") == 3 and "maintenance.start" in boot.state_order
    assert sum("not readable yet" in r.getMessage() for r in caplog.records) == 2


def test_a_rebuild_that_never_succeeds_refuses_to_boot_and_closes_what_was_opened(monkeypatch, boot):
    """Serving with counters that did not come from the ledger would let the day's caps be spent twice: no boot, no
    maintenance thread, no paid ask. Both drivers are closed."""
    use_settings(monkeypatch)
    monkeypatch.setattr(main, "CONNECT_RETRY_S", 0.05)
    boot.backend.rebuild_errors = [StateUnavailable("down")] * 10_000
    with pytest.raises(RuntimeError, match="could not be read"):
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    assert "maintenance.start" not in boot.state_order and boot.state_order.count("state_driver.close") == 1
    assert boot.order == ["bootstrap", "driver.close"]


def test_a_maintenance_thread_that_cannot_start_refuses_to_boot_and_closes_both_drivers(monkeypatch, boot):
    use_settings(monkeypatch)
    FakeMaintenance.start_error = RuntimeError("cannot start a thread")
    with pytest.raises(RuntimeError, match="cannot start"):
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    assert boot.state_order[-1] == "state_driver.close" and boot.order == ["bootstrap", "driver.close"]


@pytest.mark.parametrize("failing", ["monitor", "jobs"])
def test_a_boot_that_fails_after_the_state_started_stops_what_it_started_and_closes_both_drivers(
        monkeypatch, boot_in_order, failing):
    """The maintenance thread and the state driver are open by then: a startup that raises later (a background service
    that cannot start) must stop the thread, stop what did start, flush the tracer and close both drivers, in the order
    of the normal shutdown, before the error reaches the caller."""
    use_settings(monkeypatch)
    order = boot_in_order.order
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: FakeTracer(order))
    monkeypatch.setattr(main.monitor, "stop", lambda app: order.append("monitor.stop"))
    monkeypatch.setattr(main.jobs, "stop", lambda app: order.append("jobs.stop"))

    def cannot_start(app):
        raise RuntimeError("cannot start the background service")

    if failing == "monitor":
        monkeypatch.setattr(main.monitor, "start_if_enabled", cannot_start)
    else:
        monkeypatch.setattr(main.monitor, "start_if_enabled", lambda app: order.append("monitor.start"))
        monkeypatch.setattr(main.jobs, "start_if_enabled", cannot_start)
    with pytest.raises(RuntimeError, match="cannot start the background service"):
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    started = ["bootstrap", "state.build", "state.rebuild", "maintenance.start"] + (
        ["monitor.start"] if failing == "jobs" else [])
    assert order == [*started, "maintenance.stop", "monitor.stop", "jobs.stop", "tracer.shutdown", "state_driver.close",
                     "driver.close"]


def test_a_boot_that_fails_before_the_state_is_built_still_flushes_the_tracer_and_closes_the_driver(monkeypatch, boot):
    use_settings(monkeypatch)
    monkeypatch.setattr(main.tracing, "get_tracer", lambda s: FakeTracer(boot.order))

    def broken(settings):
        raise ValueError("cannot build the limiters")

    monkeypatch.setattr(main, "make_limiters", broken)
    with pytest.raises(ValueError, match="limiters"):
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    assert boot.order == ["bootstrap", "tracer.shutdown", "driver.close"] and boot.state_order == []


def test_a_failing_stop_while_a_failed_boot_unwinds_never_keeps_the_drivers_open(monkeypatch, boot):
    use_settings(monkeypatch)

    def boom(app):
        raise RuntimeError("stop failed")

    def first_error(app):
        raise ValueError("the first error")

    def join_failed(self, timeout=5.0):
        raise RuntimeError("join failed")

    monkeypatch.setattr(main.monitor, "start_if_enabled", first_error)
    monkeypatch.setattr(main.jobs, "stop", boom)
    monkeypatch.setattr(FakeMaintenance, "stop", join_failed)
    with pytest.raises(ValueError, match="the first error"):                  # the boot's own error, not the cleanup's
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    assert boot.state_order[-1] == "state_driver.close" and boot.order[-1] == "driver.close"


def test_an_estimate_that_cannot_be_computed_refuses_to_boot_before_the_state_is_built(monkeypatch, boot):
    use_settings(monkeypatch)

    def broken(settings):
        raise ValueError("a configured price must be finite")

    monkeypatch.setattr(main.estimate, "boot_estimates", broken)
    with pytest.raises(ValueError, match="finite"):
        with TestClient(main.create_app()):
            pytest.fail("the service must not start")
    assert boot.state_order == [] and boot.order == ["bootstrap", "driver.close"]


def test_the_lifespan_waits_for_a_stream_still_counted_before_it_stops_the_maintenance_thread_and_closes_the_drivers(
        monkeypatch, boot_in_order):
    use_settings(monkeypatch)
    with TestClient(main.create_app()):
        drain.DRAIN.enter()                                  # a paid stream that is still finishing its ledger writes

        def finishes_late():
            time.sleep(0.3)
            boot_in_order.order.append("stream.finished")
            drain.DRAIN.leave()

        threading.Thread(target=finishes_late).start()
    order = boot_in_order.order
    assert order.index("stream.finished") < order.index("maintenance.stop") < order.index("state_driver.close")
    assert order.index("stream.finished") < order.index("driver.close")


def test_a_stream_that_never_finishes_is_logged_and_does_not_hold_shutdown_past_the_bound(monkeypatch, boot, caplog):
    use_settings(monkeypatch)
    monkeypatch.setattr(drain, "LIFESPAN_IDLE_WAIT_S", 0.2)
    with caplog.at_level("ERROR", logger="semigraph.serve.main"):
        started = time.monotonic()
        with TestClient(main.create_app()):
            drain.DRAIN.enter()
        assert time.monotonic() - started < 3.0
    drain.DRAIN.leave()
    assert boot.order[-1] == "driver.close" and "state_driver.close" in boot.state_order
    assert any("still active at shutdown" in r.getMessage() for r in caplog.records)


def test_a_failing_maintenance_stop_never_keeps_the_drivers_open(monkeypatch, boot):
    use_settings(monkeypatch)

    def boom(self, timeout=5.0):
        raise RuntimeError("join failed")

    monkeypatch.setattr(FakeMaintenance, "stop", boom)
    with TestClient(main.create_app()):
        pass
    assert boot.state_order[-1] == "state_driver.close" and boot.order[-1] == "driver.close"


def test_build_state_makes_the_bounded_state_driver_and_the_backend_over_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "make_state_driver", lambda settings: seen.setdefault("driver", object()))
    monkeypatch.setattr(main, "make_backend", lambda settings, drivers: seen.update(drivers=drivers) or "backend")
    settings = Settings(_env_file=None)
    driver, backend = main.build_state(settings)
    assert driver is seen["driver"] and backend == "backend" and seen["drivers"].state is driver


def test_rebuild_state_returns_the_report_and_retries_only_an_unavailable_store(monkeypatch):
    monkeypatch.setattr(main, "REBUILD_RETRY_SLEEP_S", 0)
    backend = FakeBackend(types.SimpleNamespace(note_state=lambda e: None), "the report")
    assert main.rebuild_state(backend) == "the report"
    backend.rebuild_errors = [ValueError("a bug, not an outage")]
    with pytest.raises(ValueError, match="a bug"):
        main.rebuild_state(backend)


M4_PATHS = {
    "/api/freshness": {"get"}, "/api/admin/freshness/check": {"post"},
    "/api/company/{ticker}/dossier": {"get"}, "/api/company/{ticker}/risk-changes": {"get"},
    "/api/workspace": {"post"}, "/api/workspace/{ws}": {"get", "delete"}, "/api/workspace/{ws}/documents": {"post"},
    "/api/workspace/{ws}/jobs/{job_id}": {"get"}, "/api/workspace/{ws}/changes": {"get"},
    "/api/workspace/{ws}/evidence/{doc_id}": {"get"},
    "/api/admin/state": {"get"}, "/api/admin/policy": {"get", "post"},        # M5a I4
}


def test_the_m4_routers_are_mounted():
    """Checked through the app's own route table (OpenAPI paths): FastAPI 0.141 wraps an included router instead of
    copying its routes into ``app.router.routes``, so an identity check on those objects would pass vacuously."""
    paths = main.create_app().openapi()["paths"]
    for path, methods in M4_PATHS.items():
        assert path in paths, path
        assert methods <= set(paths[path]), (path, set(paths[path]))


# ---------------------------------------------------------------- review of M4 build: no raw workspace id in the access log

@pytest.mark.parametrize("path,expected", [
    ("/api/workspace/0123456789abcdef0123456789abcdef/documents",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/documents"),
    ("/api/workspace/0123456789abcdef0123456789abcdef/changes?document_id=0123456789ab&from=1&to=2",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/changes"),
    ("/api/workspace/0123456789abcdef0123456789abcdef/evidence/doc:0123456789ab:v1:0001",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/evidence/<doc>"),
    ("/api/stats", "/api/stats"),
    ("/api/evidence/doc:0123456789ab:v1:0001", "/api/evidence/<doc>"),
    # second Opus review, finding 9 partial: uvicorn logs the PERCENT-QUOTED path, and a query may follow the id directly
    ("/api/workspace/0123456789abcdef0123456789abcdef/evidence/doc%3A0123456789ab%3Av1%3A0003",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/evidence/<doc>"),
    ("/api/workspace/0123456789abcdef0123456789abcdef?t=1",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">"),
    ("/api/evidence/doc%3A0123456789ab%3Av1%3A0001", "/api/evidence/<doc>"),
    ("/api/company/NVDA/risk-changes?limit=20", "/api/company/NVDA/risk-changes?limit=20"),
    ("/api/x?id=doc%3A0123456789ab%3Av1%3A0001", "/api/x"),
    # closing verification (log injection): decoding is for MATCHING only; the logged path is re-escaped, so a client
    # can never write a newline, an escape sequence or a quote into the access log
    ("/api/stats%0Afake%20log%20line", "/api/stats%0Afake%20log%20line"),
    ("/api/stats%1B%5B31mred", "/api/stats%1B%5B31mred"),
    ('/api/stats%22injected', "/api/stats%22injected"),
    ("/api/workspace/0123456789abcdef0123456789abcdef/x%0Ay",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/x%0Ay"),
    # closing verification LOWs: the workspace-id SEGMENT is always hashed, whatever its case or length (an uppercased
    # or over-long id still carries the real id), and a kept query string is re-escaped like the path
    ("/api/workspace/0123456789ABCDEF0123456789ABCDEF/documents",
     "/api/workspace/<ws:" + main.ws_hash("0123456789ABCDEF0123456789ABCDEF") + ">/documents"),
    ("/api/workspace/0123456789abcdef0123456789abcdef0/jobs/j1",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef0") + ">/jobs/j1"),
    ("/api/workspace", "/api/workspace"),
    # round-6 verification L4: malformed spellings of a workspace path (each a 404) still never log the raw id
    ("/api/workspace//0123456789abcdef0123456789abcdef/documents",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/documents"),
    ("/api//workspace/0123456789abcdef0123456789abcdef",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">"),
    ("/api/workspace/%2F0123456789abcdef0123456789abcdef",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">"),
    ("/API/Workspace/0123456789abcdef0123456789abcdef/jobs/j1",
     "/api/workspace/<ws:" + main.ws_hash("0123456789abcdef0123456789abcdef") + ">/jobs/j1"),
    ("/api/company/NVDA/risk-changes?limit=20\nfake", "/api/company/NVDA/risk-changes?limit=20%0Afake"),
    ("/api/company/NVDA/risk-changes?limit=20&q=%0A", "/api/company/NVDA/risk-changes?limit=20&q=%0A"),
    ('/api/stats?x="\x1b[31m', "/api/stats?x=%22%1B%5B31m"),
])
def test_the_access_log_redacts_workspace_ids_doc_ids_and_workspace_query_strings(path, expected):
    assert main.redact_access_path(path) == expected


def test_the_access_log_filter_rewrites_uvicorns_record_in_place():
    import logging

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/api/workspace/" + "a" * 32 + "/jobs/j1", "1.1", 200), None)
    assert main.WorkspaceAccessLogFilter().filter(record) is True
    assert "a" * 32 not in record.getMessage() and "<ws:" in record.getMessage()
    assert any(isinstance(f, main.WorkspaceAccessLogFilter) for f in logging.getLogger("uvicorn.access").filters)


def test_the_process_is_hardened_before_anything_else_boots(monkeypatch, boot):
    """docs/v2/M4_PLAN.md 5 (second Opus review S1): the non-dumpable call precedes bootstrap, threads and subprocesses."""
    use_settings(monkeypatch)
    monkeypatch.setattr(main.hardening, "make_process_non_dumpable", lambda: boot.order.append("harden") or True)
    with TestClient(main.create_app()):
        pass
    assert boot.order[:2] == ["harden", "bootstrap"]


# ---------------------------------------------------------------- sse-starlette logs every event at DEBUG: pinned off

def test_importing_the_service_pins_the_sse_starlette_logger_at_info():
    """sse-starlette logs each SSE event, which carries the question or a workspace answer, at DEBUG. Production logs at
    INFO today, so nothing is written; the pin keeps it that way when someone turns the root logger to DEBUG."""
    import logging

    assert logging.getLogger("sse_starlette").level == logging.INFO
    assert not logging.getLogger("sse_starlette.sse").isEnabledFor(logging.DEBUG)      # the child that actually logs
