"""M4 step 0.7: the ``/api/ask`` seam for upload workspaces (docs/v2/M4_PLAN.md 4.4 and 5).

A workspace ask is validated (shape, strategy, ``as_of``) -> the agent is refused (400, before Turnstile and the slot) -> uploads
must be enabled (503) -> the free-tier window -> the workspace token (404 for a bad id or token, indistinguishable) -> kill
switch -> daily ceiling -> Turnstile -> per-address paid window -> slot. It never reads or writes the answer cache (an answer
drawn from a private document must never be replayed to anyone else), and nothing is written before the token check. The public
evidence route refuses ``doc:`` ids outright: the workspace is not in the citation, so resolving one would leak existence.
"""

import sys
import types

import pytest
from test_serve_api import (  # noqa: F401 - pytest fixtures
    CID,
    Q,
    FakeSettings,
    client,
    fake_answer_stream,
    fakes,
    install_agent_stream_that_must_not_run,
)

import semigraph.serve.routes as routes
from semigraph.serve import guard, store

WS = "0123456789abcdef0123456789abcdef"
TOKEN = "t" * 22
DOC = "doc:0123456789ab:v1:0001"


class UploadsOn(FakeSettings):
    uploads_enabled = True


@pytest.fixture
def ws_client(client, monkeypatch):
    client.app.state.settings = UploadsOn()
    monkeypatch.setattr(routes, "authenticate_workspace", lambda driver, ws, token: ws == WS and token == TOKEN)
    return client


def install_workspace_stream(monkeypatch, calls: list):
    def stream(question, driver, embedder, strategy="hybrid", **kw):
        calls.append({"question": question, "strategy": strategy, **kw})
        yield {"event": "retrieval", "anchors": {}, "counts": {"chunks": 0}, "doc_chunks": 1}
        yield {"event": "delta", "text": f"From your document [{DOC}]."}
        yield {"event": "done", "answer": f"From your document [{DOC}].", "citations": [DOC], "hallucinated": [],
               "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.0001,
               "chunk_ids": [DOC], "context_chars": 50, "strategy": strategy, "question": question}
    stub = types.ModuleType("semigraph.retrieval.workspace")
    stub.stream_workspace_answer = stream
    monkeypatch.setitem(sys.modules, "semigraph.retrieval.workspace", stub)


def forbid_cache(monkeypatch):
    def never(*a, **kw):
        pytest.fail("a workspace ask must never touch the answer cache")
    monkeypatch.setattr(store, "get_answer", never)
    monkeypatch.setattr(store, "put_answer", never)


def ask(client, headers=None, **body):
    return client.post("/api/ask", json={"question": Q, **body}, headers=headers or {})


# ------------------------------------------------------------------------------------------------- the public evidence route

def test_the_public_evidence_route_refuses_an_uploaded_document_id_without_touching_the_database(client, monkeypatch):
    monkeypatch.setattr(routes, "run_cypher", lambda *a, **kw: pytest.fail("no query for a doc: id"))
    r = client.get(f"/api/evidence/{DOC}")
    assert r.status_code == 404 and r.json() == {"detail": "not a public evidence id"}


# ------------------------------------------------------------------------------------------------- validation, before any gate

@pytest.mark.parametrize("agent_enabled", [True, False])
def test_the_agent_is_refused_with_a_workspace_before_turnstile_and_the_slot(ws_client, monkeypatch, fakes, agent_enabled):
    ws_client.app.state.settings.agent_enabled = agent_enabled
    install_agent_stream_that_must_not_run(monkeypatch)
    monkeypatch.setattr(guard, "verify_turnstile", lambda *a, **kw: pytest.fail("Turnstile must not be reached"))
    r = ask(ws_client, {"X-Workspace-Token": TOKEN}, strategy="agent", workspace_id=WS)
    assert r.status_code == 400 and fakes.queries == []
    if agent_enabled:
        assert r.json()["detail"] == "strategy=agent is not available with a workspace"
    else:
        assert "agent" not in r.json()["detail"]      # a deployment with the agent off never advertises it


def test_a_workspace_ask_is_hybrid_only(ws_client, fakes):
    r = ask(ws_client, {"X-Workspace-Token": TOKEN}, strategy="vector", workspace_id=WS)
    assert r.status_code == 400 and fakes.queries == []


