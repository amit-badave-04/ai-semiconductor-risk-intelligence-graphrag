"""``scripts/freshness_heartbeat.py`` (M4, docs/v2/M4_PLAN.md 4.1, 14.3): the exact logic
``.github/workflows/freshness-heartbeat.yml`` runs. Imported dynamically (scripts/ is not a package), the same
pattern ``tests/test_serve_secrets_keys.py`` uses for ``scripts/push_fly_secrets.py``.
"""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_heartbeat():
    spec = importlib.util.spec_from_file_location("freshness_heartbeat", ROOT / "scripts" / "freshness_heartbeat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


heartbeat = _load_heartbeat()


def _body(status="ok", configured=True, **extra):
    return {"status": status, "configured": configured, "pending_count": 0, "error": None, **extra}


# ---------------------------------------------------------------------- evaluate(): pure logic

@pytest.mark.parametrize("status", ["ok", "never"], ids=["ok", "never"])
def test_evaluate_exits_zero_for_a_healthy_configured_monitor(status):
    assert heartbeat.evaluate(_body(status=status, configured=True)) == 0


@pytest.mark.parametrize("status", ["error", "stale", "unconfigured"], ids=["error", "stale", "unconfigured"])
def test_evaluate_exits_nonzero_for_an_unhealthy_status(status):
    assert heartbeat.evaluate(_body(status=status, configured=True)) != 0


def test_evaluate_exits_nonzero_when_unconfigured_even_if_status_looks_healthy():
    # status is always "unconfigured" when configured is False (serve.monitor._status_for), but the heartbeat
    # checks BOTH fields explicitly rather than trusting status alone.
    assert heartbeat.evaluate(_body(status="ok", configured=False)) != 0


def test_evaluate_exits_nonzero_for_a_missing_or_unknown_status():
    assert heartbeat.evaluate({"configured": True}) != 0
    assert heartbeat.evaluate(_body(status="something-new", configured=True)) != 0


# ---------------------------------------------------------------------- main(): --body-file (no network)

def test_main_with_a_body_file_exits_zero_for_ok(tmp_path):
    f = tmp_path / "ok.json"
    f.write_text(json.dumps(_body(status="ok")), encoding="utf-8")
    assert heartbeat.main(["https://example.invalid/api/freshness", "--body-file", str(f)]) == 0


@pytest.mark.parametrize("status", ["stale", "error", "unconfigured"], ids=["stale", "error", "unconfigured"])
def test_main_with_a_recorded_bad_body_file_exits_nonzero(tmp_path, status):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(_body(status=status)), encoding="utf-8")
    assert heartbeat.main(["https://example.invalid/api/freshness", "--body-file", str(f)]) != 0


# ---------------------------------------------------------------------- fetch_status() / main(): network path

class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_status_returns_the_parsed_body_on_a_normal_200(monkeypatch):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(200, _body()))
    assert heartbeat.fetch_status("https://example.invalid/api/freshness") == _body()


def test_fetch_status_raises_unreachable_on_a_5xx_response(monkeypatch):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(503, {}))
    with pytest.raises(heartbeat.Unreachable):
        heartbeat.fetch_status("https://example.invalid/api/freshness")


def test_fetch_status_raises_unreachable_on_a_5xx_http_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b""))

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    with pytest.raises(heartbeat.Unreachable):
        heartbeat.fetch_status("https://example.invalid/api/freshness")


def test_fetch_status_propagates_a_4xx_as_a_real_failure_not_a_warning(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        heartbeat.fetch_status("https://example.invalid/api/freshness")


def test_fetch_status_raises_unreachable_on_a_connection_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    with pytest.raises(heartbeat.Unreachable):
        heartbeat.fetch_status("https://example.invalid/api/freshness")


def test_fetch_status_raises_unreachable_on_a_connection_dropped_mid_read(monkeypatch):
    """A Fly wake can drop the connection after urlopen() already succeeded — a raw OSError from .read(), never
    wrapped in a URLError."""
    class DroppedResponse:
        status = 200

        def read(self):
            raise ConnectionResetError("connection reset by peer")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", lambda req, timeout=None: DroppedResponse())
    with pytest.raises(heartbeat.Unreachable):
        heartbeat.fetch_status("https://example.invalid/api/freshness")


def test_main_exits_zero_with_a_warning_when_the_app_is_unreachable(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    assert heartbeat.main(["https://example.invalid/api/freshness"]) == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_exits_zero_with_a_warning_on_a_5xx(monkeypatch, capsys):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(500, {}))
    assert heartbeat.main(["https://example.invalid/api/freshness"]) == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_over_the_network_still_applies_the_normal_pass_fail_logic(monkeypatch):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(200, _body(status="error")))
    assert heartbeat.main(["https://example.invalid/api/freshness"]) != 0


# ---------------------------------------------------------------------- the workflow file

def test_the_workflow_yaml_parses_and_has_the_expected_triggers():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "freshness-heartbeat.yml").read_text(encoding="utf-8"))
    # PyYAML 1.1 parses the bare key `on` as the boolean True; both spellings are checked for robustness.
    triggers = doc.get("on", doc.get(True))
    assert "workflow_dispatch" in triggers
    assert triggers["schedule"][0]["cron"] == "0 */6 * * *"


def test_the_workflow_runs_the_heartbeat_script_against_the_public_endpoint():
    text = (ROOT / ".github" / "workflows" / "freshness-heartbeat.yml").read_text(encoding="utf-8")
    assert "scripts/freshness_heartbeat.py" in text
    assert "/api/freshness" in text


def test_the_workflow_carries_no_secret_and_needs_none():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "freshness-heartbeat.yml").read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        assert "secrets" not in job and "env" not in job
        for step in job.get("steps", []):
            assert "secrets." not in str(step)
