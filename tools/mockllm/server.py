"""The mock LLM provider: an OpenAI-compatible FastAPI app for the staging fleet (M5a I5, docs/v2/M5_PLAN.md section 6).

Point the service at it with ``OPENAI_API_BASE=http://<host>:8000/v1`` and model ids ``openai/mock-luna`` (draft and
planner), ``openai/mock-sonnet`` (escalation). Any model id is accepted; its role is decided by :func:`profile.role_of`.

Endpoints
    POST /v1/chat/completions   stream and non-stream (``/chat/completions`` too); answers that the real verifier accepts
                                (:mod:`answers`), timing and length from ``profiles.json`` (:mod:`profile`), a planner
                                mode for requests with ``tools`` (:mod:`planner`), the fault knobs (:mod:`knobs`)
    GET  /v1/models             the model list
    GET  /metrics               JSON counters and process CPU (:mod:`metrics`; ``?format=prometheus`` for text)
    GET  /healthz               liveness
    GET/PUT /admin/knobs        the knobs in force / change them while running (bearer ``MOCKLLM_ADMIN_TOKEN``)

Environment: ``MOCKLLM_PROFILE`` (path of profiles.json), ``MOCKLLM_SEED``, ``MOCKLLM_TIME_SCALE`` (1 = real time, 0 = no
waiting at all: for tests), ``MOCKLLM_CHUNK_INTERVAL_S``, ``MOCKLLM_ADMIN_TOKEN`` and the knob variables of :mod:`knobs`.
The mock never reads or logs an API key or any header of the caller; it does not log prompts.

Chunking: the text is streamed in chunks ``chunk_interval_s`` apart (the profile's figure, 40 ms unless measured), not one
event per token, so that a few dozen concurrent streams cost the mock a small share of a core (CPU is a validity gate of the
load test); ``/metrics`` reports the CPU it used.
"""

import asyncio
import hmac
import math
import os
import random
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import shapes
from .knobs import KnobError, Knobs, apply_patch, from_env, from_headers
from .metrics import Metrics, to_prometheus
from .profile import Profile, load_profile, role_of
from .reply import Reply, build_reply

SERVED_MODELS = ("mock-luna", "mock-sonnet", "mock-haiku")
_PIECES = re.compile(r"\S+\s*|\s+")
MAX_BODY_MESSAGES = 1000


@dataclass(frozen=True)
class MockConfig:
    profile_path: Path | None = None
    seed: int | None = None
    time_scale: float = 1.0
    chunk_interval_s: float | None = None
    admin_token: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "MockConfig":
        seed = env.get("MOCKLLM_SEED", "")
        interval = env.get("MOCKLLM_CHUNK_INTERVAL_S", "")
        scale = float(env.get("MOCKLLM_TIME_SCALE", "1") or 1)
        if scale < 0:
            raise ValueError("MOCKLLM_TIME_SCALE must not be negative")
        return cls(Path(env["MOCKLLM_PROFILE"]) if env.get("MOCKLLM_PROFILE") else None, int(seed) if seed else None, scale,
                   float(interval) if interval else None, env.get("MOCKLLM_ADMIN_TOKEN", ""))


