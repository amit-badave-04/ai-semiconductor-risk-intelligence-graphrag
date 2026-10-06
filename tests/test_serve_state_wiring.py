"""The paid-ask state wired into the HTTP layer (M5a I4, docs/v2/M5_DECISIONS.md 2.2, docs/v2/M5A_BUILD_PLAN.md step
0-I4).

``tests/test_serve_api.py`` keeps proving what each gate does for an ordinary ask; this file pins the new seams:

* every refusal ``state.reserve`` can answer, each with its status and message; the kill levels; a kill level or an
  answer cache that cannot be read fails CLOSED (no paid call, no reserve);
* the cache read budget, the workspace token read under the same bound, the cached hit's row;
* a lease that was granted and then lost before the stream took it is settled as abandoned (an exception, a cancelled
  request), and every path gives the drain count back exactly once;
* the drain: a paid ask, a workspace ask, a workspace creation and an upload are refused with 503 while cached answers
  and every read keep being served, and a refused request consumes no window;
* the kill level is set through the backend (immediate on this machine, a relaxation that cannot be stored is not
  applied), ``/api/admin/state`` is admin-only, the production validators refuse an open or raised deployment.

No database and no network: the state backend is ``tests/serve_state_fakes.FakeStateBackend`` (or the real
``InProcessBackend`` over an in-memory ledger where the real logic is the point).
"""

import json
import time
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from serve_state_fakes import (
    FakeStateBackend,
    InMemoryLedger,
    RouteSettings,
    build_route_app,
    fresh_drain,  # noqa: F401 - a fixture
)

from semigraph.config import Settings
from semigraph.serve import drain, guard, routes, store, workspace_routes
from semigraph.serve.state import Denied, Lease, StateDrivers, StateUnavailable, make_backend

pytestmark = pytest.mark.usefixtures("fresh_drain")

Q = "Which HBM suppliers does Nvidia depend on, and which export rules apply?"
CID = "0001045810-26-000021:I.1:0320"
WS = "c" * 32
TOKEN = "good-token"
DONE = {"event": "done", "answer": f"Nvidia depends on HBM suppliers [{CID}].", "citations": [CID], "hallucinated": [],
        "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.00007}


async def fake_twin(question, driver, embedder, strategy="hybrid", **kw):
    yield {"event": "retrieval", "anchors": {}, "counts": {}}
    yield {"event": "delta", "text": "Nvidia depends on "}
    yield {**DONE, "strategy": strategy, "question": question}


def must_not_run(*a, **kw):
    pytest.fail("a refused ask must never reach the answer writer")


def real_backend_settings(**changes) -> SimpleNamespace:
    """The settings the REAL backends read (a plain object: ``serve.state`` imports no ``config``); a key set to
    ``None`` is left off, as a double that predates the pepper version."""
    values = dict(state_backend="inprocess", max_queries_per_day=150, max_spend_usd_per_day=10.0,
                  paid_per_ip_per_day=20, max_concurrent_answers=2, kill_switch=False, kill_switch_refresh_s=10,
                  kill_switch_stale_s=30, state_op_timeout_s=1.0, lease_ttl_s=60, lease_renew_s=15, machine_id="m1",
                  ip_hash_version=2)
    values.update(changes)
    return SimpleNamespace(**{key: value for key, value in values.items() if value is not None})


@pytest.fixture
def cached_rows(monkeypatch):
    """The cached-answer rows ``store.log_query`` still writes."""
    rows: list[dict] = []
    monkeypatch.setattr(store, "log_query", lambda driver, **kw: rows.append(kw))
    return rows


@pytest.fixture(autouse=True)
def store_fakes(monkeypatch):
    """The store functions the routes (and the real backend) still call, with no database behind them."""
    monkeypatch.setattr(store, "ledger_summary", lambda driver: {"today": {"paid": 0}})
    monkeypatch.setattr(store, "get_policy", lambda driver, key: None)
    monkeypatch.setattr(store, "get_answer", lambda driver, key, ttl: None)
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: None)


@pytest.fixture
def backend():
    return FakeStateBackend()


@pytest.fixture
def make_client(backend, cached_rows, monkeypatch):
    """``make_client(**app_options)`` -> a started ``TestClient`` (closed with the test)."""
    monkeypatch.setattr(routes, "aanswer_stream", fake_twin)
    monkeypatch.setattr(routes, "astream_workspace_answer", fake_twin)
    opened: list[TestClient] = []

    def make(**options):
        options.setdefault("backend", backend)
        client = TestClient(build_route_app(**options))
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in reversed(opened):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def ask(client, question=Q, **body):
    return client.post("/api/ask", json={"question": question, **body})


