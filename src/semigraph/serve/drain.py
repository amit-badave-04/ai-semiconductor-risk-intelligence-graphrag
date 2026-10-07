"""The SIGTERM drain (M5a I4, docs/v2/M5A_BUILD_PLAN.md item D, option b; decision 2.2 of docs/v2/M5_DECISIONS.md).

``python -m semigraph.serve.drain`` (the Dockerfile CMD) runs the web service under :class:`DrainingServer`. On the
FIRST termination signal (Fly sends SIGTERM, ``fly.toml`` ``kill_signal``; Ctrl+C and Ctrl+Break count the same):

1. ``DRAIN.begin()``: from now on ``DRAIN.draining`` is true and ``DRAIN.try_enter()`` refuses. The routes answer 503
   to a new paid ask or upload; reads, ``/healthz`` and evidence keep answering, because
2. the LISTENER STAYS OPEN. uvicorn is not told to exit yet: stock uvicorn closes its listener the moment it has the
   signal, which no route gate can undo.
3. Streams and upload jobs that were already running finish. The drain ends, on the main loop's 100 ms tick, as soon
   as ``DRAIN.active`` is 0 or after ``DRAIN_TIMEOUT_S`` (default 240 s), whichever comes first. Only then does the
   server set ``should_exit``, tell sse-starlette to end whatever is still streaming (those streams run their cleanup,
   so the ledger row of an abandoned ask is written) and let uvicorn shut down (close the listener, wait for the
   connections and tasks, run the lifespan shutdown).
4. A SECOND signal exits without waiting: streams are ended and ``force_exit`` is set (uvicorn then skips its wait
   loops AND THE WHOLE LIFESPAN SHUTDOWN: ``Server.shutdown`` runs ``lifespan.shutdown()`` only ``if not
   self.force_exit``, so there is no settle flush, no maintenance stop and no driver close; a settle that was lost is
   charged at its estimate by the next boot). When the signal lands during the drain, the shutdown is also capped at
   ``FORCED_SHUTDOWN_S``; when it lands after uvicorn's ``shutdown()`` has started, that call has already read its
   timeout, so only the ending of the streams and ``force_exit`` apply.

The exit code is 0 when the drain ended because nothing was active, 1 when streams had to be cut or the operator
insisted. uvicorn's habit of re-raising the signal at exit (status 143, or 3 on Windows) is dropped on purpose:
``handle_exit`` never records the signal.

Budget. uvicorn's graceful-shutdown clock starts only at ``should_exit``, i.e. AFTER the drain, so the 240 s drain and
uvicorn's ``timeout_graceful_shutdown`` (``DRAIN_TIMEOUT_S + 10``, the ceiling) do not fit in ``kill_timeout`` (300 s,
Fly's maximum) together. And ``timeout_graceful_shutdown`` covers only the wait for connections and request tasks:
``Server.shutdown`` runs ``lifespan.shutdown()`` AFTER it, which is ``LIFESPAN_SHUTDOWN_BUDGET_S`` more at its worst.
When the drain ends, the window is cut to what is left of the kill timeout minus ``SHUTDOWN_MARGIN_S`` and that
lifespan budget: an early end keeps the whole 250 s, a timeout at 240 s leaves 15 s for the cut streams to finalize
(240 + 15 + 35 + 10 = 300); the lifespan then waits up to ``LIFESPAN_IDLE_WAIT_S`` more for stragglers. ``fly.toml`` pins
``kill_signal = "SIGTERM"`` and ``kill_timeout = 300``; a test checks both, derives the lifespan's worst case from the
constants of the code that sets each bound, and sums the whole chain against the kill timeout.

What the code under it does (observed: uvicorn 0.52.4, sse-starlette 3.4.11, Python 3.13 on Windows, 3.12 on Linux):

* uvicorn registers ``signal.signal(sig, server.handle_exit)`` for SIGINT and SIGTERM (and SIGBREAK on Windows) on the
  main thread. ``handle_exit`` sets ``should_exit``, and ``force_exit`` only on a repeated SIGINT, never on SIGTERM or
  SIGBREAK. Once ``should_exit`` is set, the main loop (a 100 ms tick) ends and ``shutdown()`` closes the listener at
  once, waits for connections and request tasks until ``timeout_graceful_shutdown`` (none by default: for ever),
  cancels what is left, and only then runs the lifespan shutdown. Finally it re-raises the captured signal.
* sse-starlette imports uvicorn and REPLACES ``uvicorn.Server.handle_exit`` at class level with
  ``AppStatus.handle_exit``, which sets the class attribute ``AppStatus.should_exit`` and then calls the original.
  Every ``EventSourceResponse`` waits on that flag. A watcher task polls it every 0.5 s, and also polls
  ``signal.getsignal(SIGTERM).__self__.should_exit``, the uvicorn server's own flag. When it is set, each response
  waits ``shutdown_grace_period`` seconds (default 0) for its generator to finish and then cancels it: the client
  sees a chunked body that never ends and no ``done``, the twin gets ``CancelledError`` and ``PaidResponse`` runs
  ``finalize``. So under stock uvicorn EVERY paid stream is cut at SIGTERM unless ``shutdown_grace_period`` is
  passed, and the listener is closed whatever is passed (``tests/test_serve_drain.py`` runs both).
* :class:`DrainingServer` overrides ``handle_exit`` and never calls ``super().handle_exit``, so sse-starlette's
  replacement does not run, and it keeps ``should_exit`` false, so the introspection fallback sees nothing either. A
  stream is therefore NOT cut during the drain, with the default ``shutdown_grace_period`` of 0. When the drain ends,
  :func:`stop_sse_streams` sets ``AppStatus.should_exit`` directly (the 0.5 s poll would notice ``should_exit`` too,
  but this does not depend on it); with grace 0 the streams still running are cancelled at once and ledgered.
* The global switches of sse-starlette are the class attributes ``AppStatus.should_exit``,
  ``AppStatus.enable_automatic_graceful_drain`` (``AppStatus.disable_automatic_graceful_drain()``) and
  ``AppStatus.original_handler``. Nothing here touches the last two.

THE WIRING (what the service does with this module; ``tests/test_serve_state_wiring.py`` and
``tests/test_serve_drain.py`` pin it):

* ``PaidResponse`` (an ``EventSourceResponse``) gets NO ``shutdown_grace_period``: it stays 0. The drain tells
  sse-starlette nothing until it ends, so a positive value would stack on top of the drain: the streams still running at
  the drain timeout would get that long again, past kill_timeout, and their ledger rows would not be written.
* ``routes.ask`` refuses new paid work with 503 ``MSG_DRAINING`` (``Retry-After: 30``) at two points. Early: a workspace
  ask, which is always paid, is refused before it takes any window; a public ask reads the answer cache first while
  draining, and only a miss is refused (a hit is still served and takes the free window, a refusal takes none). Again,
  immediately before ``state.reserve``: ``DRAIN.try_enter()`` in ``routes._admit`` checks and counts in one step, so a
  drain that begins in between either waits for that ask or refuses it. Without it a drain that began and ended (idle)
  between the early check and the stream would make sse-starlette cancel the new response at once: a reservation with
  no stream. A refusal, an exception or a cancellation inside ``_admit`` gives the count back (``DRAIN.leave()``).
* ``stream_runtime.PaidStream`` owns the count, with the lease, from then on. ``finalize()`` calls ``DRAIN.leave()`` as
  its LAST step, in a ``finally``, after the lease is settled, the model stream is closed and the tracer is closed, so
  ``DRAIN.active == 0`` means the ledger row exists and the stream is closed. ``finalize`` is idempotent and shielded.
* Uploads: ``workspace_routes.DrainCountedSlot`` (``app.state.upload_slots``) counts an upload on the drain from
  ``acquire`` (``DRAIN.try_enter()``: a drain that began during the body read refuses it, 503) until its job thread's
  ``release`` (``DRAIN.leave()``). Creating a workspace and uploading are refused with 503 while draining, before any
  window. Reads (stats, evidence, examples, ``/healthz``, workspace GETs) and cached answers keep being served.
* ``main.lifespan``, after ``yield``: ``await DRAIN.await_idle(LIFESPAN_IDLE_WAIT_S)`` (``wait_for_the_drain``) runs
  BEFORE the state maintenance thread stops (it still retries and flushes failed settles), the monitor and the sweeper
  stop, and the database drivers close: a stream's shielded cleanup would otherwise race ``driver.close()``. The drain
  above has normally emptied the count already; this wait is for stragglers.

``DRAIN`` is one object per process. Run as ``python -m``, this file is ``__main__`` and the application imports it
again as ``semigraph.serve.drain``: the guard at the bottom re-imports the canonical module, so the server and the
routes share one ``DRAIN`` (a test starts the real entry point to prove it).

Everything that runs inside a signal handler is an assignment or ``DRAIN.begin()`` (an ``RLock``: the handler runs on
the thread that may be inside ``enter`` or ``leave``): no logging, no thread, no ``Event``. The logging happens on
the next tick.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from types import FrameType

import anyio
import anyio.lowlevel
import uvicorn
from uvicorn.config import STARTUP_FAILURE

from ..config import get_settings

try:
    from sse_starlette.sse import AppStatus
except ImportError:  # pragma: no cover - a serving dependency; without it there is nothing to stop
    AppStatus = None

logger = logging.getLogger("uvicorn.error")      # uvicorn configures this one: the lines show up beside "Shutting down"

DEFAULT_APP = "semigraph.serve.main:app"
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - the container's listener; Fly's proxy reaches it over the private network
DEFAULT_PORT = 8080
DEFAULT_DRAIN_TIMEOUT_S = 240.0  # Settings.drain_timeout_s (env DRAIN_TIMEOUT_S) is the one source; a test pins the default
KILL_TIMEOUT_S = 300.0           # fly.toml kill_timeout, Fly's maximum; tests/test_serve_drain.py pins the two together
SHUTDOWN_MARGIN_S = 10.0         # uvicorn's graceful timeout is drain + this; the kill timeout is kept this far away
MIN_SHUTDOWN_S = 1.0             # the floor of the shutdown window after a late drain end
FORCED_SHUTDOWN_S = 2.0          # the shutdown window after a second signal
LIFESPAN_IDLE_WAIT_S = 10.0      # what the lifespan shutdown waits for stragglers (uvicorn has waited for tasks)
# The worst case of ``main.lifespan`` after ``yield``, which uvicorn runs AFTER its graceful-shutdown window: the idle wait
# above 10 s + maintenance join 5 s + settle flush 3 s and the one state operation that can overrun it (1.5 s: the
# server-side timeout and a connection attempt) + monitor stop 5 s + sweeper stop 5 s + tracer 3 s = 32.5 s, and the rest
# for the two driver closes. tests/test_serve_drain.py derives the sum from those bounds in the code that sets them and
# fails when one grows past this. A raised STATE_OP_TIMEOUT_S spends the slack (the budget assumes its default of 1 s).
LIFESPAN_SHUTDOWN_BUDGET_S = 35.0
AWAIT_IDLE_POLL_S = 0.05


class Drain:
    """The process-wide state of a drain: how many paid streams and uploads are running, and whether new ones are
    refused. Thread-safe. ``begin`` is called from a signal handler, so the lock is re-entrant (a plain lock would
    deadlock when the signal lands inside ``enter`` or ``leave``)."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._active = 0
        self._began_at: float | None = None

    def begin(self) -> bool:
        """Start refusing new work. Idempotent; True only for the call that started it (it records the start time)."""
        with self._cond:
            if self._began_at is not None:
                return False
            self._began_at = time.monotonic()
            return True

    @property
    def draining(self) -> bool:
        return self._began_at is not None

    @property
    def began_at(self) -> float | None:
        """``time.monotonic()`` when the drain began, or None."""
        return self._began_at

    @property
    def active(self) -> int:
        return self._active

    def enter(self) -> None:
        """Count one running paid stream or upload, drain or not (it was admitted before the drain began)."""
        with self._cond:
            self._active += 1

    def try_enter(self) -> bool:
        """Count one new stream or upload unless the drain has begun. The check and the count are one step: a drain
        that begins right after a True answer waits for this one, and one that began before it refuses it."""
        with self._cond:
            if self._began_at is not None:
                return False
            self._active += 1
            return True

    def leave(self) -> None:
        """Uncount one. Never goes below 0 (an unbalanced call is logged: it could hide a running stream)."""
        with self._cond:
            if self._active == 0:
                logger.warning("DRAIN.leave() without a matching enter(): the count stays at 0")
                return
            self._active -= 1
            if self._active == 0:
                self._cond.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block the calling THREAD until nothing is active (True) or ``timeout`` seconds pass (False)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._active == 0, timeout)

    async def await_idle(self, timeout: float | None = None) -> bool:
        """The same for the event loop. It polls instead of parking a thread, so it holds no thread and no limiter and
        a cancellation leaves nothing behind. It always yields to the loop at least once."""
        await anyio.lowlevel.checkpoint()
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._active:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            await anyio.sleep(AWAIT_IDLE_POLL_S if remaining is None else min(AWAIT_IDLE_POLL_S, remaining))
        return True


