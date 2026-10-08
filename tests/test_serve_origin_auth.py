"""serve/asgi_middleware.py: the OriginAuth gate of the staging API (M5a I5, docs/v2/M5_DECISIONS.md 2.1 "Staging rules").

The staging apps are reachable from the internet only with a secret origin header. These tests drive the middleware with raw
ASGI callables (no HTTP client, no Starlette): the serve-shipped CI environment has pytest and the pinned serving
requirements and nothing else, and ``tests/test_serve_*.py`` runs there.

What is pinned: the check happens BEFORE the inner app (a sentinel app records every call and must stay silent on a 403);
a missing, wrong, short, long, empty or duplicated header is a 403; only an exact GET or HEAD of ``/healthz`` is exempt;
lifespan passes through; a websocket is closed with 1008 before it is accepted; the comparison is ``hmac.compare_digest`` on
bytes; the presented value is never logged or echoed; a short secret is refused at construction (naming the setting, never
the value); the request body is never read to answer the 403.
"""

import asyncio
import hmac
import json
import logging

import pytest

from semigraph.serve import asgi_middleware
from semigraph.serve.asgi_middleware import DEFAULT_HEADER, MIN_SECRET_BYTES, OriginAuth

SECRET = "origin-secret-for-tests-" + "s" * 24            # gitleaks:allow
WRONG = "origin-secret-for-tests-" + "t" * 24             # gitleaks:allow, same length as SECRET
HEADER = DEFAULT_HEADER.encode()


