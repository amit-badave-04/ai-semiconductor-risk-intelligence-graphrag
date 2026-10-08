"""The generator's HTTP behaviour, free of locust: asks, reads and the upload cycle. stdlib + ``requests``.

``LoadClient`` takes a ``send(method, path, label=..., **kwargs) -> requests.Response`` callable. Under Locust that is
``HttpUser.client.request(..., name=label)`` (a ``requests.Session`` underneath, so ``stream=True`` works); in tests and for
anything else it is a plain ``requests.Session`` bound to a base URL. Every request carries ``X-Origin-Auth``,
``X-Test-Client-IP`` (unique per VU) and ``X-Load-Run-Id``; the bot check is satisfied by a non-empty ``turnstile_token`` (the
staging stub accepts any).

An ask is measured on the CLIENT, from the bytes it reads:

* the stream is read with ``iter_content(chunk_size=None)`` and parsed as each network chunk arrives, so a short ``retrieval``
  event is seen when it is sent and TTFE (time to the first ``retrieval`` / ``step`` event; for a cached answer, its single
  ``done``) measures the server, not a read buffer (``first_delta_s`` is recorded beside it to show the order);
* the outcome is exactly one of ``done`` | ``error_event`` (an ``error`` event inside a 200) | ``shed_429`` | ``shed_503`` |
  ``http_<status>`` | ``bad_content_type`` | ``protocol_error`` | ``timeout`` (no response before the read timeout) |
  ``exception`` (the connection failed) | ``dropped`` (the stream began and ended, or stalled, with no ``done`` / ``error``);
* a live ask is never counted as a success unless its terminal ``done`` arrived.

The upload cycle (the 5-VU population) creates a workspace, uploads one document whose text is unique to this cycle (an
unchanged hash would short-circuit with no job), watches the job stream to ``ready``, then deletes the workspace.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from tools.loadtest import sse
from tools.loadtest.records import RecordWriter

Send = Callable[..., requests.Response]

OK_OUTCOMES = frozenset({"done", "ok", "ready"})
FIRST_EVENT_NAMES = frozenset({"retrieval", "step"})
DETAIL_CHARS = 200
UPLOAD_PARAGRAPH = ("Supply concentration, export-control exposure and customer dependence are described in this synthetic "
                    "load-test memo so that the chunker, the token counter and the embedder have real work to do. ")


@dataclass(frozen=True)
class ClientConfig:
    run_id: str
    worker: int
    vu: int
    ip: str
    origin_auth: str = ""
    turnstile_token: str = "loadtest-stub"
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0              # longer than the server's 15 s keep-alive ping
    stream_cap_s: float = 180.0               # an ask that has not finished by then is dropped
    upload_watch_cap_s: float = 300.0


class StreamGauge:
    """How many streams this process has open right now (live and total); sampled at 1 Hz by the generator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.live = 0
        self.total = 0

    def enter(self, live: bool) -> None:
        with self._lock:
            self.total += 1
            self.live += int(live)

    def leave(self, live: bool) -> None:
        with self._lock:
            self.total -= 1
            self.live -= int(live)

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.live, self.total


@dataclass
class _Stream:
    """What one ask read, filled in as the bytes arrive."""

    events: dict[str, int] = field(default_factory=dict)
    pings: int = 0
    nbytes: int = 0
    ttfe_s: float | None = None
    first_delta_s: float | None = None
    terminal: sse.AskEvent | None = None
    first_name: str | None = None
    escalated: bool = False
    outcome: str | None = None
    detail: str = ""


