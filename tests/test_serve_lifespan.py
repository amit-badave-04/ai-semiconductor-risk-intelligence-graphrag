"""serve/main.py lifespan: the agent import at boot (fail fast), the async answer path's runtime and the tracer's
lifecycle.

Neo4j and the embedder are never touched: ``bootstrap`` is replaced by a stub that records that it ran. The agent package is a
stub too: a temporary ``semigraph.agent`` whose ``stream_async`` module (the module the ask route serves agent questions
with) raises the way a missing langgraph would."""

import sys
import threading
import time
import types

import anyio
import pytest
from fastapi.testclient import TestClient

from semigraph.config import Settings
from semigraph.serve import main, routes

AGENT_STREAM = "semigraph.agent.stream_async"


class Boot:
    """The stand-ins for bootstrap and the driver; ``order`` records what happened in which order."""

    def __init__(self):
        self.order: list[str] = []
        self.driver = types.SimpleNamespace(close=lambda: self.order.append("driver.close"))

    def bootstrap(self, settings):
        self.order.append("bootstrap")
        return self.driver, types.SimpleNamespace(name="fake-embedder"), {"nodes": {}}, None, frozenset()


@pytest.fixture
def boot(monkeypatch):
    b = Boot()
    monkeypatch.setattr(main, "bootstrap", b.bootstrap)
    return b


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


def test_the_lifespan_builds_the_in_flight_cap_of_paid_answers_as_a_limiter_on_the_app_loop(monkeypatch, boot):
    """M5a I2: ``answer_limiter`` (an ``anyio.CapacityLimiter``, taken without waiting by ``PaidStream``) replaces the
    ``answer_slots`` thread semaphore. It is built inside the lifespan's loop, so ``client.portal`` can use it."""
    use_settings(monkeypatch, max_concurrent_answers=1)
    with TestClient(main.create_app()) as client:
        st = client.app.state
        assert isinstance(st.answer_limiter, anyio.CapacityLimiter) and st.answer_limiter.total_tokens == 1
        assert not hasattr(st, "answer_slots")
        holder = object()
        client.portal.call(st.answer_limiter.acquire_on_behalf_of_nowait, holder)
        try:
            with pytest.raises(anyio.WouldBlock):                          # exactly one answer in flight
                client.portal.call(st.answer_limiter.acquire_on_behalf_of_nowait, object())
        finally:
            client.portal.call(st.answer_limiter.release_on_behalf_of, holder)
        assert st.answer_limiter.borrowed_tokens == 0


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
        assert st.upload_slots.acquire(blocking=False) and not st.upload_slots.acquire(blocking=False)   # exactly one
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


M4_PATHS = {
    "/api/freshness": {"get"}, "/api/admin/freshness/check": {"post"},
    "/api/company/{ticker}/dossier": {"get"}, "/api/company/{ticker}/risk-changes": {"get"},
    "/api/workspace": {"post"}, "/api/workspace/{ws}": {"get", "delete"}, "/api/workspace/{ws}/documents": {"post"},
    "/api/workspace/{ws}/jobs/{job_id}": {"get"}, "/api/workspace/{ws}/changes": {"get"},
    "/api/workspace/{ws}/evidence/{doc_id}": {"get"},
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
