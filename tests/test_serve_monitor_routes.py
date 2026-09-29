"""``GET /api/freshness`` and ``POST /api/admin/freshness/check`` (M4, docs/v2/M4_PLAN.md 4.1).

The monitor itself is faked — this file is only about the ROUTE layer: the read-rate gate, the admin-token gate
(``routes._check_admin``, real and unmocked, so a change to it is caught here too), and the 503/409 mapping.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semigraph.serve import monitor as monitor_mod
from semigraph.serve import monitor_routes
from semigraph.serve.guard import RateLimiter


class FakeSettings:
    client_ip_header = ""
    read_rate_limit_per_minute = 5
    admin_token = "secret-token"
    freshness_enabled = True
    sec_user_agent = "Test Suite test@example.com"


class FakeMonitor:
    def __init__(self, payload=None, configured=True, check_now_result=None, check_now_error=None):
        self._payload = payload or {"configured": configured, "enabled": True, "status": "ok", "checked_at": None}
        self.configured = configured
        self._check_now_result = check_now_result
        self._check_now_error = check_now_error
        self.check_now_calls = 0

    def status_payload(self):
        return self._payload

    def check_now(self, timeout_s=120):
        self.check_now_calls += 1
        if self._check_now_error:
            raise self._check_now_error
        return self._check_now_result or self._payload


@pytest.fixture
def app_client():
    def _make(monitor=None, admin_token="secret-token", read_limit=5):
        app = FastAPI()
        app.include_router(monitor_routes.router)
        settings = FakeSettings()
        settings.admin_token = admin_token
        settings.read_rate_limit_per_minute = read_limit
        app.state.settings = settings
        app.state.read_rate_limiter = RateLimiter(read_limit, 60)
        app.state.freshness_monitor = monitor
        return TestClient(app)

    return _make


# ---------------------------------------------------------------------- GET /api/freshness

def test_get_freshness_returns_the_monitor_status_payload(app_client):
    monitor = FakeMonitor(payload={"configured": True, "enabled": True, "status": "ok", "checked_at": "x"})
    client = app_client(monitor=monitor)
    resp = client.get("/api/freshness")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_get_freshness_without_a_monitor_and_unconfigured_reports_unconfigured(app_client):
    client = app_client(monitor=None)
    client.app.state.settings.sec_user_agent = ""
    resp = client.get("/api/freshness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unconfigured" and body["configured"] is False


def test_get_freshness_without_a_monitor_but_configured_reports_never(app_client):
    client = app_client(monitor=None)
    resp = client.get("/api/freshness")
    body = resp.json()
    assert body["status"] == "never" and body["configured"] is True


def test_get_freshness_without_a_monitor_and_freshness_disabled_reports_disabled(app_client):
    """FRESHNESS_ENABLED=false: start_if_enabled never even creates a monitor. That must read as "disabled", never
    "unconfigured" (a different, misleading reason) or "never" (which implies the feature is live and idle)."""
    client = app_client(monitor=None)
    client.app.state.settings.freshness_enabled = False
    resp = client.get("/api/freshness")
    body = resp.json()
    assert body["status"] == "disabled" and body["enabled"] is False
    assert body["next_check_at"] is None and body["last_error_at"] is None


def test_get_freshness_is_read_rate_limited(app_client):
    client = app_client(monitor=FakeMonitor(), read_limit=1)
    assert client.get("/api/freshness").status_code == 200
    assert client.get("/api/freshness").status_code == 429


# ---------------------------------------------------------------------- POST /api/admin/freshness/check

def test_admin_check_without_a_token_is_refused_before_touching_the_monitor(app_client):
    monitor = FakeMonitor()
    client = app_client(monitor=monitor)
    resp = client.post("/api/admin/freshness/check")
    assert resp.status_code == 404  # routes._check_admin's real behaviour — see the module docstring
    assert monitor.check_now_calls == 0


def test_admin_check_with_the_wrong_token_is_refused(app_client):
    monitor = FakeMonitor()
    client = app_client(monitor=monitor)
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "wrong"})
    assert resp.status_code == 404
    assert monitor.check_now_calls == 0


def test_admin_check_with_the_right_token_runs_check_now(app_client):
    monitor = FakeMonitor(check_now_result={"status": "ok", "configured": True})
    client = app_client(monitor=monitor)
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "secret-token"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert monitor.check_now_calls == 1


def test_admin_check_503_when_no_monitor_is_running(app_client):
    client = app_client(monitor=None)
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "secret-token"})
    assert resp.status_code == 503


def test_admin_check_503_when_unconfigured(app_client):
    monitor = FakeMonitor(configured=False)
    client = app_client(monitor=monitor)
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "secret-token"})
    assert resp.status_code == 503
    assert monitor.check_now_calls == 0


def test_admin_check_409_when_a_check_is_already_running(app_client):
    monitor = FakeMonitor(check_now_error=monitor_mod.MonitorBusy())
    client = app_client(monitor=monitor)
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "secret-token"})
    assert resp.status_code == 409


def test_admin_check_404_when_no_admin_token_is_configured_at_all(app_client):
    monitor = FakeMonitor()
    client = app_client(monitor=monitor, admin_token="")
    resp = client.post("/api/admin/freshness/check", headers={"X-Admin-Token": "anything"})
    assert resp.status_code == 404
    assert monitor.check_now_calls == 0