def events_of(response) -> list[dict]:
    out = []
    for block in response.text.split("\n\n"):
        data = "".join(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def assert_json_refusal(response, status: int, detail: str) -> None:
    assert response.status_code == status and response.json() == {"detail": detail}, response.text
    assert "event:" not in response.text


# ---------------------------------------------------------------- every refusal of the reserve, with its message

@pytest.mark.parametrize("denial,status,detail", [
    (Denied.DAILY_COUNT, 429, routes.MSG_BUDGET),
    (Denied.DAILY_SPEND, 429, routes.MSG_BUDGET),
    (Denied.IP_DAILY, 429, routes.MSG_IP_BUDGET),
    (Denied.INFLIGHT, 429, routes.MSG_BUSY),
    (Denied.KILL, 503, routes.MSG_PAUSED),
    (Denied.UNAVAILABLE, 503, routes.MSG_PAUSED),
], ids=lambda value: getattr(value, "name", str(value)))
def test_every_refusal_of_the_reserve_is_its_own_status_and_message_before_any_stream(client, backend, denial, status,
                                                                                      detail, fresh_drain):
    backend.deny.append(denial)
    assert_json_refusal(ask(client), status, detail)
    # nothing taken, nothing counted
    assert backend.granted == [] and backend.settled == [] and fresh_drain.active == 0
    assert "mark_started" not in backend.names()


def test_the_messages_are_the_plain_texts_the_page_shows():
    assert routes.MSG_IP_BUDGET == ("You have used today's live questions for your address — the example questions "
                                    "still work, or come back tomorrow.")
    assert routes.MSG_RETRIEVAL_ONLY == ("Live questions are limited to cached answers right now — the example "
                                         "questions still work.")
    assert routes.MSG_DRAINING == "The service is restarting — please try again in a minute."
    assert len({routes.MSG_BUDGET, routes.MSG_IP_BUDGET, routes.MSG_BUSY, routes.MSG_PAUSED, routes.MSG_RETRIEVAL_ONLY,
                routes.MSG_DRAINING}) == 6


def test_a_reserve_that_raises_is_503_paused_and_counts_nothing(client, backend, fresh_drain):
    backend.errors["reserve"] = StateUnavailable("the database is slow")
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)
    assert backend.granted == [] and fresh_drain.active == 0


@pytest.mark.parametrize("strategy,ask_type", [("hybrid", "hybrid"), ("vector", "vector")])
def test_the_reserve_carries_the_estimate_of_the_ask_type(client, backend, strategy, ask_type):
    assert events_of(ask(client, strategy=strategy))[-1]["event"] == "done"
    assert backend.reserve_kwargs[-1]["estimate_micro"] == client.app.state.estimates[ask_type]
    assert backend.reserve_kwargs[-1]["strategy"] == strategy and backend.reserve_kwargs[-1]["workspace"] is False


def test_a_workspace_ask_reserves_the_workspace_estimate(make_client, backend, monkeypatch):
    client = make_client(settings=RouteSettings(), estimates={"hybrid": 1, "vector": 2, "agent": 3, "workspace": 4})
    client.app.state.settings.uploads_enabled, client.app.state.uploads_ready = True, True
    monkeypatch.setattr("semigraph.uploads.repo.authenticate", lambda driver, ws, token: token == TOKEN)
    r = client.post("/api/ask", json={"question": Q, "workspace_id": WS}, headers={"X-Workspace-Token": TOKEN})
    assert events_of(r)[-1]["event"] == "done"
    assert backend.reserve_kwargs[-1]["estimate_micro"] == 4 and backend.reserve_kwargs[-1]["workspace"] is True


# ---------------------------------------------------------------- the kill levels

def test_the_kill_level_on_is_503_paused_and_reserves_nothing(client, backend):
    backend.kill = "on"
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)
    assert backend.names().count("reserve") == 0


def test_the_kill_level_retrieval_only_is_503_with_its_own_message_and_reserves_nothing(client, backend):
    backend.kill = "retrieval_only"
    assert_json_refusal(ask(client), 503, routes.MSG_RETRIEVAL_ONLY)
    assert backend.names().count("reserve") == 0


def test_the_kill_level_off_serves(client, backend):
    backend.kill = "off"
    assert events_of(ask(client))[-1]["event"] == "done"


@pytest.mark.parametrize("level", ["on", "retrieval_only"])
def test_cached_answers_are_served_at_every_kill_level(client, backend, level):
    assert events_of(ask(client))[-1]["event"] == "done"            # paid once: cached
    backend.kill = level
    assert events_of(ask(client))[0]["cached"] is True


@pytest.mark.parametrize("broken", ["raises", "garbage"])
def test_a_kill_level_that_cannot_be_read_fails_closed(client, backend, broken):
    """Unread, stale, a failing backend and a value nobody defined all mean: paid asks are off."""
    if broken == "raises":
        backend.errors["kill_level"] = RuntimeError("the backend is gone")
    else:
        backend.kill = "maybe"
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)
    assert backend.names().count("reserve") == 0


def test_the_real_backend_refuses_paid_asks_until_its_kill_level_has_been_read_and_never_reserves(
        make_client, monkeypatch, cached_rows):
    """The real ``InProcessBackend`` over an in-memory ledger: its level reads ``on`` until the maintenance thread has
    read it (``refresh_kill_level``), so a route that wires it first refuses every paid ask."""
    ledger = InMemoryLedger()
    real = make_backend(real_backend_settings(), StateDrivers(state=object()), ledger=ledger)
    client = make_client(backend=real)
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)
    assert not ledger.rows
    real.refresh_kill_level()
    assert events_of(ask(client))[-1]["event"] == "done"
    assert [r["outcome"] for r in ledger.rows.values()] == ["done"] and real.snapshot()["inflight"] == 0


