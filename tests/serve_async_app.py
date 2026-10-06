"""The app ``tests/test_serve_async_streams.py`` runs under a real uvicorn, in a subprocess (M5a I2).

It mounts the real ``semigraph.serve.routes.router`` (``POST /api/ask``, ``GET /healthz``) over the real
``PaidStream`` / ``PaidResponse``, the real limiters, ``LimitedEmbedder`` and ``LoopLagMonitor``, and fakes only what
needs Neo4j or a model:

* the store functions the routes call (``get_answer``, ``put_answer``, ``log_query``, ...) are in-memory recorders;
* ``routes.run_cypher`` (the ``/healthz`` ping) returns a row, so the real probe, with its ``limiters.health`` hop,
  answers;
* ``routes.aanswer_stream`` is an async-generator twin of a real answer: it embeds on a worker thread under
  ``limiters.embed``, retrieves on a worker thread under ``limiters.db`` (each hop followed by a checkpoint, as the
  real twin's ``_hop``), then awaits ``anyio.sleep`` between deltas, as a model would.

The embedder is CPU-bound for 0.3 s of wall-clock time, in one of two kinds (``ASYNC_APP_SPIN_KIND``, switchable at
run time with ``POST /_test/embedder``):

* ``python``: a pure-Python loop. A worker thread running Python bytecode holds the GIL, so it slows the event loop
  however the work is scheduled: the loop gets the GIL back only when the interpreter's switch interval (5 ms)
  expires, once per GIL release it makes (every socket call). This is the pessimistic case, and the loop-lag monitor
  shows it;
* ``native``: repeated ``hashlib.sha256`` over 1 MiB, which releases the GIL for the whole call. It stands for the
  real ONNX embedder (onnxruntime and the tokenizers run native code outside the GIL) and does not slow the loop.

A single long C call that does NOT release the GIL (a ``re`` match, ``sum(range(...))``) would stall the loop for its
whole duration; neither kind does that. Nothing here is for production: the module lives under ``tests/`` and the
fakes are installed by the lifespan only, so importing it (the test module imports the script helpers) patches
nothing.

What the test process observes, over ``GET /_test/state``: the ledger rows (keyed by ``ip_hash``: ``log_query`` never
sees the question, so each ask sends its own ``x-test-ask`` header and the test computes the same hash), the borrowed
tokens of the answer limiter, of the three thread limiters and of starlette's default thread pool, the thread count
and its peak, the loop-lag monitor, how many stream cleanups ran (``PaidStream.finalize``, counted once per stream)
and how many twins started and were closed. Every state-changing step is also appended, flushed, to a journal file
(``ASYNC_APP_JOURNAL``) so a test that kills the server can still read what happened.

The question picks the behaviour (a leading ``[mode]``, which is not part of the embedded text): none =
``DELTAS_NORMAL`` deltas then ``done``; ``[medium]`` = ``DELTAS_MEDIUM``; ``[long]`` = ``DELTAS_LONG``; ``[hang]`` =
retrieval, one delta, then never finishes; ``[fat]`` = deltas of ``FAT_DELTA_CHARS`` characters with no pause (a
client that stops reading blocks the server's send).

A request that carries the ``PAUSE_HEADER`` header is stalled at the very end: ``PauseWritingAfterDone`` (the outermost
ASGI layer, ``app``) pauses uvicorn's write flow control right after the ``done`` frame, as the transport does for a
client that stopped reading in the last frame, so the response's closing empty chunk can never be written.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from types import SimpleNamespace

import anyio
import anyio.lowlevel
import anyio.to_thread
from fastapi import APIRouter, FastAPI, Request
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from semigraph.serve import guard, routes, store, stream_runtime
from semigraph.serve.embed import LimitedEmbedder
from semigraph.serve.limiters import LoopLagMonitor, make_limiters

ASK_HEADER = "x-test-ask"        # the per-ask client address: ``ask_key`` of it is the ledger row's key
# the server is a subprocess, so its pepper is fixed and the test process can compute the same hash
ASK_PEPPER = "pepper-of-the-async-app-0123456789-abcdef"     # gitleaks:allow
PAUSE_HEADER = "x-test-pause-after-done"     # ``1``: the transport stops draining once the ``done`` frame is out
DELTA_PAUSE_S = 0.015            # the simulated model's time per delta
RETRIEVAL_PAUSE_S = 0.005        # the simulated graph read
DELTAS_NORMAL, DELTAS_MEDIUM, DELTAS_LONG = 8, 150, 1000
FAT_DELTA_CHARS, FAT_DELTAS_MAX = 256 * 1024, 400
CITATION = "0001045810-26-000021:I.1:0320"
SAMPLE_INTERVAL_S = 0.01
SPIN_KINDS = ("native", "python")
NATIVE_BLOCK = bytes(1 << 20)
_MODE_RE = re.compile(r"^\[(\w+)\]")


def mode_of(question: str) -> str:
    match = _MODE_RE.match(question)
    return match.group(1) if match else "normal"


def ask_key(tag: str) -> str:
    """The ``ip_hash`` of the ledger row of an ask that sent ``tag`` in ``ASK_HEADER``."""
    return guard.ip_hash(tag, ASK_PEPPER)


def content_of(question: str) -> str:
    """The question without its ``[mode]`` tag: the tag is control, not content, so it is not part of what gets embedded
    (a ``[hang]`` and a ``[normal]`` question about the same thing share one cached vector, as two spellings would)."""
    return _MODE_RE.sub("", question, count=1).lstrip()


def delta_count(question: str) -> int:
    return {"normal": DELTAS_NORMAL, "medium": DELTAS_MEDIUM, "long": DELTAS_LONG, "hang": 1,
            "fat": FAT_DELTAS_MAX}.get(mode_of(question), DELTAS_NORMAL)


def delta_text(question: str, i: int) -> str:
    return "x" * FAT_DELTA_CHARS if mode_of(question) == "fat" else f"part{i} "


def retrieval_event() -> dict:
    return {"event": "retrieval", "anchors": {"Nvidia": 1045810},
            "counts": {"edges": 2, "metrics": 0, "risks": 0, "temporal": 0, "chunks": 1}, "anchor_defaulted": False}


def done_event(question: str, text: str) -> dict:
    return {"event": "done", "answer": text, "citations": [CITATION], "hallucinated": [], "finish_reason": "stop",
            "usage": {"prompt_tokens": 120, "completion_tokens": delta_count(question)}, "cost_usd": 0.0001,
            "strategy": "hybrid", "question": question}


def expected_events(question: str) -> list[dict]:
    """The events a finished ask of this question delivers, in order (``[hang]`` and ``[fat]`` do not finish)."""
    texts = [delta_text(question, i) for i in range(delta_count(question))]
    return [retrieval_event(), *({"event": "delta", "text": t} for t in texts), done_event(question, "".join(texts))]


class Config:
    """What the test process asks of this server instance, read from the environment (see the test module)."""

    def __init__(self, env=os.environ):
        self.max_answers = int(env.get("ASYNC_APP_MAX_ANSWERS", "40"))
        self.send_timeout_s = int(env.get("ASYNC_APP_SEND_TIMEOUT_S", "2"))
        self.embed_slots = int(env.get("ASYNC_APP_EMBED_SLOTS", "1"))
        self.db_threads = int(env.get("ASYNC_APP_DB_THREADS", "4"))
        self.spin_s = float(env.get("ASYNC_APP_SPIN_S", "0.3"))
        self.spin_kind = env.get("ASYNC_APP_SPIN_KIND", "native")
        if self.spin_kind not in SPIN_KINDS:
            raise ValueError(f"ASYNC_APP_SPIN_KIND must be one of {SPIN_KINDS}, got {self.spin_kind!r}")
        self.journal = env.get("ASYNC_APP_JOURNAL", "")


class ServerState:
    """Everything the test process may ask about. Written from the event loop and from worker threads."""

    def __init__(self, journal_path: str = ""):
        self._lock = threading.Lock()
        self._journal = open(journal_path, "a", encoding="utf-8") if journal_path else None
        self.peak_threads = threading.active_count()
        self.baseline_threads = self.peak_threads
        self.embed_active = 0
        self.ledger_delay_s = 0.0        # a slow ledger write, set by ``POST /_test/ledger-delay`` (a slow database)
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.ledger_rows: list[dict] = []
            self.cache_puts: list[dict] = []
            self.counts: Counter = Counter()
            self.twin_exits: Counter = Counter()
            self.order: list[str] = []
            self.embed_concurrent_max = 0
            self.baseline_threads = self.peak_threads = threading.active_count()

    def note(self, name: str, **fields) -> None:
        line = json.dumps({"t": time.time(), "event": name, **fields}, default=str)
        with self._lock:
            self.order.append(name)
            if self._journal is not None:
                self._journal.write(line + "\n")
                self._journal.flush()

    def count(self, key: str, by: int = 1) -> None:
        with self._lock:
            self.counts[key] += by

    def sample_threads(self) -> None:
        with self._lock:
            self.peak_threads = max(self.peak_threads, threading.active_count())

    def twin_closed(self, outcome: str) -> None:
        with self._lock:
            self.counts["twin_closed"] += 1
            self.twin_exits[outcome] += 1

    def embed_enter(self) -> None:
        with self._lock:
            self.embed_active += 1
            self.embed_concurrent_max = max(self.embed_concurrent_max, self.embed_active)
            self.counts["embed_calls"] += 1

    def embed_exit(self) -> None:
        with self._lock:
            self.embed_active -= 1

    # ---- the store functions the routes call (replacing ``semigraph.serve.store``'s)

    def log_query(self, driver, *, ip_hash, strategy, cached, usage=None, cost_usd=None, workspace=False) -> None:
        row = {"ip_hash": ip_hash, "strategy": strategy, "cached": cached, "workspace": workspace, "usage": usage,
               "cost_usd": cost_usd}
        time.sleep(self.ledger_delay_s)
        with self._lock:
            self.ledger_rows.append(row)
        self.note("ledger", **row)

    def put_answer(self, driver, *, question, **kw) -> None:
        with self._lock:
            self.cache_puts.append({"question": question})
        self.note("put_answer")

    def paid_queries_today(self, driver) -> int:
        with self._lock:
            return sum(1 for r in self.ledger_rows if not r["cached"])

    def snapshot(self, app_state) -> dict:
        st = app_state
        with self._lock:
            return {
                "ledger_rows": list(self.ledger_rows), "ledger_row_count": len(self.ledger_rows),
                "cache_puts": len(self.cache_puts),
                "answer_limiter_borrowed": st.answer_limiter.borrowed_tokens,
                "db_limiter_borrowed": st.limiters.db.borrowed_tokens,
                "embed_limiter_borrowed": st.limiters.embed.borrowed_tokens,
                "default_limiter_borrowed": anyio.to_thread.current_default_thread_limiter().borrowed_tokens,
                "thread_count": threading.active_count(), "peak_thread_count": self.peak_threads,
                "baseline_thread_count": self.baseline_threads,
                "loop_lag_warnings": st.loop_lag.warnings, "max_loop_lag_ms": round(st.loop_lag.max_lag_ms, 1),
                "finalized": self.counts["finalized"], "upstream_closed": self.counts["twin_closed"],
                "twins_started": self.counts["twin_started"], "model_steps": self.counts["model_steps"],
                "embed_calls": self.counts["embed_calls"], "embed_concurrent_max": self.embed_concurrent_max,
                "background_calls": self.counts["background_calls"], "twin_exits": dict(self.twin_exits),
                "write_pauses": self.counts["write_pauses"], "order": list(self.order), "pid": os.getpid()}


STATE: ServerState | None = None   # set by the lifespan; the fakes below read it when called


class CpuBoundEmbedder:
    """``encode_query`` burns ``spin_s`` of wall-clock time, in pure Python or in GIL-releasing native code."""

    name = "cpu-bound-fake"

    def __init__(self, spin_s: float, kind: str = "native"):
        self._spin_s, self._kind = spin_s, kind

    def encode_query(self, question: str) -> list[float]:
        STATE.embed_enter()
        try:
            deadline = time.perf_counter() + self._spin_s
            if self._kind == "python":
                while time.perf_counter() < deadline:
                    pass
            else:
                while time.perf_counter() < deadline:
                    hashlib.sha256(NATIVE_BLOCK).digest()
        finally:
            STATE.embed_exit()
        return [float(len(question) % 7), 1.0, 0.5]


async def _hop(fn, *args, limiter):
    """The real twins' hop: a worker thread under a named limiter, then a checkpoint."""
    result = await anyio.to_thread.run_sync(fn, *args, limiter=limiter)
    await anyio.lowlevel.checkpoint()
    return result


