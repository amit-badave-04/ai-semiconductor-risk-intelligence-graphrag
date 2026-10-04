"""HTTP-layer tests for semigraph.serve — every gate, the SSE framing, the
cache path and the admin surface, with Neo4j, the embedder and the LLM faked.

The app is built with a small lifespan of its own (the production one connects to Neo4j): it builds the limiters INSIDE
the running loop, which ``with TestClient(app)`` keeps alive for the whole test, so every request and every call made
through ``client.portal`` shares the loop the limiters belong to. The rest of the state the routes read is installed by
the ``client`` fixture, and the store/answer functions the routes call are monkeypatched at the module level (the answer
writers are ASYNC generators, as the route requires), so what is exercised is exactly the routing + policy logic.
"""

import asyncio
import contextlib
import json
import sys
import types

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import semigraph.serve.routes as routes
from semigraph.artifacts import load_examples
from semigraph.serve import store
from semigraph.serve.guard import RateLimiter
from semigraph.serve.limiters import make_limiters
from semigraph.serve.stream_runtime import PaidStream

Q = "Which HBM suppliers does Nvidia depend on, and which export rules apply?"
CID = "0001045810-26-000021:I.1:0320"


class FakeSettings:
    kill_switch = False
    max_queries_per_day = 3
    turnstile_secret_key = ""
    turnstile_site_key = ""
    is_production = False
    max_question_chars = 200
    llm_request_timeout_s = 5
    llm_answer_max_tokens = 100
    answer_cache_ttl_hours = 24
    rate_limit_questions = 2
    rate_limit_window_seconds = 600
    max_concurrent_answers = 1
    admin_token = "secret-token"
    client_ip_header = ""
    free_rate_limit_questions = 10
    stats_cache_seconds = 0
    turnstile_required = False
    read_rate_limit_per_minute = 5
    llm_model = "anthropic/claude-sonnet-5"
    answer_model = "anthropic/claude-sonnet-5"
    escalation_model = ""
    agent_enabled = False
    uploads_enabled = False     # M4: every workspace route and workspace ask answers 503 while off
    freshness_enabled = False
    embed_slots = 1             # M5a: the limiters ``main.lifespan`` builds from these
    db_thread_limit = 4
    send_timeout_s = 30
    loop_lag_warn_ms = 100


class Fakes:
    """In-memory stand-ins for the Neo4j-backed store."""

    def __init__(self):
        self.answers: dict[str, dict] = {}
        self.policy: dict[str, str] = {}
        self.queries: list[dict] = []
        self.paid_today = 0

    def put_answer(self, driver, **kw):
        self.answers[store.cache_key(kw["question"], kw["strategy"])] = {
            "answer": kw["answer"], "source": "live",
            "citations": kw["citations"], "hallucinated": kw["hallucinated"]}

    def install(self, monkeypatch):
        monkeypatch.setattr(store, "get_answer", lambda d, key, ttl: self.answers.get(key))
        monkeypatch.setattr(store, "put_answer", self.put_answer)
        monkeypatch.setattr(store, "log_query", lambda d, **kw: self.queries.append(kw))
        monkeypatch.setattr(store, "kill_switch_on",
                            lambda d, flag: flag or self.policy.get("kill_switch") == "on")
        monkeypatch.setattr(store, "paid_queries_today", lambda d: self.paid_today)
        monkeypatch.setattr(store, "get_policy", lambda d, k: self.policy.get(k))
        monkeypatch.setattr(store, "set_policy", lambda d, k, v: self.policy.__setitem__(k, v))
        monkeypatch.setattr(store, "ledger_summary", lambda d: {"today": {"paid": len(self.queries)}})


async def fake_answer_stream(question, driver, embedder, strategy="hybrid", **kw):
    yield {"event": "retrieval", "anchors": {"Nvidia": 1045810},
           "counts": {"edges": 2, "metrics": 1, "risks": 1, "temporal": 0, "chunks": 1}}
    yield {"event": "delta", "text": "Nvidia depends on "}
    yield {"event": "delta", "text": f"HBM suppliers [{CID}]."}
    yield {"event": "done", "answer": f"Nvidia depends on HBM suppliers [{CID}].", "citations": [CID],
           "hallucinated": [], "finish_reason": "stop",
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.00007,
           "chunk_ids": [CID], "context_chars": 100, "strategy": strategy, "question": question}


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    f.install(monkeypatch)
    monkeypatch.setattr(routes, "aanswer_stream", fake_answer_stream)
    return f


# ---------------------------------------------------------------- R2: strategy=agent must clear every gate too (never exempt)

def install_agent_stream_that_must_not_run(monkeypatch):
    """A ``semigraph.agent.stream_async`` stub for a gate that must refuse the request before a single byte of an
    answer, whichever strategy: turns a regression that exempted ``strategy=agent`` from the gate into a hard test
    failure (0 planner calls) instead of a quietly-passing green run."""
    def never(*a, **kw):
        pytest.fail("a gate-refused /api/ask must never reach the agent stream")
    install_agent_module(monkeypatch, never)