def test_a_kill_level_the_maintenance_thread_stopped_refreshing_goes_stale_and_paid_asks_stop(make_client):
    """The real backend with a clock the test moves: a level read 31 s ago (``kill_switch_stale_s`` is 30) reads as
    ``on``, so the route refuses, whatever the database last said; a refresh makes it serve again."""
    now = [1_000.0]
    ledger = InMemoryLedger()
    real = make_backend(real_backend_settings(), StateDrivers(state=object()), ledger=ledger, clock=lambda: now[0])
    real.refresh_kill_level()
    client = make_client(backend=real)
    assert events_of(ask(client, Q + " fresh"))[-1]["event"] == "done"
    now[0] += 31
    assert_json_refusal(ask(client, Q + " stale"), 503, routes.MSG_PAUSED)
    assert len(ledger.rows) == 1                                           # the stale ask reserved nothing
    real.refresh_kill_level()
    assert events_of(ask(client, Q + " refreshed"))[-1]["event"] == "done"


# ---------------------------------------------------------------- the answer cache

def test_a_cache_that_cannot_be_read_is_503_paused_and_never_falls_through_to_a_paid_call(client, backend, monkeypatch):
    monkeypatch.setattr(routes, "aanswer_stream", must_not_run)
    backend.errors["cache_get"] = StateUnavailable("the database is down")
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)
    assert backend.names().count("reserve") == 0 and backend.names().count("kill_level") == 0


def test_a_cached_hit_is_logged_as_a_cached_row_through_the_bounded_state_driver_under_the_state_limiter(
        client, backend, monkeypatch):
    seen = []

    def log_query(driver, **kw):
        seen.append((driver, kw, client.app.state.limiters.state.borrowed_tokens))

    monkeypatch.setattr(store, "log_query", log_query)
    ask(client)
    assert events_of(ask(client))[0]["cached"] is True
    (driver, kw, tokens), = seen
    assert driver is client.app.state.state_store_driver and tokens == 1
    assert kw["cached"] is True and kw["strategy"] == "hybrid" and "ip_hash" in kw


def test_a_cached_row_that_cannot_be_written_is_503_and_the_answer_is_not_served(client, backend, monkeypatch):
    ask(client)
    monkeypatch.setattr(store, "log_query", lambda driver, **kw: (_ for _ in ()).throw(StateUnavailable("down")))
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)


def test_the_cache_read_budget_is_one_bucket_for_the_process_and_an_empty_one_is_429_before_any_read(
        make_client, backend):
    client = make_client(cache_rate=2)
    assert ask(client, Q + " one").status_code == 200 and ask(client, Q + " two").status_code == 200
    reads_before = backend.names().count("cache_get")
    assert_json_refusal(ask(client, Q + " three"), 429, routes.MSG_READ_RATE)
    assert backend.names().count("cache_get") == reads_before and backend.names().count("reserve") == 2


def test_the_token_bucket_refills_with_time_and_holds_one_seconds_worth():
    now = [100.0]
    bucket = guard.TokenBucket(2, clock=lambda: now[0])
    assert [bucket.take() for _ in range(3)] == [True, True, False]
    now[0] += 0.5
    assert [bucket.take() for _ in range(2)] == [True, False]       # half a second: one token
    now[0] += 60
    assert [bucket.take() for _ in range(3)] == [True, True, False]  # never more than one second's worth
    assert guard.TokenBucket(0).take() and guard.TokenBucket(-1).take()     # a rate of 0 or less disables it


# ---------------------------------------------------------------- the workspace token read

@pytest.fixture
def workspace_client(make_client, monkeypatch):
    seen = []

    def authenticate(driver, ws, token):
        seen.append((driver, ws, token, drain.DRAIN.active))
        if token == "boom":
            raise StateUnavailable("the database is slow")
        return ws == WS and token == TOKEN

    monkeypatch.setattr("semigraph.uploads.repo.authenticate", authenticate)
    client = make_client(cache_rate=2)
    client.app.state.settings.uploads_enabled, client.app.state.uploads_ready = True, True
    client.seen = seen
    return client


def ws_ask(client, token):
    return client.post("/api/ask", json={"question": Q, "workspace_id": WS}, headers={"X-Workspace-Token": token})


def test_a_workspace_token_is_read_through_the_bounded_state_driver_and_a_bad_one_is_404(workspace_client, backend):
    assert ws_ask(workspace_client, "wrong").status_code == 404
    assert events_of(ws_ask(workspace_client, TOKEN))[-1]["event"] == "done"
    assert [driver for driver, *_ in workspace_client.seen] == [workspace_client.app.state.state_store_driver] * 2
    assert backend.names().count("reserve") == 1                                  # the refused one never reached a gate


def test_a_workspace_token_read_that_fails_is_503_paused_not_a_500_and_reserves_nothing(workspace_client, backend):
    assert_json_refusal(ws_ask(workspace_client, "boom"), 503, routes.MSG_PAUSED)
    assert backend.names().count("reserve") == 0


def test_the_workspace_token_read_takes_a_token_of_the_cache_read_budget(workspace_client, backend):
    assert ws_ask(workspace_client, "a").status_code == 404 and ws_ask(workspace_client, "b").status_code == 404
    reads = len(workspace_client.seen)
    assert_json_refusal(ws_ask(workspace_client, TOKEN), 429, routes.MSG_READ_RATE)
    # no graph read once the budget is empty
    assert len(workspace_client.seen) == reads