DRAIN = Drain()


def stop_sse_streams() -> None:
    """Tell sse-starlette the server is shutting down: every ``EventSourceResponse`` still streaming is cancelled once
    its ``shutdown_grace_period`` (0 for ours) is over, and its cleanup runs."""
    if AppStatus is not None:
        AppStatus.should_exit = True


def graceful_timeout_s(drain_timeout_s: float) -> float:
    """uvicorn's ``timeout_graceful_shutdown``: larger than the drain, and the ceiling of the shutdown window."""
    return drain_timeout_s + SHUTDOWN_MARGIN_S


def post_drain_window_s(elapsed_s: float, graceful_s: float) -> float:
    """How long uvicorn may wait for connections and tasks when the drain ended ``elapsed_s`` after the first signal: its
    graceful timeout, but never past ``KILL_TIMEOUT_S - SHUTDOWN_MARGIN_S - LIFESPAN_SHUTDOWN_BUDGET_S`` counted from that
    signal (the lifespan shutdown runs after this window, not inside it), and never under the floor."""
    return max(MIN_SHUTDOWN_S, min(graceful_s, KILL_TIMEOUT_S - SHUTDOWN_MARGIN_S - LIFESPAN_SHUTDOWN_BUDGET_S - elapsed_s))


class DrainingServer(uvicorn.Server):
    """uvicorn's server with the drain of the module docstring. ``drain``, ``end_streams`` and ``clock`` are seams for
    tests; production uses the process-wide ``DRAIN``, :func:`stop_sse_streams` and ``time.monotonic``."""

    def __init__(self, config: uvicorn.Config, *, drain: Drain = DRAIN,
                 drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
                 end_streams: Callable[[], None] = stop_sse_streams, clock: Callable[[], float] = time.monotonic):
        super().__init__(config)
        self.drain, self.drain_timeout_s = drain, drain_timeout_s
        self.end_streams, self._clock = end_streams, clock
        self.exit_reason = ""                    # "", "idle", "timeout" or "forced"
        self._first_signal: int | None = None
        self._signalled_at = 0.0
        self._announced = self._forced_announced = False

    @property
    def exit_code(self) -> int:
        """0 unless streams had to be cut at the drain timeout or the operator insisted with a second signal."""
        return 0 if self.exit_reason in ("", "idle") else 1

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """The signal handler. The first signal begins the drain and nothing else (see the module docstring); a second
        one forces the exit. It does NOT call ``Server.handle_exit``: that one (replaced by sse-starlette's) would set
        ``should_exit`` at once, cut every stream and record the signal for a re-raise at exit."""
        if self._first_signal is not None:
            self._force()
            return
        self._signalled_at = self._clock()
        self._first_signal = sig
        self.drain.begin()

    def _force(self) -> None:
        self.exit_reason = self.exit_reason or "forced"
        self.config.timeout_graceful_shutdown = FORCED_SHUTDOWN_S
        self.end_streams()
        self.force_exit = True
        self.should_exit = True

    async def on_tick(self, counter: int) -> bool:
        """uvicorn's main loop calls this every 100 ms: it is the drain's watcher."""
        self._announce()
        if self._first_signal is not None and not self.should_exit:
            self._check_drain()
        return await super().on_tick(counter)

    def _check_drain(self) -> None:
        if self.drain.active == 0:
            self._finish("idle")
        elif self._clock() - self._signalled_at >= self.drain_timeout_s:
            self._finish("timeout")

    def _finish(self, reason: str) -> None:
        elapsed = self._clock() - self._signalled_at
        graceful = self.config.timeout_graceful_shutdown or graceful_timeout_s(self.drain_timeout_s)
        self.exit_reason = reason
        self.config.timeout_graceful_shutdown = post_drain_window_s(elapsed, graceful)
        if reason == "timeout":
            logger.warning("drain timed out after %.0f s: cutting %d stream(s) or upload(s) still active",
                           elapsed, self.drain.active)
        else:
            logger.info("drain finished after %.1f s: nothing is active; shutting down", elapsed)
        self.end_streams()
        self.should_exit = True

    def _announce(self) -> None:
        if self._first_signal is not None and not self._announced:
            self._announced = True
            logger.info("%s received: draining (new paid asks and uploads get 503, reads are served); %d active, "
                        "waiting up to %.0f s", signal.Signals(self._first_signal).name, self.drain.active,
                        self.drain_timeout_s)
        if self.exit_reason == "forced" and not self._forced_announced:
            self._forced_announced = True
            logger.warning("second signal: exiting without waiting for the %d active", self.drain.active)