def _retrieve() -> None:
    time.sleep(RETRIEVAL_PAUSE_S)


async def _model_events(question: str):
    """The simulated model: a pause, then a delta, each one a paid step; ``done`` last (never for ``hang``)."""
    mode, texts = mode_of(question), []
    for i in range(delta_count(question)):
        if mode != "fat":
            await anyio.sleep(DELTA_PAUSE_S)
        else:
            await anyio.lowlevel.checkpoint()
        STATE.count("model_steps")
        texts.append(delta_text(question, i))
        yield {"event": "delta", "text": texts[-1]}
    if mode == "hang":
        await anyio.sleep_forever()
    yield done_event(question, "".join(texts))


async def fake_twin(question: str, driver, embedder, *, limiters, **_unused):
    """An async-generator twin of ``aanswer_stream`` (same event grammar), recording how it started and how it ended."""
    STATE.count("twin_started")
    STATE.note("twin_start", mode=mode_of(question))
    outcome = "completed"
    try:
        await _hop(embedder.encode_query, content_of(question), limiter=limiters.embed)
        await _hop(_retrieve, limiter=limiters.db)
        yield retrieval_event()
        async for event in _model_events(question):
            yield event
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        STATE.twin_closed(outcome)
        STATE.note("twin_exit", outcome=outcome)