def test_the_workspace_token_is_checked_before_the_kill_level(workspace_client, backend):
    backend.kill = "on"
    assert ws_ask(workspace_client, "wrong").status_code == 404                     # a bad token never learns the level
    assert_json_refusal(ws_ask(workspace_client, TOKEN), 503, routes.MSG_PAUSED)
    assert backend.names().count("reserve") == 0


@pytest.mark.parametrize("denial,status,detail", [
    (Denied.DAILY_COUNT, 429, routes.MSG_BUDGET), (Denied.DAILY_SPEND, 429, routes.MSG_BUDGET),
    (Denied.IP_DAILY, 429, routes.MSG_IP_BUDGET), (Denied.INFLIGHT, 429, routes.MSG_BUSY),
], ids=lambda value: getattr(value, "name", str(value)))
def test_the_ceilings_apply_to_workspace_asks_too(workspace_client, backend, fresh_drain, denial, status, detail):
    backend.deny.append(denial)
    assert_json_refusal(ws_ask(workspace_client, TOKEN), status, detail)
    assert backend.reserve_kwargs[-1]["workspace"] is True and backend.granted == [] and fresh_drain.active == 0


def test_an_accepted_workspace_ask_streams_the_workspace_writer_with_its_arguments_and_never_the_cache(
        workspace_client, backend, monkeypatch):
    calls: list[dict] = []

    async def workspace_writer(question, driver, embedder, strategy="hybrid", **kw):
        calls.append({"strategy": strategy, **kw})
        async for event in fake_twin(question, driver, embedder, strategy, **kw):
            yield event

    monkeypatch.setattr(routes, "astream_workspace_answer", workspace_writer)
    monkeypatch.setattr(routes, "aanswer_stream", must_not_run)
    r = workspace_client.post("/api/ask", json={"question": Q, "workspace_id": WS, "as_of": "2026-09-01"},
                              headers={"X-Workspace-Token": TOKEN})
    assert events_of(r)[-1]["event"] == "done"
    assert calls[0]["workspace_id"] == WS and calls[0]["as_of"] == "2026-09-01" and calls[0]["strategy"] == "hybrid"
    assert [(s["workspace"], s["outcome"]) for s in backend.settled] == [(True, "done")]
    assert "cache_get" not in backend.names() and "cache_put" not in backend.names()


def test_a_public_ask_never_reaches_the_workspace_writer(client, backend, monkeypatch):
    monkeypatch.setattr(routes, "astream_workspace_answer", must_not_run)
    assert events_of(ask(client))[-1]["event"] == "done"
    assert [s["workspace"] for s in backend.settled] == [False]


# ---------------------------------------------------------------- a lease lost before the stream took it

def test_an_exception_between_the_reserve_and_the_stream_settles_the_lease_as_abandoned_and_counts_nothing(
        make_client, backend, monkeypatch, fresh_drain):
    def broken(*args, **kwargs):
        raise RuntimeError("cannot build the response")

    monkeypatch.setattr(routes, "PaidResponse", broken)
    client = make_client()
    with pytest.raises(RuntimeError, match="cannot build"):
        client.post("/api/ask", json={"question": Q})
    assert [(r["outcome"], r["usage"], r["cost_micro"]) for r in backend.settled] == [("abandoned", None, None)]
    assert backend.inflight == 0 and fresh_drain.active == 0


def test_a_request_cancelled_while_the_reserve_runs_still_settles_the_lease_it_was_granted(backend, fresh_drain):
    """The reserve runs on a thread that a cancellation cannot interrupt: the grant is recorded there, and the
    cancellation is delivered afterwards, so the lease must be settled by the handler that never receives it."""
    from semigraph.serve.limiters import make_limiters

    backend.delays["reserve"] = 0.25
    st = build_route_app(backend=backend).state

    async def main():
        st.limiters = make_limiters(RouteSettings())
        with anyio.move_on_after(0.05) as scope:                     # the client left while the reserve was running
            await routes._admit(st, "iph", "hybrid", False, 60_000)
        assert scope.cancelled_caught
        assert backend.inflight == 0 and fresh_drain.active == 0

    anyio.run(main)
    assert [r["outcome"] for r in backend.settled] == ["abandoned"] and len(backend.granted) == 1


def test_a_denied_or_failed_admission_gives_the_drain_count_back(backend, fresh_drain):
    from fastapi import HTTPException

    from semigraph.serve.limiters import make_limiters

    st = build_route_app(backend=backend).state

    async def main():
        st.limiters = make_limiters(RouteSettings())
        backend.deny.append(Denied.INFLIGHT)
        with pytest.raises(HTTPException) as refused:
            await routes._admit(st, "iph", "hybrid", False, 1)
        assert refused.value.status_code == 429 and fresh_drain.active == 0
        backend.errors["reserve"] = RuntimeError("unexpected")
        with pytest.raises(RuntimeError):
            await routes._admit(st, "iph", "hybrid", False, 1)
        assert fresh_drain.active == 0
        backend.errors.clear()
        lease = await routes._admit(st, "iph", "hybrid", False, 1)
        assert isinstance(lease, Lease) and fresh_drain.active == 1     # the count now belongs to the stream
        fresh_drain.leave()

    anyio.run(main)