def install_agent_module(monkeypatch, stream):
    """Register ``stream`` as ``semigraph.agent.stream_async.aagent_answer_stream`` (the route needs no langgraph)."""
    stub = types.ModuleType("semigraph.agent.stream_async")
    stub.aagent_answer_stream = stream
    monkeypatch.setitem(sys.modules, "semigraph.agent", types.ModuleType("semigraph.agent"))
    monkeypatch.setitem(sys.modules, "semigraph.agent.stream_async", stub)


def install_counting_agent_stream(monkeypatch, calls: list):
    """A WORKING ``semigraph.agent.stream_async`` stub that records every call it receives. For a gate whose quota must
    first be exhausted by real (accepted) requests — the per-IP window, the free tier — what proves the gate is not
    exempting the agent is that ``calls`` stops growing exactly when the gate starts refusing, not that it is never
    called at all."""
    async def counted(question, driver, embedder, strategy="agent", **kw):
        calls.append((question, strategy))
        async for event in fake_answer_stream(question, driver, embedder, strategy):
            yield event
    install_agent_module(monkeypatch, counted)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """What ``main.lifespan`` builds inside the running loop (the limiters are bound to it)."""
    app.state.limiters = make_limiters(app.state.settings)
    app.state.answer_limiter = anyio.CapacityLimiter(app.state.settings.max_concurrent_answers)
    app.state.loop = asyncio.get_running_loop()
    yield


@pytest.fixture
def client(fakes):
    app = FastAPI(lifespan=_lifespan)
    app.include_router(routes.router)
    app.state.settings = FakeSettings()
    app.state.driver = object()
    app.state.embedder = type("E", (), {"name": "fake-embedder"})()
    app.state.graph_stats = {"nodes": {"Company": 26}, "relationships": 1, "deleted_risk_lineages": 0}
    app.state.rate_limiter = RateLimiter(FakeSettings.rate_limit_questions,
                                         FakeSettings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(FakeSettings.free_rate_limit_questions,
                                              FakeSettings.rate_limit_window_seconds)
    app.state.read_rate_limiter = RateLimiter(FakeSettings.read_rate_limit_per_minute, 60)
    with TestClient(app) as test_client:         # ONE event loop for the whole test (the lifespan runs in it)
        yield test_client


def run_stream(client, question, strategy="hybrid", iph="iph"):
    """Drive one ``PaidStream`` to its end on the test client's own loop, settle it the way the response does, and
    return the decoded events."""
    async def go():
        stream = PaidStream(client.app.state, question, strategy, iph, twin=routes._stream_fn(strategy))
        try:
            return [json.loads(event.data) async for event in stream.events()]
        finally:
            await stream.finalize()
    return client.portal.call(go)


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        data = "".join(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            events.append(json.loads(data))
    return events


# --- free surface ---

def test_index_sets_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "semigraph" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_examples_lists_benchmark_questions_without_answers(client):
    body = client.get("/api/examples").json()
    assert len(body["examples"]) == len(load_examples()["examples"])          # no bootstrap state: every packaged example is listed
    assert set(body["examples"][0]) == {"id", "type", "question"}


def test_stats_reports_limits_and_models(client):
    body = client.get("/api/stats").json()
    assert body["limits"]["max_queries_per_day"] == 3
    assert body["models"]["embedder"] == "fake-embedder"
    assert body["models"]["escalation"] is None   # not configured on the test double


def test_evidence_rejects_malformed_ids(client):
    assert client.get("/api/evidence/not-a-chunk").status_code == 400


# --- ask: validation and gates ---

def test_ask_rejects_short_and_long_questions(client):
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 201}).status_code == 400


def test_ask_rejects_unknown_strategy(client):
    assert client.post("/api/ask", json={"question": Q, "strategy": "magic"}).status_code == 400


def test_the_agent_strategy_is_refused_and_unadvertised_while_the_agent_is_off(client, fakes):
    assert client.get("/api/stats").json()["agent_enabled"] is False
    r = client.post("/api/ask", json={"question": Q, "strategy": "agent"})
    assert r.status_code == 400 and "agent" not in r.text
    assert fakes.queries == []                                     # refused before any ledger write


def test_an_enabled_agent_streams_through_the_lazily_imported_agent_and_is_ledgered_as_agent(client, fakes, monkeypatch):
    seen = {}

    async def agent_stream(question, driver, embedder, strategy="agent", **kw):
        seen.update(kw)
        async for event in fake_answer_stream(question, driver, embedder, strategy):
            yield event

    install_agent_module(monkeypatch, agent_stream)
    client.app.state.settings.agent_enabled = True
    try:
        assert client.get("/api/stats").json()["agent_enabled"] is True
        events = parse_sse(client.post("/api/ask", json={"question": Q, "strategy": "agent"}).text)
    finally:
        client.app.state.settings.agent_enabled = False
    assert events[-1]["event"] == "done" and events[-1]["strategy"] == "agent"
    assert seen["settings"] is client.app.state.settings           # the agent reads its limits from the service's settings
    assert fakes.queries[-1]["strategy"] == "agent" and fakes.queries[-1]["cached"] is False