@pytest.mark.parametrize("ws", ["", "ABCDEF0123456789abcdef0123456789", "0123", "../../etc/passwd", WS + "0", WS[:-1] + "\n"])
def test_a_malformed_workspace_id_is_400(ws_client, fakes, ws):
    r = ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id=ws)
    assert r.status_code == 400 and fakes.queries == []


@pytest.mark.parametrize("as_of", ["2026-13-01", "2026-9-1", "yesterday", "2026-09-29T00:00:00", "2026-09-29\n"])
def test_a_malformed_as_of_is_400(ws_client, fakes, as_of):
    r = ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id=WS, as_of=as_of)
    assert r.status_code == 400 and fakes.queries == []


def test_as_of_without_a_workspace_is_400(client, fakes):
    r = ask(client, as_of="2026-09-01")
    assert r.status_code == 400 and fakes.queries == []


def test_workspace_asks_answer_503_while_uploads_are_off(client, fakes):
    r = ask(client, {"X-Workspace-Token": TOKEN}, workspace_id=WS)
    assert r.status_code == 503 and fakes.queries == []


# ------------------------------------------------------------------------------------------------- the token gate

@pytest.mark.parametrize("headers", [{}, {"X-Workspace-Token": "wrong"}, {"X-Workspace-Token": "x" * 500}])
def test_a_missing_or_wrong_token_is_404_and_writes_nothing(ws_client, fakes, monkeypatch, headers):
    forbid_cache(monkeypatch)
    r = ask(ws_client, headers, workspace_id=WS)
    assert r.status_code == 404 and r.json() == {"detail": "workspace not found"} and fakes.queries == []


def test_an_unknown_workspace_and_a_wrong_token_are_indistinguishable(ws_client, fakes):
    unknown = ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id="f" * 32)
    wrong = ask(ws_client, {"X-Workspace-Token": "wrong-token"}, workspace_id=WS)
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json()) == (404, {"detail": "workspace not found"})


def test_the_token_is_checked_before_the_kill_switch(ws_client, fakes):
    fakes.policy["kill_switch"] = "on"
    assert ask(ws_client, {"X-Workspace-Token": "wrong"}, workspace_id=WS).status_code == 404
    assert ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id=WS).status_code == 503


def test_the_daily_ceiling_applies_to_workspace_asks(ws_client, fakes):
    fakes.paid_today = FakeSettings.max_queries_per_day
    assert ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id=WS).status_code == 429


# ------------------------------------------------------------------------------------------------- an accepted workspace ask

def test_an_accepted_workspace_ask_streams_the_workspace_writer_and_never_touches_the_cache(ws_client, fakes, monkeypatch):
    forbid_cache(monkeypatch)
    calls = []
    install_workspace_stream(monkeypatch, calls)
    r = ask(ws_client, {"X-Workspace-Token": TOKEN}, workspace_id=WS, as_of="2026-09-01")
    assert r.status_code == 200 and DOC in r.text and "event: done" in r.text
    assert calls and calls[0]["workspace_id"] == WS and calls[0]["as_of"] == "2026-09-01" and calls[0]["strategy"] == "hybrid"
    assert len(fakes.queries) == 1 and fakes.queries[0]["workspace"] is True and fakes.queries[0]["cached"] is False


def test_a_public_ask_never_reaches_the_workspace_writer(client, fakes, monkeypatch):
    calls = []
    install_workspace_stream(monkeypatch, calls)
    r = ask(client)
    assert r.status_code == 200 and CID in r.text and calls == []
    assert fakes.queries[-1].get("workspace", False) is False


def test_the_stream_function_is_chosen_by_workspace_first(monkeypatch):
    calls = []
    install_workspace_stream(monkeypatch, calls)
    assert routes._stream_fn("hybrid", workspace=True) is sys.modules["semigraph.retrieval.workspace"].stream_workspace_answer
    assert routes._stream_fn("hybrid") is routes.answer_stream


# ------------------------------------------------------------------------------------------------- /api/stats

def test_stats_says_whether_uploads_are_enabled_and_carries_the_freshness_summary(client):
    body = client.get("/api/stats").json()
    assert body["uploads_enabled"] is False and body["freshness"] is None
    client.app.state.freshness_monitor = type("M", (), {"summary": lambda self: {"status": "ok", "checked_at": "t",
                                                                                  "pending_count": 2}})()
    assert client.get("/api/stats").json()["freshness"] == {"status": "ok", "checked_at": "t", "pending_count": 2}
