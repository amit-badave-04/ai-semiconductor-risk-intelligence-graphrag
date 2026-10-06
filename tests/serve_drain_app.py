"""The app ``tests/test_serve_drain.py`` runs under the real ``DrainingServer`` (``python -m semigraph.serve.drain``),
in a subprocess (M5a I4, SIGTERM drain, docs/v2/M5A_BUILD_PLAN.md item D).

It is small on purpose: it imports ``semigraph.serve.drain`` and nothing else of the service (no litellm, no Neo4j),
and it plays the part the wiring plays in production:

* ``POST /ask?mode=short|forever`` is the paid path. Like the real route it answers 503 once the drain has begun
  (``DRAIN.draining``), and otherwise with a ``DrainedResponse``: an ``EventSourceResponse`` with sse-starlette's
  DEFAULT options (``shutdown_grace_period`` 0, which is what ``PaidResponse`` uses today). ``short`` streams 30 deltas
  0.1 s apart (3 s) and ends with ``done``; ``forever`` never ends. ``grace`` is the response's
  ``shutdown_grace_period`` (0 unless a test asks for another).
* ``DrainedStream`` has ``PaidStream``'s lifetime and the pairing the wiring snippet of ``drain.py`` recommends: it
  counts itself with ``DRAIN.try_enter()`` when its ``events()`` starts (a drain that began since the route's check
  refuses it with an ``error`` event); the ledger row is written BEFORE the terminal event; ``finalize`` is idempotent
  and shielded, runs at the end of ``events()`` and in a ``finally`` around the whole ASGI call, and its LAST step is
  ``DRAIN.leave()``. Its ledger write is deliberately slow (``FINALIZE_S``), so a lifespan that did not wait would
  close the "driver" first.
* ``GET /read`` and ``GET /healthz`` are the reads that must keep answering during a drain.
* The lifespan awaits ``DRAIN.await_idle`` after ``yield`` and only then records ``driver_closed``, as the real
  lifespan must before it closes the database driver.

Every state-changing step is appended, flushed, to a journal (``DRAIN_APP_JOURNAL``) so that the test can read what
happened after the process is gone. Nothing here is for production: the module lives under ``tests/``.
"""

import contextlib
import json
import os
import threading
import time
from pathlib import Path

import anyio
import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from semigraph.serve.drain import DRAIN, LIFESPAN_IDLE_WAIT_S, Drain

MSG_DRAINING = "draining: try again in a minute"
DELTA_S = 0.1                 # the simulated model's time per delta
SHORT_DELTAS = 30             # 3 s
FINALIZE_S = 0.2              # the simulated ledger write of finalize
PING_S = 15


class Journal:
    """Timestamped markers, kept in memory and (when ``path`` is given) appended to a file, flushed at once."""

    def __init__(self, path: str = ""):
        self._lock = threading.Lock()
        self._file = open(path, "a", encoding="utf-8") if path else None
        self.entries: list[dict] = []

    def note(self, name: str, **fields) -> None:
        entry = {"t": time.time(), "event": name, **fields}
        with self._lock:
            self.entries.append(entry)
            if self._file is not None:
                self._file.write(json.dumps(entry, default=str) + "\n")
                self._file.flush()

    def names(self) -> list[str]:
        with self._lock:
            return [e["event"] for e in self.entries]


class DrainedStream:
    """One paid ask as the wiring will do it: counted when it starts, released by ``finalize``."""

    def __init__(self, mode: str, drain: Drain, journal: Journal):
        self._mode, self._drain, self._journal = mode, drain, journal
        self._counted = self._finalized = False

    async def events(self):
        if not self._drain.try_enter():
            self._journal.note("stream_refused")
            yield self._event("error", {"event": "error", "detail": MSG_DRAINING})
            return
        self._counted = True
        self._journal.note("stream_start", mode=self._mode)
        try:
            async for event in self._deltas():
                yield event
            self._journal.note("ledger_row")                      # the row exists before the client sees ``done``
            yield self._event("done", {"event": "done"})
        except BaseException as exc:
            self._journal.note("stream_cancelled", kind=type(exc).__name__)
            raise
        await self.finalize()

    async def _deltas(self):
        count = SHORT_DELTAS if self._mode == "short" else None
        i = 0
        while count is None or i < count:
            await anyio.sleep(DELTA_S)
            yield self._event("delta", {"event": "delta", "i": i})
            i += 1

    @staticmethod
    def _event(name: str, data: dict) -> ServerSentEvent:
        return ServerSentEvent(data=json.dumps(data), event=name, sep="\n")

    async def finalize(self) -> None:
        """Idempotent and shielded: the (slow) ledger write, then ``leave`` as the very last step."""
        if self._finalized:
            return
        self._finalized = True
        if not self._counted:
            return
        with anyio.CancelScope(shield=True):
            self._journal.note("finalize_start")
            try:
                await anyio.to_thread.run_sync(time.sleep, FINALIZE_S)
                self._journal.note("finalize_done")
            finally:
                self._drain.leave()


class DrainedResponse(EventSourceResponse):
    """``PaidResponse``'s shape: ``finalize`` as the background task AND in a ``finally`` around the ASGI call."""

    def __init__(self, stream: DrainedStream, grace_s: float = 0):
        super().__init__(stream.events(), ping=PING_S, sep="\n", background=BackgroundTask(stream.finalize),
                         shutdown_grace_period=grace_s)
        self._drained_stream = stream

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._drained_stream.finalize()


def create_app(drain: Drain = DRAIN, journal: Journal | None = None) -> FastAPI:
    """The app over ``drain`` (the process-wide ``DRAIN`` for the server; a private ``Drain`` for in-process tests)."""
    journal = journal or Journal(os.environ.get("DRAIN_APP_JOURNAL", ""))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        journal.note("started")
        yield
        idle = await drain.await_idle(LIFESPAN_IDLE_WAIT_S)
        journal.note("driver_closed", idle=idle, active=drain.active)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/ask")
    async def ask(mode: str = "short", grace: float = 0):
        if drain.draining:
            return JSONResponse({"detail": MSG_DRAINING}, status_code=503, headers={"Retry-After": "30"})
        return DrainedResponse(DrainedStream(mode, drain, journal), grace)

    @app.get("/read")
    async def read() -> dict:
        return {"read": True}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/_test/state")
    async def state(request: Request) -> dict:
        return {"draining": drain.draining, "active": drain.active, "pid": os.getpid()}

    return app


app = create_app()


def journal_events(path: Path) -> list[dict]:
    """The markers a finished server process left behind."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