def test_ask_streams_retrieval_deltas_and_done_then_caches(client, fakes):
    r = client.post("/api/ask", json={"question": Q})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [e["event"] for e in events] == ["retrieval", "delta", "delta", "done"]
    assert events[-1]["citations"] == [CID] and events[-1]["hallucinated"] == []
    assert fakes.queries[-1]["cached"] is False and fakes.queries[-1]["cost_usd"] == 0.00007
    # second identical question (any spacing/case) is served from the cache
    r2 = client.post("/api/ask", json={"question": Q.upper() + "  "})
    done = parse_sse(r2.text)[0]
    assert done["cached"] is True and done["source"] == "live"
    assert fakes.queries[-1]["cached"] is True


def test_cached_answers_bypass_kill_switch_and_ceiling(client, fakes):
    client.post("/api/ask", json={"question": Q})
    fakes.policy["kill_switch"] = "on"
    fakes.paid_today = 99
    assert parse_sse(client.post("/api/ask", json={"question": Q}).text)[0]["cached"] is True


@pytest.mark.parametrize("strategy", ["hybrid", "agent"])
def test_kill_switch_blocks_paid_answers_with_503(client, fakes, monkeypatch, strategy):
    client.app.state.settings.agent_enabled = True
    install_agent_stream_that_must_not_run(monkeypatch)
    fakes.policy["kill_switch"] = "on"
    r = client.post("/api/ask", json={"question": Q, "strategy": strategy})
    assert r.status_code == 503 and "paused" in r.json()["detail"]
    assert fakes.queries == []                                     # refused before a ledger write, whichever strategy


@pytest.mark.parametrize("strategy", ["hybrid", "agent"])
def test_daily_ceiling_blocks_with_429(client, fakes, monkeypatch, strategy):
    client.app.state.settings.agent_enabled = True
    install_agent_stream_that_must_not_run(monkeypatch)
    fakes.paid_today = FakeSettings.max_queries_per_day
    r = client.post("/api/ask", json={"question": Q, "strategy": strategy})
    assert r.status_code == 429 and "budget" in r.json()["detail"]
    assert fakes.queries == []


@pytest.mark.parametrize("strategy", ["hybrid", "agent"])
def test_per_ip_rate_limit_after_window_quota(client, fakes, monkeypatch, strategy):
    client.app.state.settings.agent_enabled = True
    calls: list = []
    install_counting_agent_stream(monkeypatch, calls)
    for i in range(FakeSettings.rate_limit_questions):
        assert client.post("/api/ask", json={"question": f"{Q} variant {i}", "strategy": strategy}).status_code == 200
    calls_before, queries_before = len(calls), len(fakes.queries)
    r = client.post("/api/ask", json={"question": f"{Q} variant last", "strategy": strategy})
    assert r.status_code == 429 and "address" in r.json()["detail"]
    assert len(calls) == calls_before and len(fakes.queries) == queries_before   # the refused call reaches neither


def test_busy_slot_emits_error_event_and_keeps_slot_accounting(client, fakes):
    limiter, holder = client.app.state.answer_limiter, object()
    assert limiter.total_tokens == FakeSettings.max_concurrent_answers == 1
    client.portal.call(limiter.acquire_on_behalf_of_nowait, holder)
    try:
        events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
        assert events == [{"event": "error", "detail": routes.MSG_BUSY}]
        assert fakes.queries == [] and limiter.borrowed_tokens == 1    # the refused ask wrote nothing and took nothing
    finally:
        client.portal.call(limiter.release_on_behalf_of, holder)
    assert limiter.borrowed_tokens == 0
    client.portal.call(limiter.acquire_on_behalf_of_nowait, holder)    # the one slot is free again ...
    try:
        with pytest.raises(anyio.WouldBlock):                          # ... and still exactly one
            client.portal.call(limiter.acquire_on_behalf_of_nowait, object())
    finally:
        client.portal.call(limiter.release_on_behalf_of, holder)


def test_a_finished_ask_gives_its_slot_back_and_the_next_ask_is_served(client, fakes):
    for question in (Q + " one", Q + " two"):
        assert parse_sse(client.post("/api/ask", json={"question": question}).text)[-1]["event"] == "done"
        assert client.app.state.answer_limiter.borrowed_tokens == 0


def test_every_request_runs_on_the_loop_the_limiters_were_built_in(client, monkeypatch):
    loops = []

    async def noting(question, driver, embedder, strategy="hybrid", **kw):
        loops.append(asyncio.get_running_loop())
        async for event in fake_answer_stream(question, driver, embedder, strategy):
            yield event

    monkeypatch.setattr(routes, "aanswer_stream", noting)
    for question in (Q + " first", Q + " second"):
        assert parse_sse(client.post("/api/ask", json={"question": question}).text)[-1]["event"] == "done"
    assert loops == [client.app.state.loop] * 2        # a limiter on another loop fails: "different event loop"


def test_spoofed_forwarding_header_is_ignored_unless_configured(client, fakes):
    for i in range(FakeSettings.rate_limit_questions):
        r = client.post("/api/ask", json={"question": f"{Q} spoof {i}"},
                        headers={"CF-Connecting-IP": f"1.2.3.{i}", "Fly-Client-IP": f"5.6.7.{i}"})
        assert r.status_code == 200
    r = client.post("/api/ask", json={"question": f"{Q} spoof last"}, headers={"Fly-Client-IP": "9.9.9.9"})
    assert r.status_code == 429


