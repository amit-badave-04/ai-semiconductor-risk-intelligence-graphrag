"""Red-team PoC M2RT-08: concurrent upload bodies are not bounded in memory (spec m2-search.md S2).

``upload_document`` (workspace_routes.py:455) reads and parses the WHOLE multipart body (``_read_and_gate_body`` ->
``_read_multipart``: a bytearray :295, then ``bytes(body)`` :298, then python-multipart's in-memory file
(``MAX_MEMORY_FILE_SIZE`` = cap, :291), then ``file_object.read()`` :287) BEFORE ``_finalize_upload`` takes the upload slot
(:405) and asks the memory guard (:412). The per-client window (:449) is checked once per request, before the body read, and
allows 10 per hour. So one client holding a workspace
token sends K uploads at once and ALL K bodies sit in memory and are parsed together; every one but the first is then answered
429 busy. Spec S2 requires "concurrent body memory is otherwise bounded" with ``test_concurrent_large_uploads_bounded``; the test
does not exist.

Doubles only (the fakes of tests/test_serve_workspace_routes.py): no network, no DB, no paid call. Bodies are small here
(upload_max_bytes 64 KiB) because the property under test is HOW MANY bodies are buffered at once, not their size.
Expected today: FAILS (peak bodies buffered and parsed at once == K, the number of concurrent requests).
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/: the shared fakes

import test_serve_workspace_routes as base  # noqa: E402
from serve_state_fakes import FakeStateBackend, fresh_drain, install_state  # noqa: E402, F401 - fresh_drain: a fixture

from semigraph.serve import guard, store, workspace_routes as wr  # noqa: E402
from semigraph.uploads import jobs, repo  # noqa: E402

K = 6                                    # concurrent uploads from ONE client: inside its 10-per-hour window
MAX_BYTES = 64 * 1024
WAIT_S = 3.0


def _build_client(monkeypatch):
    fr = base.FakeRepo()
    for name in ("authenticate", "touch", "quota", "latest_version", "get_job"):
        monkeypatch.setattr(repo, name, getattr(fr, name))
    monkeypatch.setattr(store, "reserve_daily_upload", lambda driver, limit, day=None: True)
    monkeypatch.setattr(jobs, "run_upload_job", lambda app, **kw: "job-1")
    app = FastAPI(lifespan=base._lifespan)
    app.include_router(wr.router)
    settings = base.FakeSettings()
    settings.upload_max_bytes = MAX_BYTES
    app.state.settings = settings
    app.state.driver = object()
    app.state.workspace_create_limiter = guard.RateLimiter(3, 86400)
    app.state.upload_limiter = guard.RateLimiter(10, 3600)         # production default: uploads_per_hour = 10
    app.state.read_rate_limiter = guard.RateLimiter(120, 60)
    app.state.upload_slots = threading.BoundedSemaphore(1)          # one upload job at a time, as in production
    app.state.uploads_ready = True
    app.state.upload_jobs = jobs.JobRegistry()
    install_state(app, backend=FakeStateBackend())
    return app


def test_concurrent_large_uploads_bounded(monkeypatch, fresh_drain):  # noqa: F811
    lock = threading.Lock()
    state = {"inside": 0, "peak": 0, "entered": 0}
    all_in = threading.Event()
    original = wr._parse_multipart_body

    def counting_parse(parser, body: bytes) -> None:
        # Runs in a threadpool worker with this request's WHOLE body already in memory (``body``).
        with lock:
            state["min_body"] = min(state.get("min_body", len(body)), len(body))
            state["inside"] += 1
            state["entered"] += 1
            state["peak"] = max(state["peak"], state["inside"])
            if state["entered"] >= K:
                all_in.set()
        try:
            all_in.wait(WAIT_S)          # hold this body in memory until the others arrive (or a bounded fix keeps them out)
            original(parser, body)
        finally:
            with lock:
                state["inside"] -= 1

    monkeypatch.setattr(wr, "_parse_multipart_body", counting_parse)
    app = _build_client(monkeypatch)
    payload = b"plain text line for the upload gate\n" * ((MAX_BYTES - 1024) // 36)

    with TestClient(app) as client:
        def one(i: int):
            return client.post(f"/api/workspace/{base.WS}/documents",
                               headers={"X-Workspace-Token": base.TOKEN, "X-Turnstile-Token": "tok"},
                               files={"file": (f"doc{i}.txt", payload + str(i).encode(), "text/plain")})

        with ThreadPoolExecutor(max_workers=K) as pool:
            responses = list(pool.map(one, range(K), timeout=60))

    codes = sorted(r.status_code for r in responses)
    # Right reason: every request passed the workspace token, Turnstile and the per-client window. A fix may refuse the
    # extra bodies with 429 busy or 503 (budget or memory guard), so only the gates BEFORE the body read are excluded here.
    assert not any(r.status_code in (403, 404) for r in responses), codes
    assert not any(r.status_code == 429 and r.json().get("detail") == wr.MSG_UPLOAD_RATE for r in responses), codes
    assert codes.count(202) == 1, codes                 # one slot: one job
    assert state["min_body"] >= MAX_BYTES - 4096, state  # whole bodies (not chunks) were in memory together

    # Spec S2: concurrent body memory is bounded by a fixed process-wide limit, not by the number of requests in flight.
    assert state["peak"] < K, (f"all {K} concurrent upload bodies were buffered and parsed at once (peak {state['peak']}); "
                               "the slot and memory guard run only after the full body read")