# ---------------------------------------------------------------- the stream: order and the drain count

def test_the_lease_is_reconciled_before_the_cache_write_and_the_drain_count_is_one_while_streaming(
        make_client, backend, monkeypatch, fresh_drain):
    seen = {}

    async def watching_twin(question, driver, embedder, strategy="hybrid", **kw):
        seen["active_while_streaming"] = fresh_drain.active
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {**DONE, "strategy": strategy, "question": question}

    monkeypatch.setattr(routes, "aanswer_stream", watching_twin)
    client = make_client()
    assert events_of(ask(client))[-1]["event"] == "done"
    names = [n for n in backend.names() if n in ("reserve", "mark_started", "reconcile", "cache_put")]
    assert names == ["reserve", "mark_started", "reconcile", "cache_put"]
    assert seen["active_while_streaming"] == 1 and fresh_drain.active == 0


def test_the_reserve_cache_put_and_reconcile_are_state_hops_under_the_state_limiter(make_client, backend):
    holds: dict[str, int] = {}
    client = make_client()
    backend.on_call = lambda name: holds.setdefault(name, client.app.state.limiters.state.borrowed_tokens)
    ask(client)
    hops = ("cache_get", "kill_level", "reserve", "mark_started", "reconcile", "cache_put")
    assert {name: holds[name] for name in hops} == dict.fromkeys(hops, 1)


# ---------------------------------------------------------------- the drain

def test_a_workspace_ask_while_draining_is_503_before_it_takes_any_window_and_reserves_nothing(
        workspace_client, backend, fresh_drain):
    fresh_drain.begin()
    free = workspace_client.app.state.free_rate_limiter
    free.max_events = 1                                                # a single token: a consumed one would show
    for _ in range(3):
        refused = ws_ask(workspace_client, TOKEN)
        assert refused.status_code == 503 and refused.json()["detail"] == routes.MSG_DRAINING
        assert refused.headers["retry-after"] == "30"
    assert workspace_client.seen == [] and backend.calls == []         # not even the token read
    assert free.allow("anyone")                                        # the window is untouched


def test_a_public_ask_that_misses_the_cache_while_draining_is_503_and_takes_no_window(client, backend, fresh_drain):
    fresh_drain.begin()
    client.app.state.free_rate_limiter.max_events = 1
    for _ in range(3):
        assert_json_refusal(ask(client, Q + " miss"), 503, routes.MSG_DRAINING)
    assert client.app.state.free_rate_limiter.allow("anyone")
    assert backend.names().count("cache_get") == 3 and backend.names().count("reserve") == 0
    assert "kill_level" not in backend.names()


def test_a_cached_answer_is_still_served_while_draining_and_takes_the_free_window(client, backend, fresh_drain):
    assert events_of(ask(client))[-1]["event"] == "done"
    fresh_drain.begin()
    client.app.state.free_rate_limiter = guard.RateLimiter(1, 600)
    assert events_of(ask(client))[0]["cached"] is True
    # a hit consumes the free window, a refusal does not
    assert ask(client).status_code == 429
    assert backend.names().count("reserve") == 1


def test_a_drain_that_begins_after_the_early_check_still_refuses_before_the_reserve(client, backend, monkeypatch):
    """The ask is counted with ``try_enter`` right before the reserve: a drain that began after the earlier check
    refuses it there, and nothing is reserved."""
    class LateDrain:
        draining = False
        active = 0

        def try_enter(self):
            return False

    monkeypatch.setattr(drain, "DRAIN", LateDrain())
    assert_json_refusal(ask(client), 503, routes.MSG_DRAINING)
    assert backend.names().count("reserve") == 0


def test_every_read_is_served_while_draining(client, backend, fresh_drain, monkeypatch):
    monkeypatch.setattr(routes, "run_cypher", lambda driver, query, **params: [{"ok": 1, "chunk_id": CID}])
    fresh_drain.begin()
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/examples").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get(f"/api/evidence/{CID}").status_code == 200


@pytest.fixture
def upload_client(make_client, monkeypatch, fresh_drain):
    monkeypatch.setattr("semigraph.uploads.repo.authenticate", lambda driver, ws, token: token == TOKEN)
    monkeypatch.setattr("semigraph.uploads.repo.touch", lambda driver, ws: None)
    settings = RouteSettings()
    settings.uploads_enabled = True
    client = make_client(settings=settings, routers=[workspace_routes.router])
    st = client.app.state
    st.uploads_ready = True
    st.workspace_create_limiter = guard.RateLimiter(1, 86_400)
    st.upload_limiter = guard.RateLimiter(1, 3_600)
    st.upload_slots = workspace_routes.DrainCountedSlot(1)
    return client


def test_creating_a_workspace_and_uploading_are_503_while_draining_and_consume_no_window(upload_client, fresh_drain):
    st = upload_client.app.state
    fresh_drain.begin()
    for _ in range(3):
        created = upload_client.post("/api/workspace", json={})
        assert created.status_code == 503 and created.json()["detail"] == routes.MSG_DRAINING
        assert created.headers["cache-control"] == "no-store"
        uploaded = upload_client.post(f"/api/workspace/{WS}/documents", headers={"X-Workspace-Token": TOKEN},
                                      files={"file": ("a.pdf", b"%PDF-1.7 x", "application/pdf")})
        assert uploaded.status_code == 503 and uploaded.json()["detail"] == routes.MSG_DRAINING
    assert st.workspace_create_limiter.allow("k") and st.upload_limiter.allow("k") and st.read_rate_limiter.allow("k")
    assert st.upload_slots.acquire(blocking=False)                       # the slot was never taken by a refused upload
    st.upload_slots.release()


