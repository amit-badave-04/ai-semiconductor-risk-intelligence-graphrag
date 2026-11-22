"""RT-007 residual: the workspace token check took the shared cache budget before Turnstile and answered 429 when it was
empty, so an anonymous drain starved workspace users. With the budget empty the check is deferred to after Turnstile,
runs unbudgeted and still runs before the paid window; it is never skipped."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/: the shared fakes

from serve_app_factory import FakeSettings, build_app, patch_answer_paths, patch_data_paths  # noqa: E402
from serve_state_fakes import FakeStateBackend, fresh_drain  # noqa: E402, F401 - fresh_drain: a fixture

from semigraph.serve import routes  # noqa: E402
from semigraph.serve.guard import TokenBucket  # noqa: E402

WS, TOKEN, IP = "0123456789abcdef0123456789abcdef", "t" * 22, "203.0.113.9"
QUESTION = "Which HBM suppliers does Nvidia depend on?"


class UploadsOn(FakeSettings):
    uploads_enabled = True


def build(monkeypatch):
    backend = FakeStateBackend()
    app = build_app(UploadsOn(turnstile_stub=True, client_ip_header="fly-client-ip"), backend=backend)
    app.state.uploads_ready = True
    patch_answer_paths(monkeypatch)
    patch_data_paths(monkeypatch)
    monkeypatch.setattr(routes, "authenticate_workspace", lambda d, ws, tok: ws == WS and tok == TOKEN)
    app.state.cache_budget = TokenBucket(2, clock=lambda: 0.0)
    for _ in range(2):
        assert app.state.cache_budget.take()                      # drained, never refills
    return app, backend


def ask(client, turnstile, token):
    body = {"question": QUESTION, "workspace_id": WS}
    if turnstile:
        body["turnstile_token"] = turnstile
    return client.post("/api/ask", json=body, headers={"fly-client-ip": IP, "x-workspace-token": token})


def test_drained_budget_verified_good_token_is_not_429(monkeypatch, fresh_drain):
    app, _ = build(monkeypatch)
    with TestClient(app) as client:
        reply = ask(client, "valid", TOKEN)
    assert reply.status_code != 429 and reply.status_code != 404, reply.text


def test_drained_budget_unverified_is_403(monkeypatch, fresh_drain):
    app, _ = build(monkeypatch)
    with TestClient(app) as client:
        reply = ask(client, None, TOKEN)
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT, reply.text


def test_drained_budget_verified_bad_token_is_404_and_takes_no_reservation(monkeypatch, fresh_drain):
    app, backend = build(monkeypatch)
    with TestClient(app) as client:
        reply = ask(client, "valid", "x" * 22)
    assert reply.status_code == 404 and reply.json()["detail"] == routes.MSG_WORKSPACE_NOT_FOUND, reply.text
    assert "reserve" not in backend.names()