def test_configured_header_keys_the_limiter(client, fakes):
    client.app.state.settings.client_ip_header = "fly-client-ip"
    for i in range(FakeSettings.rate_limit_questions):
        r = client.post("/api/ask", json={"question": f"{Q} hdr {i}"}, headers={"Fly-Client-IP": "10.0.0.1"})
        assert r.status_code == 200
    assert client.post("/api/ask", json={"question": f"{Q} hdr last"},
                       headers={"Fly-Client-IP": "10.0.0.1"}).status_code == 429
    assert client.post("/api/ask", json={"question": f"{Q} hdr other"},
                       headers={"Fly-Client-IP": "10.0.0.2"}).status_code == 200


def test_mid_stream_error_event_still_logs_spend(client, fakes, monkeypatch):
    async def failing(*a, **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {"event": "delta", "text": "partial"}
        yield {"event": "error", "detail": "ServiceUnavailableError: overloaded", "partial": "partial",
               "usage": {"prompt_tokens": 500, "completion_tokens": 20}, "cost_usd": 0.0012,
               "strategy": "hybrid"}
    monkeypatch.setattr(routes, "aanswer_stream", failing)
    events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
    assert events[-1]["event"] == "error" and "overloaded" not in events[-1]["detail"]
    last = fakes.queries[-1]
    assert last["cached"] is False and last["cost_usd"] == 0.0012 and last["usage"]["prompt_tokens"] == 500
    assert fakes.answers == {}


def test_the_mid_stream_error_log_line_redacts_a_secret_shaped_string_in_the_exception_text(client, fakes, monkeypatch, caplog):
    """The client-facing message is already generic (asserted above); this is the SERVER log line, which used to carry
    ``ev["detail"]`` (an f-string of the provider exception's type and text) verbatim."""
    secret = "sk-live-abcdef1234567890"    # a fake, deliberately secret-shaped canary for the redaction test below, never a real key — gitleaks:allow

    async def failing(*a, **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {"event": "error", "detail": f"BadRequestError: upstream rejected key {secret}",
               "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "cost_usd": 0.002, "strategy": "hybrid"}
    monkeypatch.setattr(routes, "aanswer_stream", failing)
    caplog.set_level("WARNING", logger="semigraph.serve")
    events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
    assert events[-1]["event"] == "error" and secret not in events[-1]["detail"]
    line = next(r.getMessage() for r in caplog.records if "mid-stream" in r.getMessage())
    assert secret not in line and "***" in line


@pytest.mark.parametrize("strategy", ["hybrid", "agent"])
def test_free_tier_window_gates_cache_hits_before_any_write(client, fakes, monkeypatch, strategy):
    client.app.state.settings.agent_enabled = True
    calls: list = []
    install_counting_agent_stream(monkeypatch, calls)
    client.post("/api/ask", json={"question": Q, "strategy": strategy})  # paid, populates the cache
    n_before, calls_before = len(fakes.queries), len(calls)
    for _ in range(FakeSettings.free_rate_limit_questions - 1):
        assert client.post("/api/ask", json={"question": Q, "strategy": strategy}).status_code == 200
    assert client.post("/api/ask", json={"question": Q, "strategy": strategy}).status_code == 429
    assert len(fakes.queries) == n_before + FakeSettings.free_rate_limit_questions - 1
    assert len(calls) == calls_before                    # every request after the first is a cache hit: 0 more planner calls


def test_admin_non_ascii_header_is_404_not_500(client):
    # a latin-1 byte the ASGI layer decodes to a non-ASCII str must not 500 in compare_digest
    raw = ("caf" + chr(233)).encode("latin-1")
    assert client.get("/api/admin/policy", headers={b"x-admin-token": raw}).status_code == 404


def test_stream_failure_emits_error_event_and_releases_slot(client, fakes, monkeypatch):
    async def boom(*a, **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        raise RuntimeError("provider down")
    monkeypatch.setattr(routes, "aanswer_stream", boom)
    events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
    assert events[-1]["event"] == "error" and "RuntimeError" in events[-1]["detail"]
    assert client.app.state.answer_limiter.borrowed_tokens == 0           # slot was released
    assert len(fakes.queries) == 1 and "usage" not in fakes.queries[0]    # one ledger row, no spend known


def test_truncated_answers_are_not_cached(client, fakes, monkeypatch):
    async def truncated(*a, **kw):
        yield {"event": "done", "answer": "partial", "citations": [], "hallucinated": [],
               "finish_reason": "length", "usage": None, "cost_usd": None, "chunk_ids": [],
               "context_chars": 0}
    monkeypatch.setattr(routes, "aanswer_stream", truncated)
    client.post("/api/ask", json={"question": Q})
    assert fakes.answers == {} and fakes.queries[-1]["cached"] is False


# --- admin ---

def test_admin_requires_token_and_toggles_kill_switch(client, fakes):
    assert client.get("/api/admin/policy").status_code == 404
    assert client.get("/api/admin/policy", headers={"X-Admin-Token": "wrong"}).status_code == 404
    ok = {"X-Admin-Token": "secret-token"}
    assert client.get("/api/admin/policy", headers=ok).json()["kill_switch"] == "off"
    assert client.post("/api/admin/policy", json={"kill_switch": True},
                       headers=ok).json()["kill_switch"] == "on"
    assert fakes.policy["kill_switch"] == "on"


def test_admin_disabled_when_no_token_configured(client):
    client.app.state.settings.admin_token = ""
    assert client.get("/api/admin/policy", headers={"X-Admin-Token": ""}).status_code == 404


@pytest.mark.parametrize("strategy", ["hybrid", "agent"])
def test_turnstile_required_fails_closed_without_keys(client, fakes, monkeypatch, strategy):
    client.app.state.settings.agent_enabled = True
    install_agent_stream_that_must_not_run(monkeypatch)
    client.app.state.settings.turnstile_required = True
    r = client.post("/api/ask", json={"question": Q, "strategy": strategy})
    assert r.status_code == 403 and "Bot check" in r.json()["detail"]
    assert fakes.queries == []


def test_read_endpoints_are_rate_limited(client):
    n = FakeSettings.read_rate_limit_per_minute
    codes = [client.get("/api/stats").status_code for _ in range(n + 1)]
    assert codes[:-1] == [200] * n and codes[-1] == 429
    assert client.get("/api/evidence/not-a-chunk").status_code == 429  # same window


def test_a_cached_answer_is_not_served_after_the_data_snapshot_changes(client, fakes, monkeypatch):
    """The cache key includes the snapshot id: after a data refresh yesterday's answer is a miss."""
    monkeypatch.setattr(store, "put_answer",
                        lambda d, **kw: fakes.answers.__setitem__(
                            store.cache_key(kw["question"], kw["strategy"], kw.get("snapshot_id", "")),
                            {"answer": kw["answer"], "source": "live", "citations": kw["citations"],
                             "hallucinated": kw["hallucinated"]}))
    client.app.state.snapshot_id = "snap-20260924-aaaaaaaaaa"
    first = parse_sse(client.post("/api/ask", json={"question": "Who does Nvidia depend on for HBM?"}).text)
    assert first[-1]["event"] == "done" and not first[-1].get("cached")

    again = parse_sse(client.post("/api/ask", json={"question": "Who does Nvidia depend on for HBM?"}).text)
    assert again[-1].get("cached") is True                       # same snapshot: served from cache

    client.app.state.snapshot_id = "snap-20260925-bbbbbbbbbb"   # the graph was rebuilt from newer data
    after = parse_sse(client.post("/api/ask", json={"question": "Who does Nvidia depend on for HBM?"}).text)
    assert not after[-1].get("cached")                           # miss: computed from the new data


def test_stats_report_the_snapshot_the_service_is_serving(client):
    client.app.state.snapshot = {"id": "snap-20260924-7feaaf9bfe", "as_of": "2026-09-24"}
    body = client.get("/api/stats").json()
    assert body["snapshot"] == {"id": "snap-20260924-7feaaf9bfe", "as_of": "2026-09-24"}


def test_stats_without_a_snapshot_report_null(client):
    assert client.get("/api/stats").json()["snapshot"] is None


# ---------------------------------------------- evidence drawer exposes freshness (the AMD 10-K/A case)

def test_evidence_returns_freshness_so_the_ui_can_flag_a_corrected_paragraph(client, monkeypatch):
    seen = {}

    def fake_run_cypher(driver, query, **params):
        seen["query"], seen["params"] = query, params
        return [{"chunk_id": CID, "text": "31% increase in unit shipments", "source_url": "https://sec.gov/x",
                 "section_key": "0000002488-26-000018:II.7", "section_title": "MD&A",
                 "accession_no": "0000002488-26-000018", "form": "10-K", "filing_date": "2026-02-04",
                 "filer": "AMD", "mentions": [], "status": "corrected", "is_current": False,
                 "retrievable": False, "valid_to": "2026-02-04", "superseded_by": None,
                 "corrected_by": "0000002488-26-000021"}]

    monkeypatch.setattr(routes, "run_cypher", fake_run_cypher)

    body = client.get(f"/api/evidence/{CID}").json()

    assert body["status"] == "corrected" and body["is_current"] is False
    assert body["corrected_by"] == "0000002488-26-000021" and body["valid_to"] == "2026-02-04"
    for needed in ("e.status", "e.is_current", "e.valid_to", "AMENDS"):
        assert needed in seen["query"]
    assert seen["params"] == {"id": CID}


def test_evidence_404_when_the_chunk_is_unknown(client, monkeypatch):
    monkeypatch.setattr(routes, "run_cypher", lambda driver, query, **p: [])
    assert client.get(f"/api/evidence/{CID}").status_code == 404


def test_the_route_hands_the_configured_escalation_model_to_the_answerer(client, monkeypatch):
    seen = {}

    async def capture(question, driver, embedder, strategy="hybrid", **kw):
        seen.update(kw)
        async for event in fake_answer_stream(question, driver, embedder, strategy):
            yield event

    monkeypatch.setattr(routes, "aanswer_stream", capture)
    monkeypatch.setattr(FakeSettings, "escalation_model", "anthropic/claude-sonnet-5")
    client.post("/api/ask", json={"question": Q})
    assert seen["escalation_model"] == "anthropic/claude-sonnet-5"
    assert seen["limiters"] is client.app.state.limiters          # the route hands the twin the app's named limiters
    assert seen["timeout"] == FakeSettings.llm_request_timeout_s
    assert seen["max_tokens"] == FakeSettings.llm_answer_max_tokens


def test_no_escalation_model_means_the_answerer_is_told_none(client, monkeypatch):
    seen = {}

    async def capture(question, driver, embedder, strategy="hybrid", **kw):
        seen.update(kw)
        async for event in fake_answer_stream(question, driver, embedder, strategy):
            yield event

    monkeypatch.setattr(routes, "aanswer_stream", capture)
    client.post("/api/ask", json={"question": Q + " again"})
    assert seen["escalation_model"] is None


# --- review finding S1 and the escalation events through the route ---

def test_a_client_that_disconnects_during_the_answer_still_costs_a_ledger_row(client, fakes):
    async def disconnect():
        stream = PaidStream(client.app.state, Q + " disconnect", "hybrid", "iph", twin=routes._stream_fn("hybrid"))
        gen = stream.events()
        await gen.__anext__()          # retrieval
        await gen.__anext__()          # first delta
        await gen.aclose()             # the browser went away before `done`
        await stream.finalize()        # what the response does after the disconnect

    client.portal.call(disconnect)
    assert len(fakes.queries) == 1 and fakes.queries[0].get("cost_usd") is None
    assert client.app.state.answer_limiter.borrowed_tokens == 0


def test_a_completed_answer_writes_exactly_one_ledger_row(client, fakes):
    events = run_stream(client, Q + " complete")
    assert events and len(fakes.queries) == 1 and fakes.queries[0]["cost_usd"] == 0.00007


def test_an_escalated_answer_passes_through_with_one_ledger_row_carrying_the_summed_cost(client, fakes, monkeypatch):
    async def escalating(question, driver, embedder, strategy="hybrid", **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {"event": "escalated", "reasons": ["invalid_citation"], "from": "cheap/m", "to": "strong/m"}
        yield {"event": "delta", "text": "Strong answer."}
        yield {"event": "done", "answer": "Strong answer.", "citations": [], "hallucinated": [], "finish_reason": "stop",
               "usage": {"prompt_tokens": 300, "completion_tokens": 30}, "cost_usd": 0.031, "escalated": True,
               "routed": "cheap", "answered_by": "strong/m", "escalation_reasons": ["invalid_citation"]}

    monkeypatch.setattr(routes, "aanswer_stream", escalating)
    events = run_stream(client, Q + " esc")
    assert [e["event"] for e in events] == ["retrieval", "escalated", "delta", "done"]
    assert len(fakes.queries) == 1 and fakes.queries[0]["cost_usd"] == 0.031
    assert any(a["answer"] == "Strong answer." for a in fakes.answers.values())


# ---------------------------------------------------------------- M1b: the evidence endpoint resolves every citation id form

XBRL = "xbrl:1045810:revenue:2026-01-25"
FR = "fr:2026-19537"


def capture_cypher(monkeypatch, rows):
    seen = {}

    def fake(driver, query, **params):
        seen["query"], seen["params"] = query, params
        return rows

    monkeypatch.setattr(routes, "run_cypher", fake)
    return seen


def test_a_chunk_id_still_resolves_through_the_chunk_query_and_reports_its_type(client, monkeypatch):
    seen = capture_cypher(monkeypatch, [{"chunk_id": CID, "text": "t", "item_headlines": ["Export controls could restrict sales"]}])
    body = client.get(f"/api/evidence/{CID}").json()
    assert seen["query"] == routes.EVIDENCE_QUERY and seen["params"] == {"id": CID}
    assert body["type"] == "chunk" and body["item_headlines"] == ["Export controls could restrict sales"]


def test_the_chunk_query_also_returns_the_risk_item_headline_when_the_chunk_belongs_to_an_item():
    q = routes.EVIDENCE_QUERY
    assert "OPTIONAL MATCH (ri:RiskItem {filer_cik: c.cik, accession_no: f.accession_no}) WHERE $id IN ri.chunk_ids" in q
    assert "collect(ri.headline) AS item_headlines" in q
    for kept in ("e.status", "e.is_current", "e.valid_to", "AMENDS", "s.title AS section_title", "f.form AS form"):
        assert kept in q


def test_an_xbrl_id_resolves_to_the_metric_node_with_its_filing(client, monkeypatch):
    row = {"metric_id": "1045810:revenue:2026-01-25", "metric": "revenue", "concept": "Revenues",
           "value": 215938000000.0, "unit": "USD", "period_start": "2025-01-27", "period_end": "2026-01-25",
           "company": "Nvidia", "cik": 1045810, "accession_no": "0001045810-26-000021", "form": "10-K",
           "filing_date": "2026-02-25", "source_url": "https://www.sec.gov/x"}
    seen = capture_cypher(monkeypatch, [row])
    body = client.get(f"/api/evidence/{XBRL}").json()
    assert seen["query"] == routes.XBRL_EVIDENCE_QUERY and seen["params"] == {"id": "1045810:revenue:2026-01-25"}
    assert body["type"] == "xbrl" and body["value"] == 215938000000.0 and body["accession_no"] == "0001045810-26-000021"


def test_the_xbrl_query_reads_the_metric_the_edge_accession_and_reaches_the_filing_optionally():
    q = routes.XBRL_EVIDENCE_QUERY
    assert "MATCH (m:Metric {metric_id: $id})" in q
    assert "OPTIONAL MATCH (c:Company)-[rel:REPORTS_METRIC]->(m)" in q
    assert "OPTIONAL MATCH (f:Filing {accession_no: rel.accession_no})" in q      # the first-disclosing filing may not be in the graph
    for col in ("m.value AS value", "m.unit AS unit", "AS period_start", "AS period_end", "m.concept AS concept",
                "rel.accession_no AS accession_no", "c.name AS company"):
        assert col in q


def test_a_federal_register_id_resolves_to_the_rule_and_says_it_is_external(client, monkeypatch):
    row = {"document_number": "2026-19537", "title": "Implementation of Additional Export Controls",
           "publication_date": "2026-03-12", "url": "https://www.federalregister.gov/d/2026-19537",
           "kind": "entity_list", "topics": ["china"], "relevant": True, "abstract": "a"}
    seen = capture_cypher(monkeypatch, [row])
    body = client.get(f"/api/evidence/{FR}").json()
    assert seen["query"] == routes.FR_EVIDENCE_QUERY and seen["params"] == {"id": "2026-19537"}
    assert body["type"] == "fr" and body["kind"] == "entity_list" and body["document_number"] == "2026-19537"
    assert body["source"] == "federal_register" and body["external"] is True
    assert "not a company disclosure" in body["note"]


def test_a_correction_notice_id_is_accepted(client, monkeypatch):
    seen = capture_cypher(monkeypatch, [{"document_number": "C1-2026-16628", "title": "t"}])
    assert client.get("/api/evidence/fr:C1-2026-16628").status_code == 200
    assert seen["params"] == {"id": "C1-2026-16628"}


def test_the_federal_register_query_reads_the_rule_node():
    q = routes.FR_EVIDENCE_QUERY
    assert "MATCH (x:ExportControl {rule_id: $id})" in q
    for col in ("x.rule_id AS document_number", "x.title AS title", "toString(x.date) AS publication_date",
                "x.url AS url", "x.kind AS kind", "x.topics AS topics", "x.relevant AS relevant"):
        assert col in q


@pytest.mark.parametrize("bad", ["Reported Metrics", "xbrl:notacik:revenue:2026-01-25", "xbrl:1045810:Revenue:2026-01-25",
                                 "fr:12", "fr:", "0001045810-26-000021:I.1A", "x" * 200,
                                 "xbrl:1045810:" + "a" * 150 + ":2026-01-25"])
def test_a_malformed_evidence_id_is_a_400_and_never_reaches_the_database(client, monkeypatch, bad):
    seen = capture_cypher(monkeypatch, [])
    assert client.get(f"/api/evidence/{bad}").status_code == 400
    assert seen == {}


@pytest.mark.parametrize("evidence_id", [XBRL, FR])
def test_an_unknown_but_well_formed_id_is_a_404(client, monkeypatch, evidence_id):
    capture_cypher(monkeypatch, [])
    assert client.get(f"/api/evidence/{evidence_id}").status_code == 404


def test_every_evidence_form_is_behind_the_read_rate_limit(client, monkeypatch):
    capture_cypher(monkeypatch, [{"x": 1}])
    n = FakeSettings.read_rate_limit_per_minute
    codes = [client.get(f"/api/evidence/{XBRL}").status_code for _ in range(n + 1)]
    assert codes[:-1] == [200] * n and codes[-1] == 429


def test_the_route_still_exposes_the_chunk_id_pattern_the_id_module_owns():
    from semigraph.retrieval import ids

    assert routes.CHUNK_ID_RE is ids.CHUNK_ID_RE


# ---------------------------------------------------------------- M1b: the route logs the answer checks

CLEAN_CHECKS = {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [], "pseudo_citations": []}


def stream_with(checks, routed="strong"):
    async def stream(question, driver, embedder, strategy="hybrid", **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {"event": "done", "answer": "A.", "citations": [], "hallucinated": [], "finish_reason": "stop",
               "usage": None, "cost_usd": 0.01, "routed": routed, "escalated": False, "answered_by": "strong/m",
               **({"checks": checks} if checks is not None else {})}
    return stream


def test_the_done_log_line_carries_the_checks(client, monkeypatch, caplog):
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(CLEAN_CHECKS))
    caplog.set_level("INFO", logger="semigraph.serve")
    run_stream(client, Q + " log")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("answered "))
    assert "checks=" in line and "'numbers_grounded': True" in line


def test_a_strong_routed_answer_that_fails_a_check_is_logged_as_a_warning_not_hidden(client, monkeypatch, caplog):
    failed = {"citations_retrieved": True, "numbers_grounded": False, "unmatched_numbers": ["$190 billion"],
              "pseudo_citations": ["Reported Metrics"]}
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(failed))
    caplog.set_level("INFO", logger="semigraph.serve")
    events = run_stream(client, Q + " warn")
    warning = next(r for r in caplog.records if r.levelname == "WARNING" and "failed checks" in r.getMessage())
    assert "$190 billion" in warning.getMessage() and "routed=strong" in warning.getMessage()
    assert events[-1]["checks"] == failed                     # and the client still receives them on the done event


def test_an_event_without_checks_is_logged_without_error(client, monkeypatch):
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(None))
    assert run_stream(client, Q + " nochecks")


def test_an_answer_whose_checks_failed_is_not_cached_so_the_failure_cannot_vanish_on_replay(client, fakes, monkeypatch):
    """A cached replay carries no ``checks`` (the store does not persist them): caching a strong-routed answer that failed
    them would show it as clean for the whole TTL."""
    failed = {"citations_retrieved": True, "numbers_grounded": False, "unmatched_numbers": ["$190 billion"],
              "pseudo_citations": []}
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(failed))
    events = run_stream(client, Q + " failed")
    assert events[-1]["checks"] == failed
    assert fakes.answers == {} and len(fakes.queries) == 1 and fakes.queries[0]["cost_usd"] == 0.01   # still on the ledger


@pytest.mark.parametrize("failed", [
    {"citations_retrieved": False, "numbers_grounded": True, "unmatched_numbers": [], "pseudo_citations": []},
    {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [], "pseudo_citations": ["Excerpts"]},
])
def test_any_failed_check_keeps_the_answer_out_of_the_cache(client, fakes, monkeypatch, failed):
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(failed))
    run_stream(client, Q + " failed too")
    assert fakes.answers == {}


