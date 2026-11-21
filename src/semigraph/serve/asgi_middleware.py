"""OriginAuth: the secret-header gate of the staging API (M5a I5, docs/v2/M5_DECISIONS.md 2.1 "Staging rules").

The staging apps are reachable from the internet only with a secret origin header; the load generators send it, nothing
else does. This is a pure ASGI middleware (no Starlette base class, so it runs before anything else and adds no per-request
task), stdlib only (the serve-shipped CI environment imports it with nothing extra).

* The check runs BEFORE routing and before the request body is read: a wrong request costs a 403, never a handler.
* The header must be present exactly once. A duplicate is ambiguous (a proxy appended one), so it is refused too.
* The comparison is ``hmac.compare_digest`` on bytes: no timing difference between a near miss and a far one. A value that
  is not text (any bytes) cannot raise.
* ``GET`` and ``HEAD`` of exactly ``/healthz`` are exempt (Fly's health check carries no secret). Not a prefix, not another
  method, not another case.
* ``lifespan`` passes straight through; a websocket without the header is closed with 1008 before it is accepted; any other
  scope type is refused (the ASGI recommendation for a scope an app does not understand).
* The presented value is never logged, echoed or kept. The secret is held as bytes and is not in the ``repr``.

It is wired into ``main.create_app``, and only when ``ORIGIN_AUTH_SECRET`` is set (the secret travels wrapped in
``Concealed``, so Starlette's printout of the middleware arguments does not show it). The production validators refuse the
setting in production (``config.py``): the live site would answer 403 to every browser. Refusing a short secret at
construction is a second line of defence for a deployment that skips those validators.
"""

import asyncio
import hmac
import time
from collections.abc import Callable, Iterable
from typing import Any

MIN_SECRET_BYTES = 32
DEFAULT_HEADER = "x-origin-auth"
DEFAULT_EXEMPT = ("/healthz",)
EXEMPT_METHODS = frozenset({"GET", "HEAD"})
WEBSOCKET_POLICY_VIOLATION = 1008

_BODY = b'{"detail":"Forbidden"}'
_FORBIDDEN_HEADERS = [(b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode()),
                      (b"cache-control", b"no-store")]

Scope = dict[str, Any]
Receive = Callable[[], Any]
Send = Callable[[dict], Any]


class OriginAuth:
    """``OriginAuth(app, secret, header="x-origin-auth", exempt=("/healthz",))``; ``app.add_middleware(OriginAuth, secret=...)``
    is the Starlette spelling. ``rejected`` counts the refusals (a plain int: the event loop is single-threaded)."""

    def __init__(self, app: Callable[..., Any], secret: str, *, header: str = DEFAULT_HEADER,
                 exempt: Iterable[str] = DEFAULT_EXEMPT):
        if not isinstance(secret, str):
            raise ValueError("ORIGIN_AUTH_SECRET must be text")
        key = secret.encode("utf-8")
        if len(key) < MIN_SECRET_BYTES:
            raise ValueError(f"ORIGIN_AUTH_SECRET must be at least {MIN_SECRET_BYTES} bytes")
        self._app = app
        self._secret = key
        self._header = header.strip().lower().encode("latin-1")
        self._exempt = frozenset(exempt)
        self.rejected = 0

    def __repr__(self) -> str:
        return f"OriginAuth(header={self._header.decode()!r}, exempt={sorted(self._exempt)!r})"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._app(scope, receive, send)
        elif kind in ("http", "websocket"):
            if self._allowed(scope):
                await self._app(scope, receive, send)
            else:
                await self._refuse(kind, send)
        else:
            raise RuntimeError(f"OriginAuth does not handle a {kind!r} scope")

    def _allowed(self, scope: Scope) -> bool:
        if (scope["type"] == "http" and scope.get("method") in EXEMPT_METHODS and scope.get("path") in self._exempt):
            return True
        presented = [value for name, value in scope.get("headers") or () if name == self._header]
        if len(presented) != 1:
            return False
        return hmac.compare_digest(bytes(presented[0]), self._secret)

    async def _refuse(self, kind: str, send: Send) -> None:
        self.rejected += 1
        if kind == "websocket":
            await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})
            return
        await send({"type": "http.response.start", "status": 403, "headers": list(_FORBIDDEN_HEADERS)})
        await send({"type": "http.response.body", "body": _BODY})


