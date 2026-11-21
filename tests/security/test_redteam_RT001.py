"""RT-001 (Medium, CWE-770): no request-body size cap (red-team finding, regression test).

FastAPI reads and buffers the WHOLE JSON body before pydantic's ``max_length`` or any route gate runs; uvicorn sets no
body limit (``drain.build_config``) and the only middleware ``main.create_app`` wires is the staging-only ``OriginAuth``.
So a multi-megabyte JSON body is read into memory, parsed, and handed to the route handler.

These tests build the app through ``main.create_app()`` (the production wiring) with the shared fakes of
``tests/serve_app_factory.py``: the cap is expected to land as ASGI middleware there, next to ``OriginAuth``. A fix that
wires the cap somewhere else (for example in ``drain.build_config``) needs these tests retargeted, not deleted.
``serve_app_factory.build_app`` is not used: it copies ``create_app``'s routers but none of its middleware.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # tests/: the shared fakes

from serve_app_factory import (  # noqa: E402
    FakeSettings,
    fake_lifespan,
    install_fake_state,
    patch_answer_paths,
    patch_data_paths,
)
from serve_state_fakes import FakeStateBackend, fresh_drain  # noqa: E402, F401 - fresh_drain: a fixture

from semigraph.serve import main  # noqa: E402

# The cap the fix must enforce on JSON API bodies. 64 KiB is this test's assumption: the fix chooses the real value (it
# must stay far above the largest legitimate ask, about 4,000 characters of question plus a Turnstile token). The
# multipart upload route has its own, larger limit (upload_max_bytes) and is out of scope here.
JSON_BODY_CAP_BYTES = 64 * 1024
OVERSIZE_BYTES = 2 * 1024 * 1024                                  # 2 MiB: 32x the assumed cap
QUESTION = "Which HBM suppliers does Nvidia depend on?"


def build_production_wired_app(monkeypatch: pytest.MonkeyPatch, settings: FakeSettings):
    """``main.create_app()`` over FakeSettings and the fake state; returns (app, backend)."""
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.router.lifespan_context = fake_lifespan(settings)
    backend = FakeStateBackend()
    install_fake_state(app, settings, backend=backend)
    patch_answer_paths(monkeypatch)
    patch_data_paths(monkeypatch)
    return app, backend


def test_an_oversize_ask_body_is_refused_413_before_the_handler_runs(monkeypatch, fresh_drain):
    settings = FakeSettings()
    app, backend = build_production_wired_app(monkeypatch, settings)
    # A valid ask plus an ignored extra field: AskRequest has no extra="forbid", so today the handler runs on all 2 MiB.
    body = {"question": QUESTION, "strategy": "hybrid", "padding": "x" * OVERSIZE_BYTES}
    with TestClient(app) as client:
        reply = client.post("/api/ask", json=body)
    assert reply.status_code == 413, f"a {OVERSIZE_BYTES}-byte ask body must be refused before parsing, got {reply.status_code}"
    assert not app.state.free_rate_limiter._buckets, "the oversize ask consumed a free window: the handler ran"
    assert backend.calls == [], "the oversize ask reached the state backend: the handler ran"


def test_an_oversize_ask_body_sent_chunked_without_content_length_is_refused_413(monkeypatch, fresh_drain):
    """A cap that trusts only the Content-Length header is bypassed by a chunked body: the bytes must be counted."""
    settings = FakeSettings()
    app, backend = build_production_wired_app(monkeypatch, settings)
    piece = b"x" * (64 * 1024)

    def chunks() -> Iterator[bytes]:
        yield b'{"question": "' + QUESTION.encode() + b'", "padding": "'
        for _ in range(OVERSIZE_BYTES // len(piece)):
            yield piece
        yield b'"}'

    with TestClient(app) as client:
        reply = client.post("/api/ask", content=chunks(), headers={"content-type": "application/json"})
    assert reply.status_code == 413, f"a chunked {OVERSIZE_BYTES}-byte ask body must be refused, got {reply.status_code}"
    assert backend.calls == [], "the oversize ask reached the state backend: the handler ran"


def test_an_oversize_workspace_create_body_is_refused_413(monkeypatch, fresh_drain):
    settings = FakeSettings()
    app, _ = build_production_wired_app(monkeypatch, settings)
    with TestClient(app) as client:
        reply = client.post("/api/workspace", json={"turnstile_token": "a" * OVERSIZE_BYTES})
    assert reply.status_code == 413, f"a {OVERSIZE_BYTES}-byte workspace body must be refused, got {reply.status_code}"


def test_a_normal_size_ask_is_not_refused_by_the_cap(monkeypatch, fresh_drain):
    """The control: a legitimate ask (far under the cap) still answers, so a fix that refuses everything fails here."""
    settings = FakeSettings()
    app, _ = build_production_wired_app(monkeypatch, settings)
    body = {"question": QUESTION, "strategy": "hybrid", "turnstile_token": "t" * 2048}
    assert len(str(body)) < JSON_BODY_CAP_BYTES
    with TestClient(app) as client:
        reply = client.post("/api/ask", json=body)
    assert reply.status_code == 200, reply.text
