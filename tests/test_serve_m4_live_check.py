"""``scripts/m4_live_check.py`` (M4 gate G9): the pure evaluator, and ``observe`` against an in-process fake service
(httpx.MockTransport). Nothing here touches the network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("m4_live_check", ROOT / "scripts" / "m4_live_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _load()
IDS = ["ex-01", "ex-02", "ex-03"]


def _passing_obs() -> dict:
    return {"healthz": 200, "index": 200, "index_site_key_filled": True,
            "stats": {"status": 200, "uploads_enabled": True, "node_labels": ["Company", "Filing"],
                      "freshness_status": "ok"},
            "freshness": {"status": 200, "enabled": True, "configured": True},
            "examples_status": 200, "example_ids": sorted(IDS), "dossier": 200, "risk_changes": 200,
            "public_evidence_doc_id": 404, "workspace_create_no_token": 403, "agent_with_workspace": 400}


def test_a_fully_passing_observation_set_has_no_failures():
    assert live.evaluate_g9(_passing_obs(), IDS) == []


def test_an_empty_observation_set_fails_every_check():
    assert len(live.evaluate_g9({}, IDS)) == len(live._g9_checks({}, IDS))


@pytest.mark.parametrize("mutate,expected_fragment", [
    (lambda o: o.update(healthz=503), "/healthz"),
    (lambda o: o.update(index_site_key_filled=False), "Turnstile site key"),
    (lambda o: o["stats"].update(uploads_enabled=False), "uploads enabled"),
    (lambda o: o["stats"].update(node_labels=["Company", "UserChunk"]), "User* labels"),
    (lambda o: o["stats"].update(freshness_status="unconfigured"), "freshness block"),
    (lambda o: o["freshness"].update(configured=False), "/api/freshness"),
    (lambda o: o.update(example_ids=["ex-01", "ex-02"]), "/api/examples"),
    (lambda o: o.update(dossier=500), "dossier"),
    (lambda o: o.update(risk_changes=404), "risk-changes"),
    (lambda o: o.update(public_evidence_doc_id=200), "public evidence"),
    (lambda o: o.update(workspace_create_no_token=200), "Turnstile token"),
    (lambda o: o.update(agent_with_workspace=403), "strategy=agent"),
], ids=["healthz", "site-key", "uploads", "user-labels", "freshness-summary", "freshness-route", "examples",
        "dossier", "risk-changes", "public-doc-id", "workspace-403", "agent-400"])
def test_each_check_fails_alone_when_its_observation_is_wrong(mutate, expected_fragment):
    obs = _passing_obs()
    mutate(obs)
    failures = live.evaluate_g9(obs, IDS)
    assert len(failures) == 1 and expected_fragment in failures[0]


def test_no_expected_ids_is_a_failure_never_a_vacuous_pass():
    obs = _passing_obs()
    obs["example_ids"] = []
    assert any("/api/examples" in f for f in live.evaluate_g9(obs, []))


def _fake_service(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    if method == "GET" and path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if method == "GET" and path == "/":
        return httpx.Response(200, text="<html>data-sitekey='0x4AAA'</html>")
    if method == "GET" and path == "/api/stats":
        return httpx.Response(200, json={"graph": {"nodes": {"Company": 26, "Filing": 74}}, "uploads_enabled": True,
                                         "freshness": {"status": "ok", "checked_at": None, "pending_count": 0}})
    if method == "GET" and path == "/api/freshness":
        return httpx.Response(200, json={"enabled": True, "configured": True, "status": "ok", "pending_count": 0})
    if method == "GET" and path == "/api/examples":
        return httpx.Response(200, json={"source": "x", "examples": [{"id": i, "question": "q", "type": "t"}
                                                                      for i in reversed(IDS)]})
    if method == "GET" and path.startswith("/api/company/NVDA/"):
        return httpx.Response(200, json={})
    if method == "GET" and path.startswith("/api/evidence/doc:"):
        return httpx.Response(404, json={"detail": "not a public evidence id"})
    if method == "POST" and path == "/api/workspace":
        return httpx.Response(403, json={"detail": "bot check"})
    if method == "POST" and path == "/api/ask":
        body = json.loads(request.content)
        assert body["strategy"] == "agent" and body["workspace_id"]
        return httpx.Response(400, json={"detail": "strategy=agent is not available with a workspace"})
    return httpx.Response(599)


def test_observe_against_a_fake_service_passes_g9():
    with httpx.Client(transport=httpx.MockTransport(_fake_service)) as client:
        obs = live.observe(client, "https://example.test")
    assert live.evaluate_g9(obs, IDS) == []
    assert obs["stats"]["node_labels"] == ["Company", "Filing"]


def test_main_writes_the_artifact_and_exits_0_on_a_pass(monkeypatch, tmp_path):
    expected = tmp_path / "pre_examples.json"
    expected.write_text(json.dumps({"examples": [{"id": i} for i in IDS]}), encoding="utf-8")
    real_client = httpx.Client
    monkeypatch.setattr(live.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(_fake_service)))
    monkeypatch.setattr(live, "ARTIFACT_PATH", tmp_path / "m4_live_gates.json")
    assert live.main(["--base-url", "https://example.test/", "--expect-examples", str(expected)]) == 0
    written = json.loads((tmp_path / "m4_live_gates.json").read_text(encoding="utf-8"))
    assert written["passed"] is True and written["failures"] == [] and written["expected_example_count"] == 3