def test_an_answer_with_clean_checks_or_no_checks_is_cached_as_before(client, fakes, monkeypatch):
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(CLEAN_CHECKS))
    run_stream(client, Q + " clean")
    assert len(fakes.answers) == 1
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(None))
    run_stream(client, Q + " unchecked")
    assert len(fakes.answers) == 2


@pytest.mark.parametrize("evidence_id", [CID, XBRL, FR])
def test_an_evidence_id_with_a_trailing_newline_is_a_400_not_a_404(client, monkeypatch, evidence_id):
    """``%0A`` decodes to "\n": ``$`` used to accept it and the lookup answered 404 for an id nobody could have cited."""
    seen = capture_cypher(monkeypatch, [])
    assert client.get(f"/api/evidence/{evidence_id}%0A").status_code == 400
    assert seen == {}


# ---------------------------------------------------------------- review: the new checks keep an answer out of the cache

FULL_CLEAN = {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1, "unmatched_numbers": [],
              "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
              "unsupported_removal_claim": False, "unsupported_removal_sentences": []}


@pytest.mark.parametrize("failed", [
    {**FULL_CLEAN, "has_citation": False},                                                          # uncited, not a refusal
    {**FULL_CLEAN, "numbers_grounded": False, "echoed_numbers": ["$500 billion"]},                  # only the question said it
    {**FULL_CLEAN, "unsupported_removal_claim": True, "unsupported_removal_sentences": ["x was removed"]},
], ids=["uncited", "echoed", "removal"])
def test_an_uncited_echoing_or_removal_claiming_answer_is_not_cached(client, fakes, monkeypatch, failed):
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(failed))
    events = run_stream(client, Q + " newchecks")
    assert fakes.answers == {} and events[-1]["checks"] == failed        # the client still receives them


