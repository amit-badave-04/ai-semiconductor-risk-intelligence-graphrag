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

import hmac
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
