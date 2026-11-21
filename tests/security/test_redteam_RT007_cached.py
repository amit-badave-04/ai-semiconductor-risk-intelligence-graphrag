"""RT-007 missing pins: with the shared cache budget empty, a CACHED question is never served before Turnstile, and a
verified ask for it is served from the cache after Turnstile with no paid reservation and no budget token."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/: the shared fakes

from serve_app_factory import FakeSettings, build_app, patch_answer_paths, patch_data_paths  # noqa: E402
from serve_state_fakes import FakeStateBackend, fresh_drain  # noqa: E402, F401 - fresh_drain: a fixture

from semigraph.serve import routes, store  # noqa: E402
from semigraph.serve.guard import TokenBucket  # noqa: E402

QUESTION = "Which HBM suppliers does Nvidia depend on?"
CACHED_ANSWER = "Cached answer: SK hynix and Samsung."
IP = "203.0.113.9"


def build(monkeypatch):
    backend = FakeStateBackend()
    app = build_app(FakeSettings(turnstile_stub=True, client_ip_header="fly-client-ip"), backend=backend)
    patch_answer_paths(monkeypatch)
    patch_data_paths(monkeypatch)
    key = store.cache_key(QUESTION, "hybrid", getattr(app.state, "snapshot_id", ""))
    backend.cache[key] = {"answer": CACHED_ANSWER, "source": "live", "citations": [], "hallucinated": []}
    app.state.cache_budget = TokenBucket(3, clock=lambda: 0.0)
    for _ in range(3):
        assert app.state.cache_budget.take()                      # the budget is empty and never refills
    return app, backend


def ask(client, token):
    body = {"question": QUESTION, "strategy": "hybrid"}
    if token is not None:
        body["turnstile_token"] = token
    return client.post("/api/ask", json=body, headers={"fly-client-ip": IP})


def test_an_unverified_ask_for_a_cached_question_on_an_empty_budget_is_403_and_never_served(monkeypatch, fresh_drain):
    app, backend = build(monkeypatch)
    with TestClient(app) as client:
        reply = ask(client, None)
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT, reply.text
    assert CACHED_ANSWER not in reply.text and "cache_get" not in backend.names()


def test_a_verified_ask_for_a_cached_question_on_an_empty_budget_is_served_free_after_turnstile(monkeypatch, fresh_drain):
    app, backend = build(monkeypatch)
    takes = []
    app.state.cache_budget.take = lambda: takes.append(1) or False   # spy: the budget stays empty
    with TestClient(app) as client:
        reply = ask(client, "valid-token")
    assert reply.status_code == 200 and CACHED_ANSWER in reply.text and '"cached": true' in reply.text, reply.text
    assert "reserve" not in backend.names(), "a cache hit must not take a paid reservation"
    assert len(takes) == 1, "only the skipped pre-Turnstile attempt may touch the budget; the post-Turnstile read is free"