@pytest.mark.parametrize("level", ["on", "retrieval_only"])
def test_an_upload_is_refused_unless_the_kill_level_is_off(upload_client, backend, level):
    backend.kill = level
    r = upload_client.post(f"/api/workspace/{WS}/documents", headers={"X-Workspace-Token": TOKEN},
                           files={"file": ("a.pdf", b"%PDF-1.7 x", "application/pdf")})
    assert r.status_code == 503 and r.json()["detail"] == routes.MSG_UPLOADS_OFF


def test_an_upload_whose_kill_level_cannot_be_read_is_refused(upload_client, backend):
    backend.errors["kill_level"] = RuntimeError("gone")
    r = upload_client.post(f"/api/workspace/{WS}/documents", headers={"X-Workspace-Token": TOKEN},
                           files={"file": ("a.pdf", b"%PDF-1.7 x", "application/pdf")})
    assert r.status_code == 503 and r.json()["detail"] == routes.MSG_UPLOADS_OFF


def test_the_upload_slot_counts_a_running_upload_on_the_drain_from_acquire_to_release(fresh_drain):
    slot = workspace_routes.DrainCountedSlot(1)
    assert slot.acquire(blocking=False) and fresh_drain.active == 1
    assert not slot.acquire(blocking=False) and fresh_drain.active == 1     # a refused acquire counts nothing
    slot.release()
    assert fresh_drain.active == 0
    with pytest.raises(ValueError):
        slot.release()                                                     # an over-release changes nothing
    assert fresh_drain.active == 0


# ---------------------------------------------------------------- the admin surface

ADMIN = {"X-Admin-Token": "secret-token"}


def test_admin_state_needs_the_admin_token_and_reports_the_snapshot_the_limiters_and_the_drain(client, backend,
                                                                                               fresh_drain):
    assert client.get("/api/admin/state").status_code == 404
    assert client.get("/api/admin/state", headers={"X-Admin-Token": "wrong"}).status_code == 404
    fresh_drain.enter()
    body = client.get("/api/admin/state", headers=ADMIN).json()
    fresh_drain.leave()
    assert body["state"]["backend"] == "fake" and body["state"]["paid"] == 0
    assert body["limiters"]["state"] == {"borrowed": 0, "total": 4}
    assert set(body["limiters"]) == {"embed", "db", "health", "state"}
    assert body["drain"] == {"draining": False, "active": 1} and body["maintenance"] == {"alive": False}


def test_admin_state_is_503_when_the_state_store_cannot_answer(client, backend):
    backend.errors["snapshot"] = StateUnavailable("down")
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 503


@pytest.mark.parametrize("sent,level", [(True, "on"), (False, "off"), ("on", "on"), ("off", "off"),
                                        ("retrieval_only", "retrieval_only")])
def test_the_admin_sets_a_level_by_bool_or_name_through_the_backend(client, backend, sent, level):
    r = client.post("/api/admin/policy", json={"kill_switch": sent}, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"kill_switch": level, "stored": True}
    assert backend.kill_sets == [level] and backend.kill == level


def test_the_effective_level_the_admin_sees_is_on_when_the_backend_reports_something_nobody_defined(client, backend):
    backend.kill = "maybe"
    assert client.get("/api/admin/policy", headers=ADMIN).json()["effective"] == "on"
    backend.kill = "retrieval_only"
    assert client.get("/api/admin/policy", headers=ADMIN).json()["effective"] == "retrieval_only"


@pytest.mark.parametrize("bad", ["maybe", 3, None, ""])
def test_a_level_nobody_defined_is_refused_with_422_and_changes_nothing(client, backend, bad):
    assert client.post("/api/admin/policy", json={"kill_switch": bad}, headers=ADMIN).status_code == 422
    assert backend.kill_sets == []


def test_a_tightening_that_could_not_be_stored_still_holds_and_says_so(client, backend):
    backend.errors["set_kill_level"] = StateUnavailable("the database write failed")
    r = client.post("/api/admin/policy", json={"kill_switch": "on"}, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"kill_switch": "on", "stored": False}
    assert backend.kill == "on"


def test_a_relaxation_that_could_not_be_stored_is_not_applied_and_is_503(client, backend):
    backend.kill = "on"
    backend.errors["set_kill_level"] = StateUnavailable("the database write failed")
    r = client.post("/api/admin/policy", json={"kill_switch": "off"}, headers=ADMIN)
    assert r.status_code == 503 and backend.kill == "on"
    assert_json_refusal(ask(client), 503, routes.MSG_PAUSED)           # paid asks stay off


