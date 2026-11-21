"""RT-007 (Low, CWE-400): the process-wide cache read budget is taken BEFORE the bot check (red-team finding, regression test).

``routes.ask`` -> ``_answer_from_cache`` -> ``_read_cache`` takes one token of ``app.state.cache_budget`` (routes.py:446)
before ``guard.verify_turnstile`` (routes.py:353). Anonymous asks that will fail Turnstile still drain the shared budget,
and once it is empty every public ask, from any address and with a valid Turnstile token, is answered 429
``MSG_READ_RATE``. The proposed fix skips the pre-Turnstile cache read when the budget is empty and reads the cache after
Turnstile instead.

The budget is drained through HTTP by anonymous asks (no Turnstile token) from one address, against a bucket whose clock
is frozen so it never refills during the test (the default bucket refills at 10,000 per second, which would make the
drain racy). That bucket is the state an anonymous flood leaves behind; nothing in the ask path is stubbed out.
"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/: the shared fakes

from serve_app_factory import FakeSettings, build_app, patch_answer_paths, patch_data_paths  # noqa: E402
from serve_state_fakes import FakeStateBackend, fresh_drain  # noqa: E402, F401 - fresh_drain: a fixture

from semigraph.serve import routes  # noqa: E402
from semigraph.serve.guard import TokenBucket  # noqa: E402

BUDGET = 3                                                        # cache reads the frozen bucket holds
ATTACKER_IP, VICTIM_IP = "198.51.100.66", "203.0.113.9"
QUESTION = "Which HBM suppliers does Nvidia depend on?"


def build(monkeypatch):
    settings = FakeSettings(turnstile_stub=True, client_ip_header="fly-client-ip")    # any non-empty token passes
    backend = FakeStateBackend()
    app = build_app(settings, backend=backend)
    app.state.cache_budget = TokenBucket(BUDGET, clock=lambda: 0.0)                     # frozen: never refills
    patch_answer_paths(monkeypatch)
    patch_data_paths(monkeypatch)
    return app


def ask(client: TestClient, ip: str, token: str | None):
    body = {"question": QUESTION, "strategy": "hybrid"}
    if token is not None:
        body["turnstile_token"] = token
    return client.post("/api/ask", json=body, headers={"fly-client-ip": ip})


def drain_budget_anonymously(app, client: TestClient) -> None:
    for _ in range(BUDGET):
        assert ask(client, ATTACKER_IP, None).status_code == 403          # every anonymous ask fails the bot check ...
    assert not app.state.cache_budget.take()                              # ... yet the shared budget is now empty


def test_a_verified_ask_from_a_fresh_address_is_not_starved_by_an_anonymous_drain(monkeypatch, fresh_drain):
    app = build(monkeypatch)
    with TestClient(app) as client:
        drain_budget_anonymously(app, client)
        reply = ask(client, VICTIM_IP, "valid-token")
        body = reply.text
    assert reply.status_code != 429, body
    assert reply.status_code == 200, body


def test_an_unverified_ask_on_an_empty_budget_meets_the_bot_check_not_the_budget(monkeypatch, fresh_drain):
    """Pins the ordering: Turnstile runs before the budget is consulted, so the fix cannot simply delete the budget."""
    app = build(monkeypatch)
    with TestClient(app) as client:
        drain_budget_anonymously(app, client)
        reply = ask(client, VICTIM_IP, None)
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT, reply.text


def test_a_verified_ask_answers_when_the_budget_is_not_drained(monkeypatch, fresh_drain):
    """The control: the same verified ask answers 200 today when nobody drained the budget."""
    app = build(monkeypatch)
    with TestClient(app) as client:
        reply = ask(client, VICTIM_IP, "valid-token")
        assert reply.status_code == 200, reply.text