# --- BodyLimit (red-team RT-001 / RT-002, docs/specs/redteam-2026-11-21-fixes.md F1) ---------------------------------
JSON_BODY_MAX_BYTES = 64 * 1024        # far above the largest honest ask (~4,000 characters plus a Turnstile token)
JSON_BODY_DEADLINE_S = 10.0
UPLOAD_SLACK_BYTES = 8192              # same multipart framing allowance as workspace_routes.UPLOAD_SIZE_SLACK
# Upload deadline: the full 15 MiB at a slow honest rate of 64 KiB/s (about 0.5 Mbit/s, a poor mobile link) takes 240 s;
# 300 s leaves 25 % headroom. A client slower than ~51 KiB/s is cut off.
UPLOAD_BODY_DEADLINE_S = 300.0
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_upload_route(method: str, path: str) -> bool:
    parts = path.split("/")
    return (method == "POST" and len(parts) == 5 and parts[:3] == ["", "api", "workspace"] and bool(parts[3])
            and parts[4] == "documents")


class BodyLimit:
    """Pure ASGI cap on the request body, in bytes and in time, before routing. JSON routes get ``json_max_bytes`` and
    ``json_deadline_s``; ``POST /api/workspace/{id}/documents`` gets ``upload_max_bytes`` (+ framing slack) and
    ``upload_deadline_s``. Over the cap (declared or counted) -> 413; body not complete in time -> 408 with
    ``Connection: close``. A request that declares no body (no Content-Length above 0, no Transfer-Encoding) on a bodiless
    method, and every non-http scope, passes through; responses (SSE) are never touched."""

    def __init__(self, app: Callable[..., Any], *, upload_max_bytes: int, json_max_bytes: int = JSON_BODY_MAX_BYTES,
                 json_deadline_s: float = JSON_BODY_DEADLINE_S, upload_deadline_s: float = UPLOAD_BODY_DEADLINE_S):
        self._app = app
        self._upload_max = upload_max_bytes + UPLOAD_SLACK_BYTES
        self._json_max = json_max_bytes
        self._json_deadline = json_deadline_s
        self._upload_deadline = upload_deadline_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "")
        request_headers = dict(scope.get("headers") or ())
        declared = request_headers.get(b"content-length", b"")
        # The framing decides, not the method: a GET, HEAD or OPTIONS that declares a body (a non-zero Content-Length or a
        # Transfer-Encoding) is bounded like a POST, so it cannot hold a connection open past a deadline (RT-002).
        declares_body = (declared.isdigit() and int(declared) > 0) or b"transfer-encoding" in request_headers
        if method not in _BODY_METHODS and not declares_body:
            await self._app(scope, receive, send)
            return
        upload = _is_upload_route(method, scope.get("path", ""))
        cap = self._upload_max if upload else self._json_max
        deadline = self._upload_deadline if upload else self._json_deadline
        if declared.isdigit() and int(declared) > cap:
            await _short_reply(send, 413, "Payload Too Large", close=True)
            return
        expects_body = declares_body
        refused = False
        seen = 0
        body_done = False
        started = time.monotonic()

        async def guarded_receive() -> dict:
            nonlocal refused, seen, body_done
            if body_done:
                return await receive()          # SSE: the app waits here for a disconnect; no body deadline applies
            if refused:
                return {"type": "http.disconnect"}
            try:
                message = await asyncio.wait_for(receive(), max(0.0, deadline - (time.monotonic() - started)))
            except asyncio.TimeoutError:
                refused = True
                await _short_reply(send, 408, "Request Timeout", close=True)
                return {"type": "http.disconnect"}
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                body_done = not message.get("more_body", False) and seen <= cap
                if seen > cap:
                    refused = True
                    await _short_reply(send, 413, "Payload Too Large", close=True)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict) -> None:
            if refused:
                return
            if message["type"] == "http.response.start" and expects_body and not body_done:
                # RT-002: a response that starts before the body is complete leaves unread bytes (and no deadline)
                # on a keep-alive connection; uvicorn closes the transport after a response that says so.
                headers = list(message.get("headers") or ())
                if not any(name.lower() == b"connection" for name, _ in headers):
                    message = {**message, "headers": [*headers, (b"connection", b"close")]}
            await send(message)

        try:
            await self._app(scope, guarded_receive, guarded_send)
        except Exception:
            if not refused:
                raise              # the app's own failure; only the disconnect we caused is swallowed


async def _short_reply(send: Send, status: int, detail: str, *, close: bool) -> None:
    body = ('{"detail":"%s"}' % detail).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")]
    if close:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