def test_a_flip_through_the_real_backend_is_immediate_without_waiting_for_a_refresh(make_client, monkeypatch):
    stored: dict[str, str] = {}
    monkeypatch.setattr(store, "get_policy", lambda driver, key: stored.get(key))
    monkeypatch.setattr(store, "set_policy", lambda driver, key, value: stored.__setitem__(key, value))
    real = make_backend(real_backend_settings(), StateDrivers(state=object()), ledger=InMemoryLedger())
    real.refresh_kill_level()
    client = make_client(backend=real)
    assert events_of(ask(client, Q + " one"))[-1]["event"] == "done"
    client.post("/api/admin/policy", json={"kill_switch": "retrieval_only"}, headers=ADMIN)
    assert stored["kill_switch"] == "retrieval_only"
    assert_json_refusal(ask(client, Q + " two"), 503, routes.MSG_RETRIEVAL_ONLY)       # no refresh in between
    assert client.get("/api/admin/policy", headers=ADMIN).json()["effective"] == "retrieval_only"


# ---------------------------------------------------------------- the production validators

PRODUCTION = {"environment": "production", "ip_hash_pepper": "p" * 32, "turnstile_required": True,
              "turnstile_secret_key": "turnstile-test-secret", "client_ip_header": "fly-client-ip"}   # gitleaks:allow


@pytest.fixture
def clean_environment(monkeypatch):
    for name in ("ENVIRONMENT", "IP_HASH_PEPPER", "TURNSTILE_REQUIRED", "TURNSTILE_SECRET_KEY", "CLIENT_IP_HEADER",
                 "MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_PER_IP_PER_DAY", "MAX_CONCURRENT_ANSWERS"):
        monkeypatch.delenv(name, raising=False)


def production(**changes) -> Settings:
    return Settings(_env_file=None, **{**PRODUCTION, **changes})


def test_a_correctly_configured_production_boots_with_the_approved_caps(clean_environment):
    s = production()
    assert (s.max_queries_per_day, s.max_spend_usd_per_day, s.paid_per_ip_per_day, s.max_concurrent_answers) == (
        150, 10.0, 20, 2)


@pytest.mark.parametrize("change,setting", [
    ({"turnstile_required": False}, "TURNSTILE_REQUIRED"),
    ({"turnstile_secret_key": ""}, "TURNSTILE_SECRET_KEY"),
    ({"client_ip_header": ""}, "CLIENT_IP_HEADER"),
    ({"client_ip_header": "x-forwarded-for"}, "CLIENT_IP_HEADER"),
    ({"max_queries_per_day": 151}, "MAX_QUERIES_PER_DAY"),
    ({"max_queries_per_day": 0}, "MAX_QUERIES_PER_DAY"),
    ({"max_spend_usd_per_day": 10.01}, "MAX_SPEND_USD_PER_DAY"),
    ({"max_spend_usd_per_day": 0}, "MAX_SPEND_USD_PER_DAY"),
    ({"paid_per_ip_per_day": 21}, "PAID_PER_IP_PER_DAY"),
    ({"paid_per_ip_per_day": 0}, "PAID_PER_IP_PER_DAY"),
    ({"max_concurrent_answers": 5}, "MAX_CONCURRENT_ANSWERS"),
    ({"max_concurrent_answers": 0}, "MAX_CONCURRENT_ANSWERS"),
], ids=lambda value: str(value))
def test_live_refuses_an_open_or_raised_deployment_and_names_the_setting(clean_environment, change, setting):
    with pytest.raises(ValidationError) as refused:
        production(**change)
    text = str(refused.value)
    assert setting in text and "input_value" not in text
    for secret in (PRODUCTION["turnstile_secret_key"], PRODUCTION["ip_hash_pepper"]):
        assert secret not in text


def test_every_problem_is_named_at_once_and_the_pepper_is_still_checked_first(clean_environment):
    with pytest.raises(ValidationError) as refused:
        production(turnstile_required=False, max_queries_per_day=500, client_ip_header="")
    text = str(refused.value)
    assert all(name in text for name in ("TURNSTILE_REQUIRED", "MAX_QUERIES_PER_DAY", "CLIENT_IP_HEADER"))
    with pytest.raises(ValidationError, match="IP_HASH_PEPPER"):
        production(ip_hash_pepper="short", turnstile_required=False)


def test_outside_production_the_caps_may_be_raised_zeroed_or_open(clean_environment):
    s = Settings(_env_file=None, environment="staging", max_queries_per_day=0, max_spend_usd_per_day=0,
                 paid_per_ip_per_day=0, max_concurrent_answers=40, turnstile_required=False, client_ip_header="")
    assert not s.is_production and s.max_concurrent_answers == 40