class ApiError(Exception):
    def __init__(self, status: int, message: str, type_: str = "invalid_request_error", code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.type_, self.code = status, message, type_, code


class MockState:
    """Everything the handlers share (one per app)."""

    def __init__(self, config: MockConfig, profile: Profile, knobs: Knobs, sleep: Callable, clock: Callable[[], float]):
        self.config, self.profile, self.knobs = config, profile, knobs
        self.sleep, self.clock = sleep, clock
        self.master = random.Random(config.seed)
        self.metrics = Metrics(time.time(), clock())
        self.interval_s = config.chunk_interval_s or profile.chunk_interval_s

    def request_rng(self, header_seed: str | None) -> random.Random:
        if header_seed:
            try:
                return random.Random(int(header_seed))
            except ValueError as e:
                raise ApiError(400, "x-mock-seed must be an integer") from e
        return random.Random(self.master.getrandbits(64))


def split_deltas(text: str, count: int) -> list[str]:
    """``text`` cut into at most ``count`` pieces at word boundaries; the pieces joined are exactly ``text``."""
    pieces = _PIECES.findall(text)
    if count >= len(pieces):
        return pieces
    step = len(pieces) / max(1, count)
    cuts = [round(i * step) for i in range(count + 1)]
    return [piece for piece in ("".join(pieces[a:b]) for a, b in zip(cuts, cuts[1:])) if piece]


def _error_response(e: ApiError) -> JSONResponse:
    return JSONResponse(shapes.error_body(e.message, type_=e.type_, code=e.code), status_code=e.status)


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as e:
        raise ApiError(400, "the request body is not valid JSON") from e
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages or len(messages) > MAX_BODY_MESSAGES:
        raise ApiError(400, "'messages' must be a non-empty list", code="invalid_value")
    return body


def _include_usage(body: Mapping) -> bool:
    options = body.get("stream_options")
    return bool(isinstance(options, Mapping) and options.get("include_usage"))


class Playback:
    """Plays one :class:`Reply` back in time: one object per request, the same schedule for stream and non-stream."""

    def __init__(self, state: MockState, reply: Reply, rng: random.Random):
        self.state, self.reply = state, reply
        self.cid = shapes.new_completion_id(rng)
        self.created = int(time.time())

    async def whole(self) -> dict:
        r = self.reply
        await self.state.sleep((r.ttft_s + r.decode_s) * self.state.config.time_scale)
        calls = [r.tool_call] if r.tool_call else None
        return shapes.completion_body(self.cid, r.model, self.created, r.content, r.finish_reason, r.usage, calls)

    def _chunk(self, delta: dict, finish: str | None, include_usage: bool, usage: dict | None = None) -> bytes:
        return shapes.sse_event(shapes.chunk_body(self.cid, self.reply.model, self.created, delta, finish,
                                                  include_usage=include_usage, usage=usage))

    async def _wait_until(self, due: float) -> None:
        await self.state.sleep(max(0.0, due - self.state.clock()))

    async def events(self, include_usage: bool) -> AsyncIterator[bytes]:
        r, scale = self.reply, self.state.config.time_scale
        await self.state.sleep(r.ttft_s * scale)
        first = self.state.clock()
        if r.tool_call:
            cid, name, arguments = r.tool_call
            call = {"index": 0, "id": cid, "type": "function", "function": {"name": name, "arguments": ""}}
            yield self._chunk({"role": "assistant", "content": None, "tool_calls": [call]}, None, include_usage)
            more = {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
            yield self._chunk(more, None, include_usage)
            await self._wait_until(first + r.decode_s * scale)
        else:
            yield self._chunk({"role": "assistant", "content": ""}, None, include_usage)
            async for piece in self._text_pieces(first, scale):
                yield self._chunk({"content": piece}, None, include_usage)
        yield self._chunk({}, r.finish_reason, include_usage)
        if include_usage:
            yield self._chunk({}, None, True, usage=r.usage)
        yield shapes.SSE_DONE

    async def _text_pieces(self, first: float, scale: float) -> AsyncIterator[str]:
        r = self.reply
        wanted = max(1, math.ceil(r.decode_s / self.state.interval_s))
        pieces = split_deltas(r.content or "", wanted)
        step = r.decode_s / max(1, len(pieces)) * scale
        for i, piece in enumerate(pieces):
            await self._wait_until(first + i * step)
            yield piece
        await self._wait_until(first + len(pieces) * step)


def _bearer_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    return bool(token) and hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


def create_app(config: MockConfig | None = None, *, env: Mapping[str, str] | None = None, profile: Profile | None = None,
               sleep: Callable = asyncio.sleep, clock: Callable[[], float] = time.monotonic) -> FastAPI:
    """The app. ``env`` defaults to ``os.environ``; ``sleep`` and ``clock`` are injectable for tests."""
    env = os.environ if env is None else env
    config = config or MockConfig.from_env(env)
    profile = profile or load_profile(config.profile_path)
    knobs = from_env(env, default_invalid_id_rate=profile.escalation_rate, default_slow_ttft_s=profile.slow_ttft_s)
    state = MockState(config, profile, knobs, sleep, clock)
    app = FastAPI(title="mockllm", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mock = state

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return _error_response(e)

    @app.exception_handler(KnobError)
    async def _knob_error(_: Request, e: KnobError) -> JSONResponse:
        return _error_response(ApiError(400, str(e)))

    _register_chat(app, state)
    _register_info(app, state)
    return app


def _register_chat(app: FastAPI, state: MockState) -> None:
    async def chat(request: Request):
        state.metrics.requests_total += 1
        try:
            return await serve_chat(request)
        except (ApiError, KnobError):
            state.metrics.bad_requests_total += 1
            raise

    async def serve_chat(request: Request):
        metrics = state.metrics
        body = await _read_body(request)
        model = str(body.get("model") or "mock-luna")
        metrics.count_model(model)
        knobs = from_headers(state.knobs, request.headers)
        rng = state.request_rng(request.headers.get("x-mock-seed"))
        if rng.random() < knobs.rate_429:
            metrics.rate_limited_total += 1
            metrics.rate_limited_by_role[role_of(model, bool(body.get("tools")))] += 1
            return JSONResponse(shapes.rate_limit_body(model), status_code=429,
                                headers=shapes.rate_limit_headers(knobs.retry_after_s))
        reply = build_reply(body, state.profile, knobs, rng)
        metrics.record_reply(reply)
        playback = Playback(state, reply, rng)
        if body.get("stream") is True:
            return StreamingResponse(_tracked(state, playback.events(_include_usage(body))),
                                     media_type="text/event-stream", headers={"cache-control": "no-cache"})
        metrics.enter()
        try:
            return JSONResponse(await playback.whole())
        finally:
            metrics.leave()

    app.add_api_route("/v1/chat/completions", chat, methods=["POST"])
    app.add_api_route("/chat/completions", chat, methods=["POST"])


async def _tracked(state: MockState, events: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """``events`` with the in-flight and stream counters kept, whichever way the stream ends (a client that leaves
    cancels this generator at an await)."""
    metrics = state.metrics
    metrics.streams_started += 1
    metrics.enter()
    finished = False
    try:
        async for event in events:
            yield event
        finished = True
    finally:
        metrics.leave()
        if finished:
            metrics.streams_completed += 1
        else:
            metrics.streams_aborted += 1


def _register_info(app: FastAPI, state: MockState) -> None:
    @app.get("/v1/models")
    @app.get("/models")
    async def models():
        return shapes.models_body(list(SERVED_MODELS), int(state.metrics.started_wall))

    @app.get("/v1/models/{model_id}")
    async def model(model_id: str):
        return shapes.model_body(model_id[:80], int(state.metrics.started_wall))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request):
        snapshot = state.metrics.snapshot(state.knobs, state.clock())
        if request.query_params.get("format") == "prometheus":
            return PlainTextResponse(to_prometheus(snapshot))
        return snapshot

    @app.get("/admin/knobs")
    async def get_knobs(request: Request):
        _require_admin(request, state)
        return {"knobs": state.knobs.as_dict()}

    @app.put("/admin/knobs")
    async def put_knobs(request: Request):
        _require_admin(request, state)
        try:
            patch = await request.json()
        except ValueError as e:
            raise ApiError(400, "the request body is not valid JSON") from e
        if not isinstance(patch, dict):
            raise ApiError(400, "the body must be a JSON object of knob values")
        state.knobs = apply_patch(state.knobs, patch)
        return {"knobs": state.knobs.as_dict()}


def _require_admin(request: Request, state: MockState) -> None:
    if not _bearer_ok(request, state.config.admin_token):
        raise ApiError(403, "admin access is off or the token is wrong", "invalid_request_error", "forbidden")