class Inner:
    """The app behind the gate: records every call, answers 200."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"inner"})
        elif scope["type"] == "websocket":
            await send({"type": "websocket.accept"})


def http_scope(path="/api/ask", method="GET", headers=(), query=b""):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
            "raw_path": path.encode(), "query_string": query, "headers": list(headers), "scheme": "https",
            "server": ("x", 443), "client": ("203.0.113.9", 4000)}


def auth(value):
    return (HEADER, value if isinstance(value, bytes) else value.encode())


async def never_receive():
    raise AssertionError("the middleware read the request body to answer")


def run(app, scope, receive=never_receive):
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def response_of(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def gate(inner=None, **kwargs):
    inner = inner or Inner()
    return inner, OriginAuth(inner, SECRET, **kwargs)


# --- the gate on HTTP -----------------------------------------------------------------------------------------------------

def test_the_right_header_reaches_the_inner_app_untouched():
    inner, app = gate()
    scope = http_scope(headers=[auth(SECRET)])
    status, _, body = response_of(run(app, scope))
    assert (status, body) == (200, b"inner")
    assert inner.calls == [scope] and inner.calls[0] is scope               # the very scope, not a copy
    assert app.rejected == 0


@pytest.mark.parametrize("headers", [
    [],                                                                   # missing
    [auth("")],                                                           # empty
    [auth(WRONG)],                                                        # same length, wrong
    [auth(SECRET[:-1])],                                                  # one byte short
    [auth(SECRET + "x")],                                                 # one byte long
    [auth(SECRET.upper())],
    [auth(b"\xff\xfe\x00" + SECRET.encode())],                            # not text at all: must not raise
    [auth(SECRET), auth(SECRET)],                                         # duplicated: ambiguous, refused
    [auth(WRONG), auth(SECRET)],
    [(b"x-origin-auth-extra", SECRET.encode())],                          # a different header
    [(b"authorization", SECRET.encode())],
    [(b"cookie", b"x-origin-auth=" + SECRET.encode())],
], ids=["missing", "empty", "wrong", "short", "long", "upper", "binary", "duplicated", "wrong-then-right",
        "other-header", "authorization", "cookie"])
def test_everything_but_exactly_one_right_header_is_a_403_before_the_inner_app(headers):
    inner, app = gate()
    status, response_headers, body = response_of(run(app, http_scope(headers=headers)))
    assert status == 403 and inner.calls == []
    assert json.loads(body) == {"detail": "Forbidden"}
    assert response_headers[b"content-type"] == b"application/json"
    assert int(response_headers[b"content-length"]) == len(body)
    assert response_headers[b"cache-control"] == b"no-store"
    assert SECRET.encode() not in body and WRONG.encode() not in body
    assert app.rejected == 1


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_every_method_is_gated_including_a_cors_preflight(method):
    inner, app = gate()
    assert response_of(run(app, http_scope(method=method)))[0] == 403 and inner.calls == []
    assert response_of(run(app, http_scope(method=method, headers=[auth(SECRET)])))[0] == 200


def test_the_403_is_sent_without_reading_the_body_and_the_inner_app_never_runs():
    inner, app = gate()
    run(app, http_scope(method="POST"), receive=never_receive)              # never_receive raises if it is called
    assert inner.calls == []


def test_a_query_string_does_not_stand_in_for_the_header():
    inner, app = gate()
    assert response_of(run(app, http_scope(query=b"x-origin-auth=" + SECRET.encode())))[0] == 403


# --- /healthz ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_healthz_is_exempt_for_get_and_head_only_by_its_exact_path(method):
    inner, app = gate()
    assert response_of(run(app, http_scope("/healthz", method)))[0] == 200 and len(inner.calls) == 1
    assert app.rejected == 0


@pytest.mark.parametrize("path", ["/healthz/", "/healthz/x", "/healthzz", "/api/healthz", "/healthz/../api/ask", "/HEALTHZ",
                                  "/healthz%2f", "//healthz", "/", "/api/ask"])
def test_nothing_but_the_exact_healthz_path_is_exempt(path):
    inner, app = gate()
    assert response_of(run(app, http_scope(path)))[0] == 403 and inner.calls == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_healthz_is_not_exempt_for_methods_that_change_things(method):
    inner, app = gate()
    assert response_of(run(app, http_scope("/healthz", method)))[0] == 403 and inner.calls == []


def test_the_exempt_paths_are_configurable_and_empty_means_none():
    inner, app = gate(exempt=())
    assert response_of(run(app, http_scope("/healthz")))[0] == 403 and inner.calls == []
    inner, app = gate(exempt=("/ready",))
    assert response_of(run(app, http_scope("/ready")))[0] == 200
    assert response_of(run(app, http_scope("/healthz")))[0] == 403


# --- other scope types --------------------------------------------------------------------------------------------------------

def test_lifespan_passes_through_without_a_header():
    inner, app = gate()
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    run(app, scope, receive=lambda: asyncio.sleep(0))
    assert inner.calls == [scope]


def test_a_websocket_without_the_header_is_closed_before_it_is_accepted():
    inner, app = gate()
    scope = {**http_scope("/ws"), "type": "websocket", "scheme": "wss", "subprotocols": []}
    sent = run(app, scope)
    assert sent == [{"type": "websocket.close", "code": 1008}] and inner.calls == []


def test_a_websocket_with_the_header_reaches_the_inner_app():
    inner, app = gate()
    scope = {**http_scope("/ws", headers=[auth(SECRET)]), "type": "websocket", "scheme": "wss", "subprotocols": []}
    assert run(app, scope) == [{"type": "websocket.accept"}] and inner.calls == [scope]


def test_an_unknown_scope_type_is_refused_not_passed_on():
    inner, app = gate()
    with pytest.raises(RuntimeError):
        run(app, {"type": "carrier-pigeon"})
    assert inner.calls == []


# --- the comparison and the secret ----------------------------------------------------------------------------------------------

def test_the_comparison_is_hmac_compare_digest_on_bytes(monkeypatch):
    seen = []
    real = hmac.compare_digest

    def recording(a, b):
        seen.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(asgi_middleware.hmac, "compare_digest", recording)
    inner, app = gate()
    run(app, http_scope(headers=[auth(SECRET)]))
    run(app, http_scope(headers=[auth(WRONG)]))
    assert seen == [(bytes, bytes), (bytes, bytes)]


def test_a_custom_header_name_is_honoured_and_the_default_stops_working():
    inner, app = gate(header="X-Staging-Auth")                              # any case in, lower-cased inside
    assert response_of(run(app, http_scope(headers=[(b"x-staging-auth", SECRET.encode())])))[0] == 200
    assert response_of(run(app, http_scope(headers=[auth(SECRET)])))[0] == 403


@pytest.mark.parametrize("secret", ["", " ", "short", "s" * (MIN_SECRET_BYTES - 1), None, b"b" * 40, 12345])
def test_a_missing_short_or_non_text_secret_is_refused_at_construction(secret):
    with pytest.raises(ValueError) as e:
        OriginAuth(Inner(), secret)
    message = str(e.value)
    assert "ORIGIN_AUTH_SECRET" in message
    if isinstance(secret, str) and secret.strip():
        assert secret not in message                                        # never the value


def test_the_minimum_length_secret_is_accepted_and_counts_bytes_not_characters():
    OriginAuth(Inner(), "s" * MIN_SECRET_BYTES)
    with pytest.raises(ValueError):
        OriginAuth(Inner(), "é" * (MIN_SECRET_BYTES // 2 - 1))               # 31 bytes in 15 characters


def test_a_secret_with_surrounding_whitespace_is_used_as_given_not_stripped():
    inner = Inner()
    padded = " " + SECRET
    app = OriginAuth(inner, padded)
    assert response_of(run(app, http_scope(headers=[auth(SECRET)])))[0] == 403
    assert response_of(run(app, http_scope(headers=[auth(padded)])))[0] == 200


def test_the_secret_is_not_in_the_repr_and_the_presented_value_is_never_logged(caplog):
    inner, app = gate()
    with caplog.at_level(logging.DEBUG):
        run(app, http_scope(headers=[auth(WRONG)]))
        run(app, http_scope(headers=[auth(SECRET)]))
    assert SECRET not in repr(app) and SECRET not in str(vars(app).keys())
    assert WRONG not in caplog.text and SECRET not in caplog.text


def test_the_module_is_stdlib_only():
    source = (asgi_middleware.__file__ and open(asgi_middleware.__file__, encoding="utf-8").read())
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"hmac", "logging", "__future__", "collections", "typing"}, imports
