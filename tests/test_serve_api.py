"""HTTP-layer tests for semigraph.serve — every gate, the SSE framing, the
cache path and the admin surface, with Neo4j, the embedder and the LLM faked.

The app is built WITHOUT its lifespan (that would connect to Neo4j); the
state the routes read is installed by the ``client`` fixture, and the
store/answer functions the routes call are monkeypatched at the module
level, so what is exercised is exactly the routing + policy logic.
"""

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import semigraph.serve.routes as routes
from semigraph.serve import store
from semigraph.serve.guard import RateLimiter

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


def fake_answer_stream(question, driver, embedder, strategy="hybrid", **kw):
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
    monkeypatch.setattr(routes, "answer_stream", fake_answer_stream)
    return f


@pytest.fixture
def client(fakes):
    app = FastAPI()
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
    app.state.answer_slots = threading.BoundedSemaphore(FakeSettings.max_concurrent_answers)
    return TestClient(app)


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
    assert len(body["examples"]) == 20
    assert set(body["examples"][0]) == {"id", "type", "question"}


def test_stats_reports_limits_and_models(client):
    body = client.get("/api/stats").json()
    assert body["limits"]["max_queries_per_day"] == 3
    assert body["models"]["embedder"] == "fake-embedder"


def test_evidence_rejects_malformed_ids(client):
    assert client.get("/api/evidence/not-a-chunk").status_code == 400


# --- ask: validation and gates ---

def test_ask_rejects_short_and_long_questions(client):
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 201}).status_code == 400


def test_ask_rejects_unknown_strategy(client):
    assert client.post("/api/ask", json={"question": Q, "strategy": "magic"}).status_code == 400


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


def test_kill_switch_blocks_paid_answers_with_503(client, fakes):
    fakes.policy["kill_switch"] = "on"
    r = client.post("/api/ask", json={"question": Q})
    assert r.status_code == 503 and "paused" in r.json()["detail"]


def test_daily_ceiling_blocks_with_429(client, fakes):
    fakes.paid_today = FakeSettings.max_queries_per_day
    r = client.post("/api/ask", json={"question": Q})
    assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_per_ip_rate_limit_after_window_quota(client, fakes):
    for i in range(FakeSettings.rate_limit_questions):
        assert client.post("/api/ask", json={"question": f"{Q} variant {i}"}).status_code == 200
    r = client.post("/api/ask", json={"question": f"{Q} variant last"})
    assert r.status_code == 429 and "address" in r.json()["detail"]


def test_busy_slot_emits_error_event_and_keeps_slot_accounting(client):
    client.app.state.answer_slots.acquire()
    try:
        events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
        assert events == [{"event": "error", "detail": routes.MSG_BUSY}]
    finally:
        client.app.state.answer_slots.release()
    assert client.app.state.answer_slots.acquire(blocking=False)  # still exactly one slot
    client.app.state.answer_slots.release()


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
    def failing(*a, **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield {"event": "delta", "text": "partial"}
        yield {"event": "error", "detail": "ServiceUnavailableError: overloaded", "partial": "partial",
               "usage": {"prompt_tokens": 500, "completion_tokens": 20}, "cost_usd": 0.0012,
               "strategy": "hybrid"}
    monkeypatch.setattr(routes, "answer_stream", failing)
    events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
    assert events[-1]["event"] == "error" and "overloaded" not in events[-1]["detail"]
    last = fakes.queries[-1]
    assert last["cached"] is False and last["cost_usd"] == 0.0012 and last["usage"]["prompt_tokens"] == 500
    assert fakes.answers == {}


def test_free_tier_window_gates_cache_hits_before_any_write(client, fakes):
    client.post("/api/ask", json={"question": Q})  # paid, populates the cache
    n_before = len(fakes.queries)
    for _ in range(FakeSettings.free_rate_limit_questions - 1):
        assert client.post("/api/ask", json={"question": Q}).status_code == 200
    assert client.post("/api/ask", json={"question": Q}).status_code == 429
    assert len(fakes.queries) == n_before + FakeSettings.free_rate_limit_questions - 1


def test_admin_non_ascii_header_is_404_not_500(client):
    # a latin-1 byte the ASGI layer decodes to a non-ASCII str must not 500 in compare_digest
    raw = ("caf" + chr(233)).encode("latin-1")
    assert client.get("/api/admin/policy", headers={b"x-admin-token": raw}).status_code == 404


def test_stream_failure_emits_error_event_and_releases_slot(client, monkeypatch):
    def boom(*a, **kw):
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        raise RuntimeError("provider down")
    monkeypatch.setattr(routes, "answer_stream", boom)
    events = parse_sse(client.post("/api/ask", json={"question": Q}).text)
    assert events[-1]["event"] == "error" and "RuntimeError" in events[-1]["detail"]
    assert client.app.state.answer_slots.acquire(blocking=False)  # slot was released
    client.app.state.answer_slots.release()


def test_truncated_answers_are_not_cached(client, fakes, monkeypatch):
    def truncated(*a, **kw):
        yield {"event": "done", "answer": "partial", "citations": [], "hallucinated": [],
               "finish_reason": "length", "usage": None, "cost_usd": None, "chunk_ids": [],
               "context_chars": 0}
    monkeypatch.setattr(routes, "answer_stream", truncated)
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


def test_turnstile_required_fails_closed_without_keys(client, fakes):
    client.app.state.settings.turnstile_required = True
    r = client.post("/api/ask", json={"question": Q})
    assert r.status_code == 403 and "Bot check" in r.json()["detail"]
    assert fakes.queries == []


def test_read_endpoints_are_rate_limited(client):
    n = FakeSettings.read_rate_limit_per_minute
    codes = [client.get("/api/stats").status_code for _ in range(n + 1)]
    assert codes[:-1] == [200] * n and codes[-1] == 429
    assert client.get("/api/evidence/not-a-chunk").status_code == 429  # same window
