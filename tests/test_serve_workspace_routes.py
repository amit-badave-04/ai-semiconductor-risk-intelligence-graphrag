"""HTTP-layer tests for the upload workspace routes (M4 Worker C, docs/v2/M4_PLAN.md 4.4, 5, 14.6, 14.7).

Neo4j (``uploads.repo``), the daily-upload ledger (``serve.store``) and the job runner (``uploads.jobs``) are all
monkeypatched at module level, in the style of ``tests/test_serve_api.py`` (fixtures NOT imported from it — that file
is forbidden to import from for this worker's routes, so this module builds its own small fakes for the same
policy-and-routing surface).
"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semigraph.serve import guard, routes, store, workspace_routes as wr
from semigraph.uploads import jobs, repo

WS = "a" * 32
TOKEN = "good-token"
DOC = "0123456789ab"


class FakeSettings:
    uploads_enabled = True
    turnstile_secret_key = ""
    turnstile_site_key = ""
    is_production = False
    client_ip_header = ""
    kill_switch = False
    upload_max_bytes = 15 * 1024 * 1024
    max_uploads_per_day = 40
    workspace_ttl_hours = 24
    upload_max_documents = 3
    upload_max_versions = 5
    upload_max_workspace_pages = 120
    upload_max_pages = 30
    upload_max_tokens = 16000
    upload_max_workspace_tokens = 48000


class FakeRepo:
    def __init__(self):
        self.workspaces = {WS: {"token": TOKEN}}
        self.documents: dict[str, dict] = {}     # document_id -> {"versions": {n: content_hash}}
        self.deleted: list[str] = []
        self.touched: list[str] = []
        self.jobs: dict[str, dict] = {}

    def authenticate(self, driver, ws, token):
        w = self.workspaces.get(ws)
        return bool(w) and w["token"] == token

    def touch(self, driver, ws):
        self.touched.append(ws)

    def create_workspace(self, driver, ttl_hours):
        return "b" * 32, "new-token", "2026-09-30T00:00:00+00:00"

    def get_workspace(self, driver, ws):
        if ws not in self.workspaces:
            return None
        return {"workspace_id": ws, "expires_at": "later", "documents": [], "usage": {"documents": 0, "pages": 0}}

    def delete_workspace(self, driver, ws):
        self.deleted.append(ws)
        return True

    def quota(self, driver, ws):
        versions_by_document = {d: len(v["versions"]) for d, v in self.documents.items()}
        return {"documents": len(self.documents), "versions_by_document": versions_by_document, "pages": 0,
               "embedded_tokens": 0}

    def latest_version(self, driver, ws, document_id):
        doc = self.documents.get(document_id)
        if not doc or not doc["versions"]:
            return None
        n = max(doc["versions"])
        return {"version": n, "content_hash": doc["versions"][n]}

    def get_changes(self, driver, ws, document_id, older, newer):
        return {"items_compared": True, "added": [], "removed": [], "changed": [], "unchanged_count": 0} \
            if document_id == DOC and older == 1 and newer == 2 else None

    def evidence(self, driver, ws, chunk_id):
        return {"id": chunk_id, "text": "hi", "document_id": DOC, "version": 1, "is_current": True,
               "status": "current", "valid_to": "later", "superseded_by_version": None, "title": "t"} \
            if chunk_id == "doc:0123456789ab:v1:0001" else None

    def get_job(self, driver, ws, job_id):
        return self.jobs.get((ws, job_id))


@pytest.fixture
def fake_repo(monkeypatch):
    fr = FakeRepo()
    for name in ("authenticate", "touch", "create_workspace", "get_workspace", "delete_workspace", "quota",
                "latest_version", "get_changes", "evidence", "get_job"):
        monkeypatch.setattr(repo, name, getattr(fr, name))
    return fr


@pytest.fixture
def fake_store(monkeypatch):
    calls = {"kill_switch": False, "reserved": True, "reserve_calls": 0}

    def kill_switch_on(driver, flag):
        return calls["kill_switch"]

    def reserve_daily_upload(driver, limit):
        calls["reserve_calls"] += 1
        return calls["reserved"]

    monkeypatch.setattr(store, "kill_switch_on", kill_switch_on)
    monkeypatch.setattr(store, "reserve_daily_upload", reserve_daily_upload)
    return calls


@pytest.fixture
def fake_jobs(monkeypatch):
    calls = []

    def run_upload_job(app, **kw):
        calls.append(kw)
        return "job-123"

    monkeypatch.setattr(jobs, "run_upload_job", run_upload_job)
    return calls


@pytest.fixture
def client(fake_repo, fake_store, fake_jobs):
    app = FastAPI()
    app.include_router(wr.router)
    app.state.settings = FakeSettings()
    app.state.driver = object()
    app.state.workspace_create_limiter = guard.RateLimiter(3, 86400)
    app.state.upload_limiter = guard.RateLimiter(10, 3600)
    app.state.upload_slots = threading.BoundedSemaphore(1)
    app.state.uploads_token_counter_ok = True
    app.state.upload_jobs = jobs.JobRegistry()
    return TestClient(app)


def _auth(token=TOKEN):
    return {"X-Workspace-Token": token}


# ---------------------------------------------------------------- UPLOADS_ENABLED gate


@pytest.mark.parametrize("method,path", [
    ("post", "/api/workspace"), ("get", f"/api/workspace/{WS}"), ("delete", f"/api/workspace/{WS}"),
    ("post", f"/api/workspace/{WS}/documents"), ("get", f"/api/workspace/{WS}/jobs/j1"),
    ("get", f"/api/workspace/{WS}/changes?document_id=d&from=1&to=2"),
    ("get", f"/api/workspace/{WS}/evidence/doc:0123456789ab:v1:0001"),
])
def test_every_route_answers_503_when_uploads_are_disabled(client, method, path):
    client.app.state.settings.uploads_enabled = False
    kwargs = {"json": {}} if path == "/api/workspace" else {}
    r = getattr(client, method)(path, headers=_auth(), **kwargs)
    assert r.status_code == 503
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- token gate: 404 for bad id, unknown ws, wrong token


@pytest.mark.parametrize("ws,token", [(WS, "wrong-token"), ("f" * 32, TOKEN), ("not-even-hex", TOKEN)])
def test_workspace_routes_404_on_bad_id_or_wrong_token_alike(client, ws, token):
    r = client.get(f"/api/workspace/{ws}", headers=_auth(token))
    assert r.status_code == 404 and r.json()["detail"] == routes.MSG_WORKSPACE_NOT_FOUND


def test_a_missing_token_header_is_also_404(client):
    r = client.get(f"/api/workspace/{WS}")
    assert r.status_code == 404


# ---------------------------------------------------------------- POST /api/workspace (create)


def test_create_workspace_succeeds_without_turnstile_configured_outside_production(client):
    r = client.post("/api/workspace", json={"turnstile_token": None})
    assert r.status_code == 201
    body = r.json()
    assert body["workspace_id"] == "b" * 32 and body["token"] == "new-token"
    assert body["limits"]["max_documents"] == 3
    assert r.headers["cache-control"] == "no-store"


def test_create_workspace_fails_closed_when_turnstile_unconfigured_in_production(client):
    client.app.state.settings.is_production = True
    r = client.post("/api/workspace", json={})
    assert r.status_code == 503


def test_create_workspace_rejects_a_bad_turnstile_token_when_configured(client, monkeypatch):
    client.app.state.settings.turnstile_secret_key = "secret"

    async def fail(*a, **kw):
        return False

    monkeypatch.setattr(guard, "verify_turnstile", fail)
    r = client.post("/api/workspace", json={"turnstile_token": "bad"})
    assert r.status_code == 403


def test_create_workspace_is_rate_limited_per_address(client):
    client.app.state.workspace_create_limiter = guard.RateLimiter(1, 86400)
    assert client.post("/api/workspace", json={}).status_code == 201
    assert client.post("/api/workspace", json={}).status_code == 429


# ---------------------------------------------------------------- GET/DELETE /api/workspace/{ws}


def test_get_workspace_returns_the_repo_shape(client):
    r = client.get(f"/api/workspace/{WS}", headers=_auth())
    assert r.status_code == 200 and r.json()["workspace_id"] == WS
    assert r.headers["cache-control"] == "no-store"


def test_delete_workspace_returns_204_and_calls_repo(client, fake_repo):
    r = client.delete(f"/api/workspace/{WS}", headers=_auth())
    assert r.status_code == 204
    assert fake_repo.deleted == [WS]


# ---------------------------------------------------------------- POST /api/workspace/{ws}/documents


def _upload(client, *, filename="a.txt", content=b"hello world", content_type="text/plain",
           data=None, headers=None):
    data = {} if data is None else data
    data.setdefault("turnstile_token", "tok")
    return client.post(f"/api/workspace/{WS}/documents", headers={**_auth(), **(headers or {})},
                       files={"file": (filename, content, content_type)}, data=data)


def test_a_new_document_upload_is_accepted_and_starts_a_job(client, fake_jobs):
    r = _upload(client)
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"] == "job-123" and body["version"] == 1
    assert fake_jobs[0]["kind"] == "txt" and fake_jobs[0]["workspace_id"] == WS
    assert r.headers["cache-control"] == "no-store"


def test_kill_switch_blocks_uploads_with_503(client, fake_store):
    fake_store["kill_switch"] = True
    r = _upload(client)
    assert r.status_code == 503


def test_upload_without_a_file_field_is_400(client):
    r = client.post(f"/api/workspace/{WS}/documents", headers=_auth(), data={"turnstile_token": "tok"})
    assert r.status_code == 400


def test_content_length_over_the_cap_is_rejected_before_reading(client):
    client.app.state.settings.upload_max_bytes = 10
    r = _upload(client, content=b"x" * 50000)
    assert r.status_code == 413 and r.json()["code"] == "too_large"


def test_an_unsupported_file_type_is_rejected_by_the_gate(client):
    r = _upload(client, filename="a.bin", content=b"\x00\x01binary junk", content_type="application/octet-stream")
    assert r.status_code == 415
    assert r.json()["code"] == "unsupported_type"


def test_an_unknown_client_supplied_document_id_is_404(client):
    r = _upload(client, data={"document_id": "ffffffffffff", "turnstile_token": "tok"})
    assert r.status_code == 404


def test_identical_content_to_the_latest_version_is_reported_unchanged_with_no_job(client, fake_repo, fake_jobs):
    fake_repo.documents[DOC] = {"versions": {1: __import__("hashlib").sha256(b"hello world").hexdigest()}}
    r = _upload(client, data={"document_id": DOC, "turnstile_token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"unchanged": True, "document_id": DOC, "version": 1}
    assert fake_jobs == []


def test_max_documents_quota_is_a_429_with_a_code(client, fake_repo):
    fake_repo.documents = {f"doc{i:09d}": {"versions": {1: "h"}} for i in range(3)}
    r = _upload(client)
    assert r.status_code == 429 and r.json()["code"] == "max_documents"


def test_max_versions_quota_is_a_429_with_a_code(client, fake_repo):
    fake_repo.documents[DOC] = {"versions": {i: f"h{i}" for i in range(1, 6)}}
    r = _upload(client, data={"document_id": DOC, "turnstile_token": "tok"})
    assert r.status_code == 429 and r.json()["code"] == "max_versions"


def test_a_busy_slot_answers_429_busy_without_starting_a_job(client, fake_jobs):
    client.app.state.upload_slots.acquire()     # simulate another upload already in flight
    r = _upload(client)
    assert r.status_code == 429 and r.json()["code"] == "busy"
    assert fake_jobs == []


def test_the_daily_upload_limit_releases_the_slot_it_acquired(client, fake_store, fake_jobs):
    fake_store["reserved"] = False
    r = _upload(client)
    assert r.status_code == 429 and r.json()["code"] == "daily_limit"
    assert fake_jobs == []
    assert client.app.state.upload_slots.acquire(blocking=False) is True   # the slot was released, not leaked


def test_a_bad_turnstile_token_on_upload_is_403_and_never_reaches_the_gate_or_the_job(client, fake_jobs, monkeypatch):
    client.app.state.settings.turnstile_secret_key = "secret"

    async def fail(*a, **kw):
        return False

    monkeypatch.setattr(guard, "verify_turnstile", fail)
    r = _upload(client)
    assert r.status_code == 403
    assert fake_jobs == []


def test_junk_files_never_spend_the_daily_upload_budget(client, fake_store):
    r = _upload(client, filename="a.bin", content=b"\x00\x01binary", content_type="application/octet-stream")
    assert r.status_code == 415
    assert fake_store["reserve_calls"] == 0


# ---------------------------------------------------------------- GET /api/workspace/{ws}/jobs/{job_id} (SSE)


def test_a_live_job_streams_its_queued_events_then_closes(client):
    reg = client.app.state.upload_jobs
    q = reg.create(WS, "job1")
    q.put({"job_id": "job1", "state": "parsing"})
    q.put({"job_id": "job1", "state": "ready", "version": 1})
    r = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    assert r.status_code == 200
    compact = r.text.replace(" ", "")
    assert "event:job" in compact and '"state":"ready"' in compact


def test_a_finished_jobs_events_replay_from_the_persisted_snapshot(client, fake_repo):
    fake_repo.jobs[(WS, "job2")] = {"job_id": "job2", "state": "ready", "version": 1, "chunks": 2}
    r = client.get(f"/api/workspace/{WS}/jobs/job2", headers=_auth())
    assert r.status_code == 200
    assert '"state":"ready"' in r.text.replace(" ", "")


def test_an_unknown_job_id_is_404(client):
    r = client.get(f"/api/workspace/{WS}/jobs/does-not-exist", headers=_auth())
    assert r.status_code == 404


# ---------------------------------------------------------------- GET /api/workspace/{ws}/changes


def test_changes_returns_the_repo_report(client):
    r = client.get(f"/api/workspace/{WS}/changes?document_id={DOC}&from=1&to=2", headers=_auth())
    assert r.status_code == 200 and r.json()["items_compared"] is True


def test_changes_404_for_an_unknown_version_pair(client):
    r = client.get(f"/api/workspace/{WS}/changes?document_id={DOC}&from=9&to=10", headers=_auth())
    assert r.status_code == 404


# ---------------------------------------------------------------- GET /api/workspace/{ws}/evidence/{doc_id}


def test_evidence_returns_the_repo_row(client):
    r = client.get(f"/api/workspace/{WS}/evidence/doc:0123456789ab:v1:0001", headers=_auth())
    assert r.status_code == 200 and r.json()["id"] == "doc:0123456789ab:v1:0001"


def test_evidence_404_for_a_malformed_id(client):
    r = client.get(f"/api/workspace/{WS}/evidence/not-a-doc-id", headers=_auth())
    assert r.status_code == 404


def test_evidence_404_for_an_unknown_id_of_the_right_shape(client):
    r = client.get(f"/api/workspace/{WS}/evidence/doc:ffffffffffff:v1:0001", headers=_auth())
    assert r.status_code == 404