def _install_fakes(state: ServerState) -> None:
    store.get_answer = lambda driver, key, ttl: None
    store.put_answer = state.put_answer
    store.log_query = state.log_query
    store.kill_switch_on = lambda driver, env_flag: False
    store.paid_queries_today = state.paid_queries_today
    routes.run_cypher = lambda driver, query, **params: [{"ok": 1}]
    routes.aanswer_stream = fake_twin
    _count_finalizes(state)


def _count_finalizes(state: ServerState) -> None:
    """Record every ``PaidStream.finalize`` call (and whether it was the first of its stream), and every background task
    sse-starlette runs for a ``PaidStream``. Patched on the class before any stream exists."""
    original_finalize = stream_runtime.PaidStream.finalize
    original_background = BackgroundTask.__call__

    async def finalize(self) -> None:
        first = not self._finalized
        state.note("finalize_start", first=first)
        try:
            await original_finalize(self)
        finally:
            state.note("finalize_end", first=first)
            if first:
                state.count("finalized")

    async def background(self) -> None:
        owner = getattr(self.func, "__self__", None)
        counted = isinstance(owner, stream_runtime.PaidStream)
        if counted:
            state.count("background_calls")
            state.note("background_start")
        try:
            await original_background(self)
        finally:
            if counted:
                state.note("background_end")

    stream_runtime.PaidStream.finalize = finalize
    BackgroundTask.__call__ = background