class LoadClient:
    def __init__(self, send: Send, config: ClientConfig, writer: RecordWriter, *, gauge: StreamGauge | None = None,
                 phase: Callable[[], str] = lambda: "steady", clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 listeners: tuple[Callable[[dict], None], ...] = ()) -> None:
        self.send, self.cfg, self.writer = send, config, writer
        self.gauge, self.phase, self.clock, self.wall = gauge or StreamGauge(), phase, clock, wall
        self.listeners = listeners

    # -- plumbing -------------------------------------------------------------------------------------------------------

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"X-Test-Client-IP": self.cfg.ip, "X-Load-Run-Id": self.cfg.run_id}
        if self.cfg.origin_auth:
            h["X-Origin-Auth"] = self.cfg.origin_auth
        h.update(extra or {})
        return h

    def emit(self, kind: str, **fields: Any) -> dict:
        record = {"kind": kind, "ts": round(self.wall(), 3), "run_id": self.cfg.run_id, "worker": self.cfg.worker,
                  "vu": self.cfg.vu, "phase": self.phase(), **fields}
        self.writer.write(record)
        for listener in self.listeners:
            listener(record)
        return record

    def _timeout(self) -> tuple[float, float]:
        return self.cfg.connect_timeout_s, self.cfg.read_timeout_s

    def _call(self, method: str, path: str, label: str, **kwargs) -> tuple[requests.Response | None, str | None, str]:
        """``(response, failure_outcome, detail)``: a response, or why there is none."""
        try:
            return self.send(method, path, label=label, timeout=self._timeout(), **kwargs), None, ""
        except requests.exceptions.ReadTimeout as e:             # connected, then no response: a ConnectTimeout is an "exception"
            return None, "timeout", type(e).__name__
        except requests.exceptions.RequestException as e:
            return None, "exception", f"{type(e).__name__}: {str(e)[:DETAIL_CHARS]}"

    @staticmethod
    def _status_outcome(status: int) -> str:
        return {429: "shed_429", 503: "shed_503"}.get(status, f"http_{status}")

    @staticmethod
    def _body_detail(response: requests.Response) -> str:
        try:
            text = response.text
        except Exception:                                                    # noqa: BLE001 - detail only
            return ""
        return text[:DETAIL_CHARS]

    # -- reads ------------------------------------------------------------------------------------------------------------

    def read(self, path: str, *, label: str, want_json: bool = False) -> tuple[dict, Any]:
        """A plain GET. Returns ``(record, parsed_json_or_None)``."""
        t0 = self.clock()
        response, failure, detail = self._call("GET", path, label, headers=self.headers())
        if response is None:
            return self.emit("read", label=label, status=None, outcome=failure, total_s=round(self.clock() - t0, 4),
                              detail=detail), None
        status, data, outcome = response.status_code, None, "ok" if response.status_code == 200 else None
        try:
            if status == 200 and want_json:
                data = response.json()
            else:
                response.content                                            # noqa: B018 - read the body: the time is the whole reply
        except ValueError:
            outcome, detail = "protocol_error", "body is not JSON"
        except requests.exceptions.RequestException as e:
            outcome, detail = "exception", type(e).__name__
        outcome = outcome or self._status_outcome(status)
        record = self.emit("read", label=label, status=status, outcome=outcome, total_s=round(self.clock() - t0, 4),
                            detail=detail or ("" if outcome == "ok" else self._body_detail(response)))
        response.close()
        return record, data

    # -- asks -------------------------------------------------------------------------------------------------------------

    def ask(self, klass: str, question: str, strategy: str, *, salt: str | None = None) -> tuple[dict, dict | None]:
        """One ask. Returns ``(record, done_payload_or_None)``."""
        live = klass != "cached"
        label = f"ask[{klass}]"
        body = {"question": question, "strategy": strategy, "turnstile_token": self.cfg.turnstile_token}
        t0 = self.clock()
        self.gauge.enter(live)
        try:
            response, failure, detail = self._call("POST", "/api/ask", label, json=body, stream=True,
                                                   headers=self.headers({"Accept": "text/event-stream"}))
            if response is None:
                return self._ask_record(klass, strategy, question, salt, None, failure, t0, None, _Stream(), detail), None
            try:
                stream = self._read_stream(response, t0)
            except Exception as e:                                           # noqa: BLE001 - an unforeseen reader bug is one failed ask
                stream = _Stream(outcome="exception", detail=f"{type(e).__name__}: {str(e)[:DETAIL_CHARS]}")
            finally:
                ttfb_s, status = getattr(response, "elapsed", None), response.status_code
                response.close()
        finally:
            self.gauge.leave(live)
        done = stream.terminal.payload if stream.terminal and stream.terminal.name == "done" else None
        record = self._ask_record(klass, strategy, question, salt, status, stream.outcome, t0,
                                  ttfb_s.total_seconds() if ttfb_s is not None else None, stream, stream.detail)
        return record, done

    def _ask_record(self, klass, strategy, question, salt, status, outcome, t0, ttfb_s, stream: _Stream, detail) -> dict:
        done = stream.terminal.payload if stream.terminal and stream.terminal.name == "done" else {}
        return self.emit(
            "ask", klass=klass, strategy=strategy, question=question, salt=salt, status=status, outcome=outcome,
            ttfb_s=None if ttfb_s is None else round(ttfb_s, 4),
            ttfe_s=None if stream.ttfe_s is None else round(stream.ttfe_s, 4),
            first_delta_s=None if stream.first_delta_s is None else round(stream.first_delta_s, 4),
            total_s=round(self.clock() - t0, 4), events=stream.events, pings=stream.pings, bytes=stream.nbytes,
            cached=done.get("cached") is True if done else None, escalated=stream.escalated or bool(done.get("escalated")),
            answered_by=done.get("answered_by"), citations=list(done.get("citations") or [])[:10],
            detail=detail, ip=self.cfg.ip)

    def _read_stream(self, response: requests.Response, t0: float) -> _Stream:
        stream = _Stream()
        status = response.status_code
        if status != 200:
            stream.outcome, stream.detail = self._status_outcome(status), self._body_detail(response)
            return stream
        if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
            stream.outcome, stream.detail = "bad_content_type", (response.headers.get("Content-Type") or "")[:DETAIL_CHARS]
            return stream
        parser = sse.SseParser()
        try:
            for chunk in response.iter_content(chunk_size=None):
                if not chunk:
                    continue
                stream.nbytes += len(chunk)
                now = self.clock() - t0
                for item in parser.feed(chunk):
                    self._take(stream, item, now)
                    if stream.terminal is not None:
                        break
                if stream.terminal is not None:
                    break
                if now > self.cfg.stream_cap_s:
                    stream.outcome, stream.detail = "dropped", f"no terminal event within {self.cfg.stream_cap_s:g} s"
                    return stream
            if stream.terminal is None:
                for item in parser.finish():
                    self._take(stream, item, self.clock() - t0)
        except sse.SseProtocolError as e:
            stream.outcome, stream.detail = "protocol_error", str(e)[:DETAIL_CHARS]
            return stream
        except requests.exceptions.RequestException as e:             # reset, truncated chunked body, read timeout mid-stream
            stream.outcome, stream.detail = "dropped", f"{type(e).__name__}: {str(e)[:DETAIL_CHARS]}"
            return stream
        if stream.terminal is None:
            stream.outcome, stream.detail = "dropped", "the stream ended without a done or error event"
        else:
            stream.outcome = "done" if stream.terminal.name == "done" else "error_event"
            if stream.terminal.name == "error":
                stream.detail = str(stream.terminal.payload.get("detail"))[:DETAIL_CHARS]
        return stream

    @staticmethod
    def _take(stream: _Stream, item, elapsed: float) -> None:
        if isinstance(item, sse.Ping):
            stream.pings += 1
            return
        event = sse.parse_ask_event(item)
        stream.events[event.name] = stream.events.get(event.name, 0) + 1
        if stream.first_name is None:
            stream.first_name = event.name
        if stream.ttfe_s is None and (event.name in FIRST_EVENT_NAMES or event.cached):
            stream.ttfe_s = elapsed
        if event.name == "delta" and stream.first_delta_s is None:
            stream.first_delta_s = elapsed
        if event.name == "escalated":
            stream.escalated = True
        if event.terminal:
            stream.terminal = event

    # -- the upload cycle ---------------------------------------------------------------------------------------------------

    def upload_document(self, seq: int) -> bytes:
        marker = f"load-test upload run={self.cfg.run_id} worker={self.cfg.worker} vu={self.cfg.vu} seq={seq} nonce={uuid.uuid4().hex}"
        return (marker + "\n\n" + "\n\n".join(UPLOAD_PARAGRAPH + f"Paragraph {i}." for i in range(24))).encode("utf-8")

    def _step(self, label: str, method: str, path: str, **kwargs) -> tuple[requests.Response | None, dict]:
        t0 = self.clock()
        response, failure, detail = self._call(method, path, label, **kwargs)
        if response is None:
            return None, self.emit("upload_step", label=label, status=None, outcome=failure,
                                    total_s=round(self.clock() - t0, 4), detail=detail)
        ok = response.status_code in (200, 201, 202, 204)
        record = self.emit("upload_step", label=label, status=response.status_code,
                            outcome="ok" if ok else self._status_outcome(response.status_code),
                            total_s=round(self.clock() - t0, 4), detail="" if ok else self._body_detail(response))
        return (response if ok else None), record

    def upload_cycle(self, seq: int) -> dict:
        """create workspace -> upload -> watch the job to a terminal state -> delete. Returns the ``upload`` summary record."""
        t0 = self.clock()

        def finish(outcome: str, detail: str = "", **extra) -> dict:
            return self.emit("upload", outcome=outcome, detail=detail, total_s=round(self.clock() - t0, 4), seq=seq, **extra)

        created, record = self._step("upload_create", "POST", "/api/workspace", json={"turnstile_token": self.cfg.turnstile_token},
                                     headers=self.headers())
        if created is None:
            return finish(record["outcome"], record.get("detail", ""))
        try:
            created_json = created.json()
            workspace_id, token = created_json["workspace_id"], created_json["token"]
        except (ValueError, KeyError, TypeError):
            return finish("protocol_error", "the workspace reply has no id and token")
        auth = self.headers({"X-Workspace-Token": token, "X-Turnstile-Token": self.cfg.turnstile_token})
        name = f"loadtest-{self.cfg.run_id}-{self.cfg.vu}-{seq}.txt"
        posted, record = self._step("upload_post", "POST", f"/api/workspace/{workspace_id}/documents", headers=auth,
                                    files={"file": (name, self.upload_document(seq), "text/plain")})
        if posted is None:
            self._delete(workspace_id, auth)
            return finish(record["outcome"], record.get("detail", ""))
        try:
            job_id = posted.json().get("job_id")
        except (ValueError, AttributeError):
            job_id = None
        if not job_id:
            self._delete(workspace_id, auth)
            return finish("protocol_error", "the upload was accepted without a job id")
        outcome, detail, to_ready = self._watch_job(workspace_id, job_id, auth)
        self._delete(workspace_id, auth)
        return finish(outcome, detail, to_ready_s=to_ready)

    def _watch_job(self, ws: str, job_id: str, headers: dict[str, str]) -> tuple[str, str, float | None]:
        t0 = self.clock()
        response, failure, detail = self._call("GET", f"/api/workspace/{ws}/jobs/{job_id}", "upload_watch",
                                               headers={**headers, "Accept": "text/event-stream"}, stream=True)
        if response is None:
            return failure or "exception", detail, None
        try:
            if response.status_code != 200:
                return self._status_outcome(response.status_code), self._body_detail(response), None
            parser, state = sse.SseParser(), ""
            try:
                for chunk in response.iter_content(chunk_size=None):
                    for item in parser.feed(chunk):
                        if isinstance(item, sse.Ping):
                            continue
                        event = sse.parse_job_event(item)
                        state = event.state
                        if event.terminal:
                            return ("ready" if state == "ready" else "failed"), str(event.payload.get("error") or ""), \
                                round(self.clock() - t0, 3)
                    if self.clock() - t0 > self.cfg.upload_watch_cap_s:
                        return "timeout", f"job still {state!r} after {self.cfg.upload_watch_cap_s:g} s", None
            except sse.SseProtocolError as e:
                return "protocol_error", str(e)[:DETAIL_CHARS], None
            except requests.exceptions.RequestException as e:
                return "dropped", f"{type(e).__name__}: {str(e)[:DETAIL_CHARS]}", None
            return "dropped", f"the job stream ended in state {state!r}", None
        finally:
            response.close()

    def _delete(self, workspace_id: str, headers: dict[str, str]) -> None:
        response, _ = self._step("upload_delete", "DELETE", f"/api/workspace/{workspace_id}", headers=headers)
        if response is not None:
            response.close()
