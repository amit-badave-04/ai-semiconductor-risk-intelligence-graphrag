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
    app.state.read_rate_limiter = guard.RateLimiter(120, 60)
    app.state.upload_slots = threading.BoundedSemaphore(1)
    app.state.uploads_ready = True     # routes.uploads_available(app.state) = uploads_enabled AND uploads_ready
    app.state.upload_jobs = jobs.JobRegistry()
    return TestClient(app)


def _auth(token=TOKEN):
    return {"X-Workspace-Token": token}


def _turnstile(token="tok"):
    return {"X-Turnstile-Token": token}


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
    return client.post(f"/api/workspace/{WS}/documents", headers={**_auth(), **_turnstile(), **(headers or {})},
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
    r = client.post(f"/api/workspace/{WS}/documents", headers={**_auth(), **_turnstile()})
    assert r.status_code == 400


def test_more_than_the_max_multipart_parts_is_a_400_not_a_hang(client):
    """Finding 3: the page sends at most file + document_id + title. A body with many more parts must be rejected
    cheaply, never let python-multipart's per-part Python state machine run unbounded on the event loop."""
    data = {f"field{i}": "x" for i in range(wr.MAX_MULTIPART_PARTS + 5)}
    r = _upload(client, data=data)
    assert r.status_code == 400
    assert r.headers["cache-control"] == "no-store"


def test_a_multipart_body_with_no_boundary_is_400_never_500(client):
    r = client.post(f"/api/workspace/{WS}/documents",
                    headers={**_auth(), **_turnstile(), "content-type": "multipart/form-data"},
                    content=b"garbage body with no boundary at all")
    assert r.status_code == 400
    assert r.json()["code"] == "malformed"
    assert r.headers["cache-control"] == "no-store"


def test_a_garbage_multipart_body_is_400_never_500(client):
    r = client.post(f"/api/workspace/{WS}/documents",
                    headers={**_auth(), **_turnstile(),
                             "content-type": "multipart/form-data; boundary=----abc"},
                    content=b"this is not a valid multipart body at all, no boundaries here")
    assert r.status_code == 400
    assert r.json()["code"] == "malformed"


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


# ---------------------------------------------------------------- slot-leak on the exception path (findings 5/17/23)


def test_the_upload_slot_is_released_when_reserve_daily_upload_raises(client, monkeypatch):
    def boom(driver, limit):
        raise RuntimeError("simulated transient Neo4j error")

    monkeypatch.setattr(store, "reserve_daily_upload", boom)
    with pytest.raises(RuntimeError):
        _upload(client)
    # The slot must be free again for the NEXT upload — not leaked forever (the bug: only `not reserved` released it).
    assert client.app.state.upload_slots.acquire(blocking=False) is True


def test_the_upload_slot_is_released_when_run_upload_job_raises_before_the_thread_starts(client, monkeypatch):
    def boom(app, **kw):
        raise RuntimeError("simulated thread-start failure")

    monkeypatch.setattr(jobs, "run_upload_job", boom)
    with pytest.raises(RuntimeError):
        _upload(client)
    assert client.app.state.upload_slots.acquire(blocking=False) is True


def test_a_second_upload_after_a_reserve_failure_is_not_permanently_busy(client, monkeypatch):
    """End-to-end version of the slot-leak finding: the FIRST request's failure must not turn every LATER upload
    into a permanent 429 busy until the process restarts."""
    calls = {"n": 0}

    def flaky(driver, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient Neo4j error")
        return True

    monkeypatch.setattr(store, "reserve_daily_upload", flaky)
    with pytest.raises(RuntimeError):
        _upload(client)
    r2 = _upload(client)
    assert r2.status_code == 202


# ---------------------------------------------------------------- GET /api/workspace/{ws}/jobs/{job_id} (SSE)
# Findings 1/14/22 (docs/v2/M4_PLAN.md 15.3): the SSE stream is now an ASYNC generator over JobRegistry's
# append-only, fan-out event log — never a queue.Queue, never a threadpool thread.


def test_a_live_job_streams_its_queued_events_then_closes(client):
    reg = client.app.state.upload_jobs
    reg.create(WS, "job1")
    reg.append(WS, "job1", {"job_id": "job1", "state": "parsing"})
    reg.append(WS, "job1", {"job_id": "job1", "state": "ready", "version": 1})
    r = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    assert r.status_code == 200
    compact = r.text.replace(" ", "")
    assert "event:job" in compact and '"state":"ready"' in compact


def test_every_watcher_sees_every_event_not_just_one_of_them(client):
    """The root of the SSE leak: a queue.Queue hands each item to exactly ONE consumer. Two SEPARATE requests on the
    same job must each see the full event sequence, including the terminal one."""
    reg = client.app.state.upload_jobs
    reg.create(WS, "job1")
    reg.append(WS, "job1", {"job_id": "job1", "state": "parsing"})
    reg.append(WS, "job1", {"job_id": "job1", "state": "ready", "version": 1})
    r1 = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    r2 = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    for r in (r1, r2):
        compact = r.text.replace(" ", "")
        assert '"state":"parsing"' in compact and '"state":"ready"' in compact


def test_a_4th_live_watcher_on_the_same_job_gets_429(client):
    """At most MAX_LIVE_WATCHERS_PER_JOB (3) live SSE connections per job (docs/v2/M4_PLAN.md 15.3)."""
    reg = client.app.state.upload_jobs
    reg.create(WS, "job1")
    assert reg.try_watch(WS, "job1") is True   # simulates 3 already-open connections on this job
    assert reg.try_watch(WS, "job1") is True
    assert reg.try_watch(WS, "job1") is True
    r = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    assert r.status_code == 429
    assert r.headers["cache-control"] == "no-store"


def test_a_watcher_slot_frees_up_once_its_connection_finishes(client):
    reg = client.app.state.upload_jobs
    reg.create(WS, "job1")
    reg.append(WS, "job1", {"job_id": "job1", "state": "ready", "version": 1})
    r = client.get(f"/api/workspace/{WS}/jobs/job1", headers=_auth())
    assert r.status_code == 200     # a terminal event: this watcher finishes and releases its slot immediately
    assert reg.try_watch(WS, "job1") is True
    assert reg.try_watch(WS, "job1") is True
    assert reg.try_watch(WS, "job1") is True


def test_the_job_event_stream_is_an_async_generator_never_a_threadpool_generator():
    """Structural regression guard for findings 1/14/22: sse_starlette runs a SYNC generator through
    ``starlette.concurrency.iterate_in_threadpool`` (the un-cancellable call that caused the leak) but iterates an
    ASYNC generator natively. This pins the fix at the type level, not just by behaviour."""
    import inspect

    assert inspect.isasyncgenfunction(wr._job_event_stream)


def test_a_finished_jobs_events_replay_from_the_persisted_snapshot(client, fake_repo):
    fake_repo.jobs[(WS, "job2")] = {"job_id": "job2", "state": "ready", "version": 1, "chunks": 2}
    r = client.get(f"/api/workspace/{WS}/jobs/job2", headers=_auth())
    assert r.status_code == 200
    assert '"state":"ready"' in r.text.replace(" ", "")


def test_workspace_get_routes_take_the_read_rate_window(client):
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    assert client.get(f"/api/workspace/{WS}", headers=_auth()).status_code == 200
    r = client.get(f"/api/workspace/{WS}", headers=_auth())
    assert r.status_code == 429


# ---------------------------------------------------------------- round-2 review S4/R2: the read-rate window must
# run BEFORE authentication, so a caller who never has a valid token (a wrong token or an unknown workspace) is
# rate-limited too, instead of paying an unbounded number of Neo4j lookups. The 404 must stay indistinguishable
# from the authenticated-but-unknown case either way.


@pytest.mark.parametrize("method,path,ws,token", [
    ("get", "/api/workspace/{ws}", WS, "wrong-token"),
    ("get", "/api/workspace/{ws}", "f" * 32, TOKEN),
    ("delete", "/api/workspace/{ws}", WS, "wrong-token"),
    ("get", "/api/workspace/{ws}/jobs/does-not-exist", WS, "wrong-token"),
    ("get", "/api/workspace/{ws}/changes?document_id=" + DOC + "&from=1&to=2", WS, "wrong-token"),
    ("get", "/api/workspace/{ws}/evidence/doc:0123456789ab:v1:0001", WS, "wrong-token"),
])
def test_a_wrong_token_or_unknown_workspace_request_is_rate_limited_not_just_the_authenticated_ones(
        client, fake_repo, method, path, ws, token):
    """S4/R2: before the fix, ``_require_read_rate`` ran AFTER ``_authenticate``, so a request that never
    authenticates (wrong token, unknown workspace) skipped the limiter entirely and paid an unbounded number of
    Neo4j lookups. With a 1-request window, the FIRST such request still gets 404 (the limiter allowed it), but the
    SECOND must be 429 from the limiter itself, before a second lookup ever runs."""
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    full_path = path.format(ws=ws)
    lookups_before = len(fake_repo.touched)
    r1 = getattr(client, method)(full_path, headers=_auth(token))
    assert r1.status_code == 404
    assert r1.json()["detail"] == routes.MSG_WORKSPACE_NOT_FOUND
    r2 = getattr(client, method)(full_path, headers=_auth(token))
    assert r2.status_code == 429
    assert r2.headers["cache-control"] == "no-store"
    # touch() only runs on a SUCCESSFUL auth, so this also proves no extra lookup slipped through on request 2.
    assert len(fake_repo.touched) == lookups_before


def test_delete_workspace_also_takes_the_read_rate_window(client):
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    assert client.delete(f"/api/workspace/{WS}", headers=_auth()).status_code == 204
    r = client.delete(f"/api/workspace/{WS}", headers=_auth())
    assert r.status_code == 429
    assert r.headers["cache-control"] == "no-store"


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


def test_changes_route_takes_the_read_rate_window(client):
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    assert client.get(f"/api/workspace/{WS}/changes?document_id={DOC}&from=1&to=2", headers=_auth()).status_code == 200
    r = client.get(f"/api/workspace/{WS}/changes?document_id={DOC}&from=1&to=2", headers=_auth())
    assert r.status_code == 429


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


def test_evidence_route_takes_the_read_rate_window(client):
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    path = f"/api/workspace/{WS}/evidence/doc:0123456789ab:v1:0001"
    assert client.get(path, headers=_auth()).status_code == 200
    assert client.get(path, headers=_auth()).status_code == 429


def test_an_upload_with_a_wrong_token_is_rate_limited_before_any_database_lookup(client, fake_repo, monkeypatch):
    """docs/v2/M4_PLAN.md 16: like every other workspace route, the upload takes the in-memory read-rate window BEFORE
    authentication, so a wrong-token flood never reaches the database; the per-address upload window stays in place."""
    lookups = []
    real = repo.authenticate           # the fixture installed the fake's bound method on the module
    monkeypatch.setattr(repo, "authenticate", lambda *a, **kw: (lookups.append(1), real(*a, **kw))[1])
    client.app.state.read_rate_limiter = guard.RateLimiter(1, 86400)
    wrong = {"X-Workspace-Token": "wrong", "X-Turnstile-Token": "t"}
    files = {"file": ("a.md", b"# a\nbody\n", "text/markdown")}
    assert client.post(f"/api/workspace/{WS}/documents", headers=wrong, files=files).status_code == 404
    assert lookups == [1]
    r = client.post(f"/api/workspace/{WS}/documents", headers=wrong, files=files)
    assert r.status_code == 429 and r.headers["cache-control"] == "no-store"
    assert lookups == [1]          # the second, rate-limited request never reached the workspace lookup
