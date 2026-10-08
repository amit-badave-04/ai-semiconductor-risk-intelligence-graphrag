"""A strict parser for the site's server-sent-event grammar. Pure stdlib.

Two layers, so a wire problem and a grammar problem are told apart:

* :class:`SseParser` is the WHATWG event-stream framing: ``\\n``, ``\\r\\n`` and lone ``\\r`` line ends (sse-starlette's default
  separator is ``\\r\\n``; the site sets ``\\n``), ``event:`` / ``data:`` / ``id:`` / ``retry:`` fields, multi-line data, a
  blank line to dispatch, ``:`` comment lines (the server's keep-alive ping is ``: ping - <utc timestamp>``), and a UTF-8
  decoder that survives a multibyte character split across two network chunks (answers carry curly quotes). Anything that is
  not UTF-8, or an event without data, is an :class:`SseProtocolError`, never silently skipped.
* :func:`parse_ask_event` / :func:`parse_job_event` are the site's two grammars on top of it. ``/api/ask`` speaks
  ``retrieval | step | delta | escalated | done | error`` (``serve/stream_runtime.sse_event``: the data is the JSON event, and it
  repeats the event name under ``"event"``; a cached answer is a single ``done`` carrying ``"cached": true``). The workspace job
  stream speaks ``job`` (``workspace_routes._job_sse``), terminal at state ``ready`` or ``failed``. An unknown event name, data
  that is not a JSON object, a name that disagrees with the payload's own, or a missing required field is a protocol error.

Network chunks are fed as they arrive (``iter_content(chunk_size=None)``), so a short ``retrieval`` event is parsed the moment
it is sent and the time to first event measures the server, not a read buffer.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from typing import Any

ASK_EVENTS = ("retrieval", "step", "delta", "escalated", "done", "error")
ASK_TERMINAL = frozenset({"done", "error"})
JOB_EVENT = "job"
JOB_TERMINAL_STATES = frozenset({"ready", "failed"})

# required payload fields per event: name -> {field: type}. Only what every producer of the event writes
# (SEC, agent and workspace paths; the pre-M5 recordings in tests/data pin it).
_ASK_REQUIRED: dict[str, dict[str, type | tuple[type, ...]]] = {
    "retrieval": {"counts": dict},
    "step": {"n": int, "tool": str, "ok": bool},
    "delta": {"text": str},
    "escalated": {"from": str, "to": str, "reasons": list},
    "done": {"answer": str},
    "error": {"detail": str},
}


class SseProtocolError(ValueError):
    """The bytes are not a valid event stream, or an event breaks the site's grammar."""


@dataclass(frozen=True)
class SseMessage:
    """One dispatched event block, before the grammar looks at it."""

    event: str | None
    data: str
    id: str | None = None
    retry: int | None = None


@dataclass(frozen=True)
class Ping:
    """A comment line (keep-alive)."""

    comment: str


@dataclass(frozen=True)
class AskEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.name in ASK_TERMINAL

    @property
    def cached(self) -> bool:
        return self.name == "done" and self.payload.get("cached") is True


@dataclass(frozen=True)
class JobEvent:
    state: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in JOB_TERMINAL_STATES


class SseParser:
    """Feed bytes, get back :class:`SseMessage` and :class:`Ping` items in arrival order."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._text = ""
        self._first = True
        self._event: str | None = None
        self._data: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None
        self._has_fields = False

    # -- public -------------------------------------------------------------------------------------------------

    def feed(self, chunk: bytes) -> list[SseMessage | Ping]:
        try:
            piece = self._decoder.decode(chunk)
        except UnicodeDecodeError as e:
            raise SseProtocolError(f"the stream is not valid UTF-8 ({e.reason})") from None
        if self._first and piece:
            piece = piece.removeprefix("﻿")
            self._first = False
        self._text += piece
        return self._drain(final=False)

    def finish(self) -> list[SseMessage | Ping]:
        """End of stream: flush the decoder and a trailing line without terminator. A half-received event is NOT dispatched
        (see :attr:`pending`)."""
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as e:
            raise SseProtocolError(f"the stream ends inside a UTF-8 character ({e.reason})") from None
        self._text += tail
        return self._drain(final=True)

    @property
    def pending(self) -> bool:
        """True while bytes or fields of an event that was never dispatched (no blank line) are buffered: a truncated stream."""
        return bool(self._text) or self._has_fields

    # -- framing ------------------------------------------------------------------------------------------------

    def _drain(self, *, final: bool) -> list[SseMessage | Ping]:
        out: list[SseMessage | Ping] = []
        while True:
            line, rest = self._next_line(final)
            if line is None:
                break
            self._text = rest
            item = self._line(line)
            if item is not None:
                out.append(item)
        return out

    def _next_line(self, final: bool) -> tuple[str | None, str]:
        text = self._text
        for i, ch in enumerate(text):
            if ch == "\n":
                return text[:i], text[i + 1:]
            if ch == "\r":
                if i + 1 == len(text) and not final:
                    return None, text                      # a lone CR may be the first half of CRLF: wait for the next chunk
                skip = 2 if text[i + 1:i + 2] == "\n" else 1
                return text[:i], text[i + skip:]
        return None, text

    def _line(self, line: str) -> SseMessage | Ping | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return Ping(line[1:].lstrip(" "))
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        self._has_fields = True
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            self._id = value
        elif name == "retry":
            self._retry = int(value) if value.isdigit() else self._retry
        return None                                         # an unknown field is ignored, as the spec says

    def _dispatch(self) -> SseMessage | None:
        if not self._has_fields:
            return None                                     # a stray blank line
        if not self._data:
            self._reset()
            raise SseProtocolError("an event block without data")
        message = SseMessage(self._event, "\n".join(self._data), self._id, self._retry)
        self._reset()
        return message

    def _reset(self) -> None:
        self._event, self._data, self._id, self._retry, self._has_fields = None, [], None, None, False


# ---- the site's grammars --------------------------------------------------------------------------------------------

def _json_object(message: SseMessage) -> dict[str, Any]:
    try:
        payload = json.loads(message.data)
    except json.JSONDecodeError as e:
        raise SseProtocolError(f"event {message.event!r}: data is not JSON ({e.msg})") from None
    if not isinstance(payload, dict):
        raise SseProtocolError(f"event {message.event!r}: data is not a JSON object")
    return payload


def parse_ask_event(message: SseMessage) -> AskEvent:
    """One ``/api/ask`` event under the site's grammar, or :class:`SseProtocolError`."""
    name = message.event
    if name not in _ASK_REQUIRED:
        raise SseProtocolError(f"unknown ask event name {name!r}")
    payload = _json_object(message)
    if payload.get("event") != name:
        raise SseProtocolError(f"event {name!r}: payload names {payload.get('event')!r}")
    for key, kind in _ASK_REQUIRED[name].items():
        value = payload.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise SseProtocolError(f"event {name!r}: field {key!r} is missing or not {getattr(kind, '__name__', kind)}")
    return AskEvent(name, payload)


def parse_job_event(message: SseMessage) -> JobEvent:
    """One workspace job-stream event (``event: job``), or :class:`SseProtocolError`."""
    if message.event != JOB_EVENT:
        raise SseProtocolError(f"unknown job event name {message.event!r}")
    payload = _json_object(message)
    state = payload.get("state")
    if not isinstance(state, str) or not state:
        raise SseProtocolError("job event: field 'state' is missing")
    return JobEvent(state, payload)