def build_config(app: str, host: str, port: int, drain_timeout_s: float, loop: str = "auto") -> uvicorn.Config:
    """The configuration ``uvicorn <app> --host <host> --port <port>`` builds, plus the graceful timeout. Every other
    option keeps ``uvicorn.Config``'s default, which is the command line's (a test compares them). ``loop`` is "auto"
    (asyncio's loop in the image: uvloop is not in its requirements); only a test passes another."""
    return uvicorn.Config(app, host=host, port=port, loop=loop,
                          timeout_graceful_shutdown=graceful_timeout_s(drain_timeout_s))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m semigraph.serve.drain", description=__doc__.splitlines()[0])
    parser.add_argument("--app", default=DEFAULT_APP, help="the ASGI app to serve, as module:attribute")
    parser.add_argument("--app-dir", default=None, help="a directory to put on sys.path first (for the app)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help="default: $PORT, else 8080")
    parser.add_argument("--loop", default="auto", help="uvicorn's event loop implementation")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until a signal ends the drain; returns the process exit code. The tail of ``uvicorn.run`` is mirrored: the
    app is imported before the loop starts, and a start that failed exits with uvicorn's code 3. The drain timeout is
    the validated ``Settings.drain_timeout_s`` (the app's own settings object: the production validators run here too,
    before the app is imported, and a configuration they refuse ends the process with the refusal)."""
    args = _parse_args(argv)
    drain_timeout_s = get_settings().drain_timeout_s
    if args.app_dir:
        sys.path.insert(0, args.app_dir)
    config = build_config(args.app, args.host, args.port, drain_timeout_s, args.loop)
    config.load_app()
    server = DrainingServer(config, drain_timeout_s=drain_timeout_s)
    server.run()
    return server.exit_code if server.started else STARTUP_FAILURE


if __name__ == "__main__":
    # ``python -m`` runs this file as ``__main__``, while the application imports ``semigraph.serve.drain``: a second
    # module with its own DRAIN. Run the canonical one, so the signal handler and the routes share a single DRAIN.
    from semigraph.serve.drain import main as _main

    sys.exit(_main())