def _settings(cfg: Config) -> SimpleNamespace:
    """The generous settings the routes and ``PaidStream`` read: no real cap, no kill switch, no Turnstile."""
    return SimpleNamespace(
        max_question_chars=500, agent_enabled=False, uploads_enabled=False, client_ip_header=ASK_HEADER,
        ip_hash_pepper=ASK_PEPPER, answer_cache_ttl_hours=24, kill_switch=False, max_queries_per_day=100_000,
        turnstile_secret_key="", turnstile_required=False, is_production=False, llm_request_timeout_s=30,
        llm_answer_max_tokens=100, escalation_model="", send_timeout_s=cfg.send_timeout_s, embed_slots=cfg.embed_slots,
        db_thread_limit=cfg.db_threads, max_concurrent_answers=cfg.max_answers, loop_lag_warn_ms=100)


async def _sample_threads(state: ServerState) -> None:
    while True:
        state.sample_threads()
        await anyio.sleep(SAMPLE_INTERVAL_S)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global STATE
    cfg = Config()
    settings = _settings(cfg)
    STATE = state = ServerState(cfg.journal)
    _install_fakes(state)
    app.state.settings = settings
    app.state.driver = object()
    app.state.embedder = LimitedEmbedder(CpuBoundEmbedder(cfg.spin_s, cfg.spin_kind), settings.embed_slots)
    app.state.limiters = make_limiters(settings)
    app.state.answer_limiter = anyio.CapacityLimiter(settings.max_concurrent_answers)
    app.state.snapshot_id = ""
    app.state.tracer = None
    app.state.rate_limiter = guard.RateLimiter(10**6, 3600)
    app.state.free_rate_limiter = guard.RateLimiter(10**6, 3600)
    app.state.read_rate_limiter = guard.RateLimiter(10**6, 60)
    app.state.loop_lag = LoopLagMonitor(settings.loop_lag_warn_ms)
    tasks = [asyncio.create_task(app.state.loop_lag.run()), asyncio.create_task(_sample_threads(state))]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


test_router = APIRouter(prefix="/_test")


@test_router.get("/ready")
async def ready(request: Request) -> dict:
    """``loop`` is the module of the running event loop's class: the test pins that the server runs production's loop
    (``--loop asyncio``), not uvloop, whichever the machine has installed."""
    return {"ready": True, "pid": os.getpid(), "loop": type(asyncio.get_running_loop()).__module__}


