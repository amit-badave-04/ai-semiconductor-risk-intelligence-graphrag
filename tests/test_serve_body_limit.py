"""BodyLimit deadline and size cases, driven at the ASGI level (RT-001/RT-002, spec F1). No sockets."""

import asyncio
import time

from semigraph.serve.asgi_middleware import BodyLimit

UPLOAD = "/api/workspace/abc123/documents"


async def _ok_app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(len(body)).encode()})


async def _drive(app, method, path, chunks, *, delay=0.0, headers=()):
    """Feeds ``chunks`` with ``delay`` seconds before each; returns (status, response headers, elapsed seconds)."""
    queue = list(chunks)
    sent: list[dict] = []

    async def receive():
        if not queue:
            await asyncio.sleep(3600)
        await asyncio.sleep(delay)
        piece = queue.pop(0)
        return {"type": "http.request", "body": piece, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": list(headers)}
    started = time.monotonic()
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"]), time.monotonic() - started


def _limit(**kwargs):
    return BodyLimit(_ok_app, upload_max_bytes=1000, **kwargs)


def test_a_slow_json_body_is_answered_408_and_closed_at_the_deadline():
    app = _limit(json_deadline_s=0.2)
    status, headers, elapsed = asyncio.run(_drive(app, "POST", "/api/ask", [b"a", b"b", b"c"], delay=0.1))
    assert status == 408 and headers[b"connection"] == b"close"
    assert elapsed < 0.2 + 0.3


def test_an_honest_upload_at_the_slowest_rate_completes_past_the_json_deadline():
    app = _limit(json_deadline_s=0.1, upload_deadline_s=2.0)
    status, _, _ = asyncio.run(_drive(app, "POST", UPLOAD, [b"x" * 100] * 4, delay=0.15))
    assert status == 200


def test_a_stalled_upload_is_answered_408_at_the_upload_deadline():
    app = _limit(upload_deadline_s=0.2)
    status, _, elapsed = asyncio.run(_drive(app, "POST", UPLOAD, [b"x"] * 3, delay=0.15))
    assert status == 408 and elapsed < 0.2 + 0.3


def test_json_over_cap_is_413_but_the_same_size_upload_is_allowed():
    app = _limit(json_max_bytes=100)
    assert asyncio.run(_drive(app, "POST", "/api/ask", [b"x" * 200]))[0] == 413
    assert asyncio.run(_drive(app, "POST", UPLOAD, [b"x" * 200]))[0] == 200


def test_an_upload_over_its_cap_is_413_declared_and_counted():
    app = _limit()
    declared = [(b"content-length", b"999999")]
    assert asyncio.run(_drive(app, "POST", UPLOAD, [b"x"], headers=declared))[0] == 413
    assert asyncio.run(_drive(app, "POST", UPLOAD, [b"x" * 5000] * 2))[0] == 413     # cap is 1000 + 8192 slack


def test_get_is_untouched_by_the_caps():
    app = _limit(json_max_bytes=1)
    assert asyncio.run(_drive(app, "GET", "/api/ask", [b"xxxx"]))[0] == 200


# Round-3 review (RT-002 bypass): the limits follow the request's framing, not its method. A GET, HEAD or OPTIONS that
# declares a body gets the same cap, deadline and connection close as a POST.
def test_a_get_declaring_an_oversize_body_gets_413_with_connection_close():
    app = _limit(json_max_bytes=100)
    status, headers, _ = asyncio.run(_drive(app, "GET", "/healthz", [b"x"], headers=[(b"content-length", b"1000000000")]))
    assert status == 413 and headers.get(b"connection") == b"close"


def test_a_slow_chunked_options_body_is_answered_408_and_closed():
    app = _limit(json_deadline_s=0.2)
    status, headers, elapsed = asyncio.run(_drive(app, "OPTIONS", "/api/ask", [b"a", b"b", b"c"], delay=0.1,
                                                  headers=[(b"transfer-encoding", b"chunked")]))
    assert status == 408 and headers.get(b"connection") == b"close" and elapsed < 0.2 + 0.3


def test_waiting_for_a_disconnect_after_the_body_is_not_cut_by_the_deadline():
    """SSE: the response streams while the app waits on receive() for a disconnect; the body deadline must not apply."""
    seen = {}

    async def sse_app(scope, receive, send):
        await receive()                                    # the whole body
        await send({"type": "http.response.start", "status": 200, "headers": []})
        listener = asyncio.ensure_future(receive())        # Starlette's disconnect listener
        await asyncio.sleep(0.4)                           # the stream outlives the 0.1 s body deadline
        seen["listener_done"] = listener.done()
        listener.cancel()
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def run():
        async def receive():
            await asyncio.sleep(3600)

        first = [{"type": "http.request", "body": b"{}", "more_body": False}]

        async def feed():
            return first.pop(0) if first else await receive()

        async def send(message):
            sent.append(message)

        app = BodyLimit(sse_app, upload_max_bytes=1000, json_deadline_s=0.1)
        await app({"type": "http", "method": "POST", "path": "/api/ask", "headers": []}, feed, send)

    asyncio.run(run())
    assert seen["listener_done"] is False
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [200]


def test_the_upload_deadline_allows_15_mib_at_64_kib_per_second():
    from semigraph.serve.asgi_middleware import UPLOAD_BODY_DEADLINE_S
    assert 15 * 1024 * 1024 / (64 * 1024) < UPLOAD_BODY_DEADLINE_S


# --- F1: a response that starts before the request body is complete must close the connection (RT-002 bypass) -------

async def _early_app(scope, receive, send):
    """Answers 401 without reading the body (any early app response: 404, 401, a handler that never reads)."""
    await send({"type": "http.response.start", "status": 401, "headers": [(b"x-a", b"b")]})
    await send({"type": "http.response.body", "body": b"no"})


def test_a_declared_oversize_post_gets_413_with_connection_close():
    app = _limit(json_max_bytes=100)
    status, headers, _ = asyncio.run(_drive(app, "POST", "/api/ask", [b"x"], headers=[(b"content-length", b"5000")]))
    assert status == 413 and headers.get(b"connection") == b"close"


def test_a_counted_oversize_body_gets_413_with_connection_close():
    app = _limit(json_max_bytes=10)
    status, headers, _ = asyncio.run(_drive(app, "POST", "/api/ask", [b"x" * 8, b"y" * 8]))
    assert status == 413 and headers.get(b"connection") == b"close"


def test_an_app_response_before_the_body_is_read_carries_connection_close():
    app = BodyLimit(_early_app, upload_max_bytes=1000)
    for method in ("POST", "PUT", "DELETE"):
        status, headers, _ = asyncio.run(_drive(app, method, "/api/x", [b"abc"], headers=[(b"content-length", b"3")]))
        assert status == 401 and headers.get(b"connection") == b"close", method
    status, headers, _ = asyncio.run(_drive(app, "POST", "/api/x", [b"abc"], headers=[(b"transfer-encoding", b"chunked")]))
    assert headers.get(b"connection") == b"close"


def test_a_bodiless_early_response_is_not_closed():
    app = BodyLimit(_early_app, upload_max_bytes=1000)
    _, headers, _ = asyncio.run(_drive(app, "POST", "/api/x", [b""], headers=[(b"content-length", b"0")]))
    assert b"connection" not in headers


def test_a_fully_read_request_keeps_keep_alive():
    app = _limit()
    status, headers, _ = asyncio.run(_drive(app, "POST", "/api/ask", [b"abc"], headers=[(b"content-length", b"3")]))
    assert status == 200 and b"connection" not in headers