def test_the_new_settings_have_the_owner_approved_defaults_and_bounds(clean_environment):
    s = Settings(_env_file=None)
    assert (s.state_backend, s.max_spend_usd_per_day, s.paid_per_ip_per_day) == ("inprocess", 10.0, 20)
    assert (s.state_op_timeout_s, s.state_connection_acquisition_s) == (1.0, 0.5)
    assert (s.kill_switch_refresh_s, s.kill_switch_stale_s, s.cache_read_budget_per_s) == (10, 30, 10)
    assert (s.lease_ttl_s, s.lease_renew_s, s.drain_timeout_s) == (60, 15, 240)
    assert s.machine_id
    for bad in ({"state_backend": "valkey"}, {"state_op_timeout_s": 0}, {"drain_timeout_s": 290},
                {"cache_read_budget_per_s": 0}, {"kill_switch_stale_s": 5}, {"lease_renew_s": 60}, {"machine_id": ""}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


def test_the_machine_id_is_flys_when_set_else_the_host_name(clean_environment, monkeypatch):
    import socket

    monkeypatch.setenv("FLY_MACHINE_ID", "e286abcd1234")
    assert Settings(_env_file=None).machine_id == "e286abcd1234"
    monkeypatch.delenv("FLY_MACHINE_ID")
    assert Settings(_env_file=None).machine_id == socket.gethostname()


def test_the_drain_timeout_is_read_from_drain_timeout_s(clean_environment, monkeypatch):
    monkeypatch.setenv("DRAIN_TIMEOUT_S", "120")
    assert Settings(_env_file=None).drain_timeout_s == 120


# ---------------------------------------------------------------- the windows are seeded from the ledger, not reset

def test_a_window_seed_puts_earlier_events_back_and_never_replaces_a_live_one():
    window = guard.RateLimiter(3, 600)
    now = time.monotonic()
    window.seed("a", [now - 700, now - 30, now - 20, now - 10, now + 5])       # one too old, one in the future
    assert window.allow("a") is False                                           # the three that count fill it
    other = guard.RateLimiter(2, 600)
    other.seed("b", [now - 5, now - 4, now - 3])                                # only the newest two fit
    assert other.allow("b") is False
    other.seed("c", [])
    assert other.allow("c") is True
    live = guard.RateLimiter(2, 600)
    assert live.allow("d")
    live.seed("d", [now - 1, now - 2])                                          # a key that has events is left alone
    assert live.allow("d") and not live.allow("d")
    guard.RateLimiter(0, 600).seed("x", [now])                                  # a disabled window ignores a seed


def test_a_seeded_window_refuses_exactly_when_the_rebuilt_ledger_says_the_address_is_used_up():
    now = time.monotonic()
    window = guard.RateLimiter(3, 600)
    window.seed("addr", [now - 100, now - 50, now - 10])
    assert window.allow("addr") is False
    window.seed("fresh", [now - 100])
    assert [window.allow("fresh") for _ in range(3)] == [True, True, False]


# ---------------------------------------------------------------- the pepper (I3) on the rows the new path writes

PEPPER = "wiring-test-pepper-0123456789-abcdefghijklm"  # gitleaks:allow
ADDRESS = {"fly-client-ip": "198.51.100.7"}


class PepperedSettings(RouteSettings):
    client_ip_header = "fly-client-ip"
    ip_hash_pepper = PEPPER
    ip_hash_version = 2


def test_a_cached_answer_is_ledgered_under_the_peppered_hash_with_its_version(make_client, backend, cached_rows):
    backend.cache[store.cache_key(Q, "hybrid", "")] = {"answer": "cached", "citations": [], "hallucinated": [],
                                                       "source": "benchmark"}
    client = make_client(settings=PepperedSettings())
    r = client.post("/api/ask", json={"question": Q}, headers=ADDRESS)
    assert r.status_code == 200 and '"cached": true' in r.text
    assert cached_rows == [{"ip_hash": guard.ip_hash("198.51.100.7", PEPPER), "strategy": "hybrid", "cached": True,
                            "ip_hash_v": 2}]


def paid_row_of_the_real_backend(make_client, **backend_changes) -> dict:
    ledger = InMemoryLedger()
    real = make_backend(real_backend_settings(**backend_changes), StateDrivers(state=object()), ledger=ledger)
    real.refresh_kill_level()
    client = make_client(backend=real, settings=PepperedSettings())
    assert events_of(client.post("/api/ask", json={"question": Q}, headers=ADDRESS))[-1]["event"] == "done"
    row, = ledger.rows.values()
    return row


def test_the_paid_row_carries_the_peppered_hash_and_the_version_of_the_pepper(make_client):
    row = paid_row_of_the_real_backend(make_client, ip_hash_version=3)
    assert row["ip_hash"] == guard.ip_hash("198.51.100.7", PEPPER) and row["ip_hash_v"] == 3
    assert row["status"] == "settled" and row["outcome"] == "done" and row["workspace"] is False


def test_settings_without_a_version_leave_the_paid_row_without_one(make_client):
    assert paid_row_of_the_real_backend(make_client, ip_hash_version=None)["ip_hash_v"] is None


# ---------------------------------------------------------------- the schema the ledger needs exists before serving

def test_ensure_indexes_creates_the_ledger_constraints_and_indexes_of_the_state_package(monkeypatch):
    """``bootstrap`` runs ``store.ensure_indexes`` before the first paid ask: the unique ledger id, day counter and
    per-address day must exist by then (the same statements as ``ledger.ensure_state_schema``, sent through
    ``run_cypher`` like every other statement of ``ensure_indexes``)."""
    from semigraph.serve.state import ledger

    sent: list[str] = []
    monkeypatch.setattr(store, "run_cypher", lambda driver, statement, **params: sent.append(statement) or [])
    store.ensure_indexes(object())
    assert set(ledger.STATE_SCHEMA_STATEMENTS) <= set(sent) and len(sent) == len(set(sent))
    assert all("IF NOT EXISTS" in s or s.startswith("DROP INDEX") for s in sent)