@test_router.get("/state")
async def state_of(request: Request) -> dict:
    return STATE.snapshot(request.app.state)


@test_router.post("/embedder")
async def set_embedder_kind(request: Request, kind: str) -> dict:
    """Switch the fake embedder between ``native`` and ``python`` (see the module docstring) while the server runs."""
    if kind not in SPIN_KINDS:
        raise ValueError(f"kind must be one of {SPIN_KINDS}")
    request.app.state.embedder._embedder._kind = kind
    return {"kind": kind}


@test_router.post("/ledger-delay")
async def set_ledger_delay(ms: int) -> dict:
    """Make every ledger write take ``ms`` more (it runs on a worker thread): a client that sees the terminal event
    before the row exists will find the row missing."""
    STATE.ledger_delay_s = ms / 1000.0
    return {"ms": ms}


@test_router.post("/reset")
async def reset(request: Request) -> dict:
    """Zero the counters and the loop-lag and thread-peak records (after a warm-up, before the measured part)."""
    STATE.reset()
    monitor = request.app.state.loop_lag
    monitor.max_lag_ms, monitor.warnings = 0.0, 0
    return STATE.snapshot(request.app.state)


@test_router.post("/order")
async def order_probe(request: Request, run: str, hold_send_s: float = 0.0):
    """A plain sse-starlette response shaped like ``PaidResponse`` (a generator, a background task, a ``finally``
    around the ASGI call, a client-close handler), whose generator waits forever after two events. It records what
    runs, and in what order, when the client disconnects: ``hold_send_s > 0`` delays every body send, so a disconnect
    lands while the generator is suspended at its ``yield`` rather than awaiting."""
    def note(what: str) -> None:
        STATE.note(f"probe:{run}:{what}")

    async def events():
        note("generator_start")
        try:
            yield ServerSentEvent(data="first", sep="\n")
            yield ServerSentEvent(data="second", sep="\n")
            await anyio.sleep_forever()
        except BaseException as exc:
            note(f"generator_raised_{type(exc).__name__}")
            raise
        finally:
            note("generator_finally")

    async def background() -> None:
        note("background")

    async def client_closed(message) -> None:
        note("client_close_handler")

    class ProbeResponse(EventSourceResponse):
        async def __call__(self, scope, receive, send) -> None:
            async def slow_send(message) -> None:
                if hold_send_s and message["type"] == "http.response.body":
                    await anyio.sleep(hold_send_s)
                await send(message)
            try:
                await super().__call__(scope, receive, slow_send)
            finally:
                note("call_finally")

    return ProbeResponse(events(), ping=15, sep="\n", background=BackgroundTask(background),
                         client_close_handler_callable=client_closed)


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(routes.router)
    app.include_router(test_router)
    return app


def _flow_control_of(send):
    """uvicorn's ``FlowControl`` of the connection ``send`` writes to. uvicorn hands the app the bound ``send`` method
    of its ``RequestResponseCycle`` (httptools and h11 alike), which owns ``flow``. Raises, so that a test never passes
    without having stalled anything, if a uvicorn release stops doing so."""
    flow = getattr(getattr(send, "__self__", None), "flow", None)
    if flow is None:
        raise RuntimeError("cannot reach uvicorn's flow control from the ASGI send callable")
    return flow


class PauseWritingAfterDone:
    """A pure-ASGI wrapper around the app, outermost, that for a request carrying ``PAUSE_HEADER`` makes the transport
    look like a client that stopped reading just as the last frame went out: right after the chunk holding the ``done``
    event is handed to uvicorn it calls ``flow.pause_writing()``, which is what the transport does when its buffer
    passes the high-water mark (64 KiB unsent). Nothing ever resumes it, so uvicorn's ``send`` waits in
    ``flow.drain()`` for the next message (the empty closing chunk) until the connection closes. Deterministic: it
    needs no real backlog. Counted in ``write_pauses`` so a test can assert the stall was injected. Lifespan and other
    requests pass through."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (PAUSE_HEADER.encode(), b"1") not in scope["headers"]:
            return await self.inner(scope, receive, send)
        flow = _flow_control_of(send)

        async def pausing_send(message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and message.get("body", b"").startswith(b"event: done"):
                flow.pause_writing()
                STATE.count("write_pauses")
        await self.inner(scope, receive, pausing_send)


app = PauseWritingAfterDone(create_app())
