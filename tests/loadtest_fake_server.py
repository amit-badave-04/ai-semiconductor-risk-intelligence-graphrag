"""A stdlib fake of the staging API for the load generator's tests: it speaks the real wire (HTTP/1.1 chunked server-sent events
encoded by sse-starlette, the site's ``\\n`` separator, the keep-alive ping comment) and the real routes the generator calls, so
``tools/loadtest`` is exercised end to end without Fly, a provider, Neo4j or the embedder.

A question containing a marker picks the behaviour: ``[[429]]`` / ``[[503]]`` / ``[[500]]`` (refused before the stream),
``[[drop]]`` (socket cut mid-stream), ``[[eof]]`` (clean end, no terminal event), ``[[error]]`` (an ``error`` event),
``[[bad]]`` (an event name outside the grammar), ``[[html]]`` (a 200 that is not an event stream), ``[[stall]]`` (silence past
the client's read timeout), ``[[escalate]]``, ``[[slow]]`` (0.3 s before the first event), ``[[hang]]`` (no response headers within the read timeout). A question equal to a listed
example is a cached answer: one ``done`` event with ``"cached": true``. Every request must carry ``X-Origin-Auth``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sse_starlette import ServerSentEvent

ORIGIN_AUTH = "fake-origin-secret"            # gitleaks:allow
EVENT_GAP_S = 0.15                            # between the retrieval event and the first delta: TTFE must come first


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip("?.! ")


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:       # a client that aborts a stream is part of the tests
        pass


class FakeStagingServer:
    def __init__(self, *, examples: list[dict] | None = None, origin_auth: str = ORIGIN_AUTH, job_final: str = "ready",
                 stats: dict | None = None) -> None:
        self.examples = examples if examples is not None else [
            {"id": "N1", "type": "numeric", "question": "What was Nvidia's total revenue for the fiscal year ended January 28, 2024?"},
            {"id": "N2", "type": "numeric", "question": "What was AMD's total revenue for fiscal 2024?"}]
        self.origin_auth, self.job_final = origin_auth, job_final
        self.stats = stats or {"limits": {"max_queries_per_day": 0, "max_spend_usd_per_day": 0, "per_ip_per_day": 0,
                                          "per_ip": "20 per 10 min", "max_question_chars": 500},
                               "agent_enabled": True, "uploads_enabled": True, "paused": False}
        self.requests: list[dict] = []
        self.asks: list[dict] = []
        self.uploads: list[bytes] = []
        self._lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"fake": self})
        self.httpd = _QuietServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "FakeStagingServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def record(self, entry: dict) -> None:
        with self._lock:
            self.requests.append(entry)

    @property
    def cached_keys(self) -> set[str]:
        return {_norm(e["question"]) for e in self.examples}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeStagingServer

    def log_message(self, *args) -> None:          # keep pytest output clean
        pass

    # -- helpers --------------------------------------------------------------------------------------------------------

    def _json(self, status: int, payload, extra: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if status != 204:
            self.wfile.write(body)

    def _stream_headers(self, content_type: str = "text/event-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _event(self, event: dict | str, name: str | None = None) -> None:
        payload = event if isinstance(event, dict) else {"event": name}
        self._chunk(ServerSentEvent(data=json.dumps(payload), event=name or payload["event"], sep="\n").encode())

    def _end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _auth_ok(self) -> bool:
        self.fake.record({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.headers.get("X-Origin-Auth") != self.fake.origin_auth:
            self._json(403, {"detail": "forbidden"})
            return False
        return True

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    # -- routes -----------------------------------------------------------------------------------------------------------

    def do_GET(self) -> None:                      # noqa: N802
        if not self._auth_ok():
            return
        path = self.path.split("?")[0]
        if path == "/":
            body = b"<!doctype html><title>fake</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/stats":
            self._json(200, self.fake.stats)
        elif path == "/api/examples":
            self._json(200, {"source": "fake", "examples": self.fake.examples})
        elif path == "/api/freshness":
            self._json(200, {"enabled": False, "status": "disabled"})
        elif path.startswith("/api/evidence/"):
            missing = "missing" in path
            self._json(404 if missing else 200, {"detail": "no evidence with that id"} if missing else {"type": "chunk", "text": "x"})
        elif path.startswith("/api/company/"):
            self._json(200, {"ticker": path.split("/")[3], "path": path})
        elif "/jobs/" in path:
            self._job_stream()
        else:
            self._json(404, {"detail": "not found"})

    def do_DELETE(self) -> None:                   # noqa: N802
        if self._auth_ok():
            self._json(204, None)

    def do_POST(self) -> None:                     # noqa: N802
        raw = self._body()                         # read first: an unread body would corrupt the keep-alive connection
        if not self._auth_ok():
            return
        path = self.path.split("?")[0]
        if path == "/api/ask":
            self._ask(json.loads(raw))
        elif path == "/api/workspace":
            ok = bool(json.loads(raw).get("turnstile_token"))
            self._json(201 if ok else 403, {"workspace_id": "w" * 32, "token": "t" * 43, "expires_at": "later"} if ok else {"detail": "bot"})
        elif path.endswith("/documents"):
            self._upload(raw)
        else:
            self._json(404, {"detail": "not found"})

    def _upload(self, raw: bytes) -> None:
        if not self.headers.get("X-Workspace-Token") or not self.headers.get("X-Turnstile-Token"):
            return self._json(404, {"detail": "workspace not found"})
        message = BytesParser(policy=default_policy).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
        parts = [p for p in message.iter_parts() if p.get_filename()]
        if not parts:
            return self._json(400, {"detail": "a file is required"})
        with self.fake._lock:
            self.fake.uploads.append(parts[0].get_payload(decode=True))
        self._json(202, {"job_id": "j" * 12, "document_id": "d1", "version": 1})

    def _job_stream(self) -> None:
        self._stream_headers()
        states = ["queued", "extracting", "embedding"]
        for state in states:
            self._chunk(ServerSentEvent(data=json.dumps({"state": state}), event="job", sep="\n").encode())
            time.sleep(0.02)
        if self.fake.job_final == "stall":
            time.sleep(2)
        self._chunk(ServerSentEvent(data=json.dumps({"state": self.fake.job_final, "error": "boom" if self.fake.job_final == "failed" else None}),
                                    event="job", sep="\n").encode())
        self._end()

    def _ask(self, body: dict) -> None:
        question = body.get("question", "")
        with self.fake._lock:
            self.fake.asks.append({"body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if "[[hang]]" in question:
            time.sleep(2)                           # no response at all before the client's read timeout
        for marker, status in (("[[429]]", 429), ("[[503]]", 503), ("[[500]]", 500)):
            if marker in question:
                return self._json(status, {"detail": f"refused {status}"})
        if "[[html]]" in question:
            self._stream_headers("text/html")
            self._chunk(b"<html></html>")
            return self._end()
        if _norm(question) in self.fake.cached_keys:
            self._stream_headers()
            self._event({"event": "done", "cached": True, "question": question, "strategy": body.get("strategy"),
                         "answer": "Cached answer [x].", "citations": ["x"], "hallucinated": [], "source": "benchmark"})
            return self._end()
        if "[[slow]]" in question:
            time.sleep(0.3)
        self._stream_headers()
        self._event({"event": "retrieval", "anchors": {}, "counts": {"chunks": 2}, "anchor_defaulted": False})
        time.sleep(EVENT_GAP_S)
        if "[[stall]]" in question:
            time.sleep(2)
            return
        if "[[bad]]" in question:
            return self._event({"event": "progress"})
        if "[[escalate]]" in question:
            self._event({"event": "escalated", "from": "mock/luna", "to": "mock/sonnet", "reasons": ["draft rejected"]})
        self._event({"event": "delta", "text": "Nvidia depends on TSMC [x]. "})
        if "[[drop]]" in question:
            self.close_connection = True
            return                                  # no terminating chunk: the client sees a truncated body
        self._chunk(ServerSentEvent(comment=f"ping - {datetime.now(timezone.utc)}", sep="\n").encode())
        self._event({"event": "delta", "text": "and on foundry partners [y]."})
        if "[[eof]]" in question:
            return self._end()
        if "[[error]]" in question:
            self._event({"event": "error", "detail": "The answer service failed.", "partial": True})
            return self._end()
        self._event({"event": "done", "answer": "Nvidia depends on TSMC [x]. and on foundry partners [y].",
                     "answered_by": "mock/luna", "citations": ["x", "y"], "escalated": "[[escalate]]" in question,
                     "hallucinated": [], "checks": {}, "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
        self._end()


def body_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":                       # `python tests/loadtest_fake_server.py`: serve until killed, print the URL
    import sys

    with FakeStagingServer() as server:
        print(server.url, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            sys.exit(0)