def test_a_zero_citation_refusal_with_full_checks_is_cached(client, fakes, monkeypatch):
    refusal = {**FULL_CLEAN, "numbers_checked": 0, "has_citation": False, "is_refusal": True}
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(refusal))
    run_stream(client, Q + " refusal")
    assert len(fakes.answers) == 1


def test_the_failed_check_warning_names_the_new_failures(client, monkeypatch, caplog):
    failed = {**FULL_CLEAN, "has_citation": False}
    monkeypatch.setattr(routes, "aanswer_stream", stream_with(failed))
    caplog.set_level("INFO", logger="semigraph.serve")
    run_stream(client, Q + " warnuncited")
    assert any(r.levelname == "WARNING" and "failed checks" in r.getMessage() and "'has_citation': False" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------- /healthz runs under its own one-thread limiter

def test_healthz_pings_the_database_on_a_worker_thread_under_the_health_limiter(client, monkeypatch):
    seen = []

    def ping(driver, query, **params):
        seen.append((query, client.app.state.limiters.health.borrowed_tokens))
        return [{"ok": 1}]

    monkeypatch.setattr(routes, "run_cypher", ping)
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "db": True, "embedder": "fake-embedder"}
    assert seen == [("RETURN 1 AS ok", 1)]                     # one token of the health limiter was held while it ran
    assert client.app.state.limiters.health.total_tokens == 1 and client.app.state.limiters.health.borrowed_tokens == 0


def test_healthz_reports_degraded_with_a_503_when_the_database_is_unreachable(client, monkeypatch):
    def down(driver, query, **params):
        raise ConnectionError("neo4j is down")

    monkeypatch.setattr(routes, "run_cypher", down)
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "degraded", "db": False}
    assert client.app.state.limiters.health.borrowed_tokens == 0
