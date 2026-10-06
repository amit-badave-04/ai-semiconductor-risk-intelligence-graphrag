"""The async answer path against a REAL uvicorn on a real socket (M5a I2, docs/v2/M5A_BUILD_PLAN.md sections 4 and 9.1).

``tests/serve_async_app.py`` (run as ``uvicorn serve_async_app:app`` in a subprocess, never imported by production
code) mounts the real ``routes.router`` over the real ``PaidStream`` / ``PaidResponse``, limiters, ``LimitedEmbedder``,
loop-lag monitor, ``InProcessBackend`` and ``MaintenanceThread``; only the database (an in-memory ledger), the model and
the CPU-bound embedder (0.3 s per question) are fakes. The ledger row of a paid ask is its lease: reserved before the
stream starts, settled before the terminal event, so "a row" below means a SETTLED one.
``httpx.ASGITransport`` cannot disconnect mid-stream, a ``TestClient`` shares the test's loop, and a send timeout
needs a client that really stops reading, so these tests talk to a socket. Each one asks the server for its state
(``GET /_test/state``). Every ask sends its own ``x-test-ask`` header: a ledger row never holds the question, so the
row's ``ip_hash`` is the ask's name.

1. 40 concurrent streams (40 distinct questions: 40 real embeddings) all reach ``done`` with the fake's events;
   ``/healthz`` answers in well under a second beside them; the loop-lag monitor stays silent; the thread count stays
   at or below 40 plus a few (the gates' state calls run under the 4-thread state limiter, so starlette's own pool,
   capped at 40, is not used by an ask; the parked-streams test in 2 is what shows a stream holds no thread); every
   ledger row is present exactly once. The 100 ms loop-lag criterion holds for an embedder that releases the GIL (native
   code, like onnxruntime). A pure-Python CPU-bound embedder slows the loop whatever thread it runs on (the GIL is
   shared): its test pins only what must still hold.
2. The money rule over HTTP: with a slow database, the ledger row and the cache write exist by the time the client
   sees ``done``. And 40 streams parked at the model hold a lease each and no thread at all; when all 40
   clients leave at once, each costs exactly one ledger row without usage (settled ``abandoned``, charged its estimate).
3. A client that leaves after the first delta (20 times, alternating a stream that never finishes and one that would
   run 15 s): within 2 s there is exactly one ledger row, without usage, the lease is back, the twin was closed
   by a cancellation, and the model produces no further step.
4. A client that leaves before the first byte (a raw socket, FIN or RST, after 0 to 300 ms): no leaked slot or twin,
   never two rows for one ask, and exactly one row for every reserve the backend granted (some asks leave before the
   route reserved: no row at all).
5. The in-flight cap over HTTP (a second server, ``max_concurrent_answers=1``): the sync path's message as a pre-stream
   HTTP 429 with a JSON body and NO event stream, and no ledger row; five simultaneous asks leave exactly one holder.
   And the per-address daily cap: the 21st paid ask of one address is a 429 with its own message.
6. A client that stops reading (a raw socket with a tiny receive buffer: headers read, then silence past
   ``send_timeout_s=2``): the server drops it on the send timeout, settles the ledger row once and frees the lease. The
   server log holds one warning line for it and no traceback; uvicorn's own one-line ``ASGI callable returned without
   completing response.`` error (no traceback) remains, because the response was started and cannot be completed for a
   client that is not reading.
7. SIGTERM during a stream: under stock uvicorn (what a server not run by ``serve.drain`` does: the stream is cut) and
   under the image's entry point ``python -m semigraph.serve.drain`` (the stream finishes with ``done``, its row is
   settled ``done``, a paid ask that arrives during the drain is a 503 and the process exits 0).
8. The sse-starlette behaviour ``PaidResponse`` relies on, asserted empirically (below).
9. A client that stops reading in the LAST frame: after the ``done`` frame uvicorn's write flow control is paused (as
   its transport does with 64 KiB unsent; ``serve_async_app.PauseWritingAfterDone``), so the closing empty chunk is
   never accepted. The lease is free before the send timeout (``events()`` ends with ``finalize``), a second ask on the
   one-lease server is served, and the response itself ends on the send timeout with one warning line (``PaidResponse``
   bounds the closing send, which sse-starlette leaves unbounded). The servers run ``--loop asyncio``, production's.

What SIGTERM does today (test 7). Windows has no SIGTERM for a subprocess, so the server is started in its own process
group and sent CTRL_BREAK_EVENT, which uvicorn handles as SIGBREAK, as it handles SIGTERM; the test skips, saying so,
if the signal cannot be sent or has no effect. The stream is CUT, not finished: sse-starlette's shutdown hook cancels
every SSE stream once uvicorn has the signal (``shutdown_grace_period`` is 0). The client gets a protocol error from a
chunked body that never ended, the server logs "ASGI callable returned without completing response", and ``done``
never arrives. The cancelled twin closes its upstream stream, ``PaidResponse`` runs ``finalize`` as the background
task, and the ask costs exactly ONE ledger row, settled abandoned (no usage) before the process exits. uvicorn then
re-raises the signal with its default action, so the exit status is the platform's signal status (3 for SIGBREAK on
Windows). Nothing refuses new asks and nothing waits for a stream: uvicorn closes its listener at once. The drain entry
point (``serve.drain``, the image's CMD) changes all of this: the second SIGTERM test below runs the same app under it.

What sse-starlette does when the client disconnects (test 8; ``/_test/order`` is a plain ``EventSourceResponse`` built
like ``PaidResponse``: a generator, a BackgroundTask, a ``finally`` around the ASGI call, a client-close handler).
Observed order, one case per bullet:

* The generator is AWAITING (the model) when the client leaves: ``client_close_handler`` (the ``http.disconnect``
  message was read), then the CancelledError arrives INSIDE the running generator at its ``await``, so its ``except``
  and ``finally`` run (this is how a twin closes its upstream stream), then the BackgroundTask
  (``PaidStream.finalize``), then the ``finally`` around the ASGI call (``finalize`` again: a no-op, it is
  idempotent).

* The generator is SUSPENDED at its ``yield`` (a send is in progress) when the client leaves:
  ``client_close_handler``, the BackgroundTask, the ``finally``; nothing cancels or closes the generator: it is
  dropped, and only the garbage collector closes it (GeneratorExit), at a time of its own after the call has returned
  (not within the 0.5 s watched in one environment, within a few seconds in another). So ``finalize`` closes the twin
  itself with ``aclose()``, and a twin behind a dropped ``events()`` generator is not closed by that. The send-timeout
  path (test 6) is this case and worse: the BackgroundTask does not run at all (the SendTimeoutError skips it), only
  the ``finally`` does, and the twin sees GeneratorExit from ``finalize``'s ``aclose()``.

* The same disconnect through the real ``PaidStream``: twin exit (CancelledError), the background task, the first
  ``finalize`` (it writes the abandoned ledger row), the second ``finalize`` (a no-op).
"""

import asyncio
import contextlib
import json
import os
import signal
import socket
import statistics
import struct
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import httpx
import pytest
from serve_async_app import ASK_HEADER, PAUSE_HEADER, ask_key, expected_events, mode_of

from semigraph.serve.routes import MSG_DRAINING, MSG_IP_BUDGET
from semigraph.serve.stream_runtime import MSG_BUSY

REPO = Path(__file__).resolve().parents[1]
SRC, TESTS = REPO / "src", REPO / "tests"
IS_WINDOWS = sys.platform == "win32"
SERVER_START_TIMEOUT_S = 90.0
SERVER_STOP_GRACE_S = 5.0
SEND_TIMEOUT_S = 2                # the send timeout of the shared and the one-slot server
CLEANUP_BUDGET_S = 2.0            # a disconnected ask is fully cleaned up within this long
QUIET_FOR_S = 0.3
POLL_S = 0.01
STREAMS = 40
ENV_WHITELIST = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME",
                 "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "VIRTUAL_ENV")
PYTHON_SPIN_STREAMS = 20
DISCONNECT_REPEATS = 20
EARLY_ASKS = 16
EARLY_DELAYS_S = (0.0, 0.0, 0.003, 0.01, 0.02, 0.05, 0.1, 0.3)
SIGTERM_EXIT_WAIT_S = 20.0
BODY = "How does a long answer about Nvidia HBM suppliers reach a client"   # one question text: one cached embedding
HANG, LONG, MEDIUM, FAT, NORMAL = (f"[{mode}] {BODY}" for mode in ("hang", "long", "medium", "fat", "normal"))
# What the runs below observed (see the module docstring); the tests pin them.
TIMEOUT_EXIT = "GeneratorExit"
SEND_TIMEOUT_LINE = "client dropped on the send timeout (SendTimeoutError)"      # PaidResponse's one warning line
UVICORN_INCOMPLETE_LINE = "ASGI callable returned without completing response."  # uvicorn's own, one line, no trace
SIGTERM_TWIN_EXIT = "CancelledError"
AWAITING_ORDER = ["generator_start", "client_close_handler", "generator_raised_CancelledError", "generator_finally",
                  "background", "call_finally"]
SUSPENDED_ORDER = ["generator_start", "client_close_handler", "background", "call_finally"]    # then, maybe, a GC close
PAID_ORDER = ["twin_start", "twin_exit", "background_start", "finalize_start", "ledger", "finalize_end",
              "background_end", "finalize_start", "finalize_end"]


# ---- the server process

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Server:
    """One ``uvicorn serve_async_app:app`` subprocess on 127.0.0.1 (``settings`` become ``ASYNC_APP_*`` variables). Its
    working directory is a temporary one (nothing the app imports can read the repository's ``.env``) and its
    environment holds no secrets: a whitelist of what an interpreter needs, plus ``PYTHONPATH``."""

    def __init__(self, workdir: Path, *, drained: bool = False, **settings: object):
        self.drained = drained             # start it as the image does (``python -m semigraph.serve.drain``)
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = workdir / f"server-{self.port}.log"
        self.journal_path = workdir / f"journal-{self.port}.jsonl"
        self.pid = 0                       # the interpreter that serves (a venv launcher may sit in front of it)
        self.loop = ""                     # the module of its event loop's class (``/_test/ready`` says)
        self.workdir = workdir
        self.env = {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}
        self.env.update({"PYTHONPATH": str(SRC), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                         "LITELLM_LOCAL_MODEL_COST_MAP": "True", "ASYNC_APP_JOURNAL": str(self.journal_path)})
        self.env.update({f"ASYNC_APP_{k.upper()}": str(v) for k, v in settings.items()})
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Server":
        # ``--loop asyncio``: production's loop (its requirements dropped uvloop). ``auto`` would pick uvloop wherever
        # it is installed (the unit job has it, ``serve-shipped`` does not) and the two jobs would test different loops.
        command = [sys.executable, "-m", "uvicorn", "serve_async_app:app", "--app-dir", str(TESTS),
                   "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning", "--loop", "asyncio"]
        if self.drained:
            command = [sys.executable, "-m", "semigraph.serve.drain", "--app", "serve_async_app:app", "--app-dir",
                       str(TESTS), "--host", "127.0.0.1", "--port", str(self.port), "--loop", "asyncio"]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(command, cwd=self.workdir, env=self.env, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=flags)
        self._wait_ready()
        return self

    def log_tail(self, chars: int = 3000) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-chars:] if self.log_path.exists() else ""

    def log_size(self) -> int:
        """Bytes in the server log so far: a mark for ``log_since`` (the log is shared by every test of a module)."""
        return len(self.log_path.read_bytes()) if self.log_path.exists() else 0

    def log_since(self, mark: int) -> str:
        return self.log_path.read_bytes()[mark:].decode("utf-8", errors="replace") if self.log_path.exists() else ""

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the server exited with {self.proc.returncode} before it was ready:\n"
                                   f"{self.log_tail()}")
            try:
                reply = httpx.get(f"{self.base}/_test/ready", timeout=1.0, trust_env=False)
                self.pid, self.loop = reply.json()["pid"], reply.json()["loop"]
                return
            except (httpx.TransportError, ValueError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"the server was not ready in {SERVER_START_TIMEOUT_S} s:\n{self.log_tail()}")

    def stop(self) -> int | None:
        """Terminate (a hard stop on Windows), kill after ``SERVER_STOP_GRACE_S``; assert nothing is left running."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=SERVER_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=SERVER_STOP_GRACE_S)
        assert proc is None or proc.poll() is not None, "the server process is still running"
        assert not (self.pid and _pid_alive(self.pid)), f"the server interpreter {self.pid} is still running"
        assert not _port_open(self.port), f"something still listens on port {self.port}"
        return None if proc is None else proc.returncode

    def state(self) -> dict:
        return httpx.get(f"{self.base}/_test/state", timeout=5.0, trust_env=False).json()

    def reset(self) -> dict:
        return httpx.post(f"{self.base}/_test/reset", timeout=5.0, trust_env=False).json()

    def journal(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        return [json.loads(line) for line in self.journal_path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def main_server(tmp_path_factory):
    """The shared server: room for 40 asks at once, a 2 s send timeout, one embedding at a time (the production
    ``embed_slots``), four database threads. Every test starts from a quiet server with zeroed counters."""
    server = Server(tmp_path_factory.mktemp("serve_async"), max_answers=STREAMS, send_timeout_s=SEND_TIMEOUT_S,
                    embed_slots=1, db_threads=4).start()
    yield server
    server.stop()


@pytest.fixture
def server(main_server):
    wait_until_quiet(main_server)
    main_server.reset()
    return main_server


# ---- observing the server

def is_quiet(state: dict) -> bool:
    return (state["inflight_leases"] == 0 and state["reserved_rows"] == 0 and state["state_limiter_borrowed"] == 0
            and state["db_limiter_borrowed"] == 0 and state["embed_limiter_borrowed"] == 0
            and state["twins_started"] == state["upstream_closed"])


def wait_until_quiet(server: Server, timeout: float = 10.0) -> dict:
    """Poll until nothing is held and every twin was closed, for ``QUIET_FOR_S`` in a row."""
    deadline, quiet_since, state = time.monotonic() + timeout, None, {}
    while time.monotonic() < deadline:
        state = server.state()
        if is_quiet(state):
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= QUIET_FOR_S:
                return state
        else:
            quiet_since = None
        time.sleep(0.05)
    raise AssertionError(f"the server did not go quiet in {timeout} s: {json.dumps(state, default=str)[:1500]}")


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60.0, connect=10.0),
                             limits=httpx.Limits(max_connections=200, max_keepalive_connections=0))


async def astate(client: httpx.AsyncClient, base: str) -> dict:
    return (await client.get(f"{base}/_test/state")).json()


async def wait_for(client: httpx.AsyncClient, base: str, ready: Callable[[dict], bool],
                   timeout: float = CLEANUP_BUDGET_S) -> tuple[dict, float]:
    """Poll the state every ``POLL_S`` until ``ready(state)``; returns the state and the seconds it took (the clock
    starts when called). Fails with the last state when it does not hold in ``timeout`` seconds."""
    started, state = time.perf_counter(), {}
    while time.perf_counter() - started < timeout:
        state = await astate(client, base)
        if ready(state):
            return state, time.perf_counter() - started
        await asyncio.sleep(POLL_S)
    raise AssertionError(f"not reached in {timeout} s: {json.dumps(state, default=str)[:1800]}")


def new_tag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def rows_of(state: dict, tag: str) -> list[dict]:
    return [r for r in state["ledger_rows"] if r["ip_hash"] == ask_key(tag)]


class Events:
    """Reads an SSE response one event at a time: ``(event name, data)``, comments (the keep-alive pings) skipped. It
    keeps its line iterator alive: a collected ``aiter_lines()`` generator closes the response behind the caller's
    back."""

    def __init__(self, response: httpx.Response):
        self._lines, self._name = response.aiter_lines(), None

    async def next(self) -> tuple[str, dict] | None:
        async for line in self._lines:
            if line.startswith("event:"):
                self._name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                return self._name, json.loads(line[len("data:"):])
        return None

    async def until(self, name: str) -> list[tuple[str, dict]]:
        """The events up to and including the first one called ``name``; the stream ending first is a failure."""
        seen = []
        while (item := await self.next()) is not None:
            seen.append(item)
            if item[0] == name:
                return seen
        raise AssertionError(f"the stream ended before a {name!r} event: {seen}")

    async def rest(self) -> list[tuple[str, dict]]:
        return [item async for item in self._drain()]

    async def _drain(self):
        while (item := await self.next()) is not None:
            yield item


def ask_request(client: httpx.AsyncClient, base: str, question: str, tag: str,
                extra_headers: dict[str, str] | None = None) -> AbstractAsyncContextManager:
    """The streaming ask; ``extra_headers`` are sent beside the ask's own."""
    return client.stream("POST", f"{base}/api/ask", json={"question": question},
                         headers={ASK_HEADER: tag, **(extra_headers or {})})


async def ask_all(client: httpx.AsyncClient, base: str, question: str, tag: str) -> list[tuple[str, dict]]:
    async with ask_request(client, base, question, tag) as response:
        assert response.status_code == 200, response.status_code
        return await Events(response).rest()


async def ask_refused(client: httpx.AsyncClient, base: str, question: str, tag: str) -> tuple[int, str, bytes]:
    """An ask the route refuses before any stream: (status, content type, the whole body)."""
    async with ask_request(client, base, question, tag) as response:
        body = await response.aread()
        return response.status_code, response.headers.get("content-type", ""), body


def assert_refused_without_a_stream(refused: tuple[int, str, bytes], status: int, detail: str) -> None:
    """A pre-stream refusal: the status, a JSON body with the message, and not one byte of an event stream."""
    code, content_type, body = refused
    assert code == status and content_type.startswith("application/json"), refused
    assert json.loads(body) == {"detail": detail} and b"event:" not in body and b"data:" not in body, body


async def leave_after_first_delta(base: str, question: str, tag: str) -> None:
    """Ask, read until the first ``delta``, then drop the connection (a fresh client, closed: the socket goes away)."""
    client = new_client()
    try:
        async with ask_request(client, base, question, tag) as response:
            await Events(response).until("delta")
    finally:
        await client.aclose()


def raw_ask_bytes(port: int, question: str, tag: str) -> bytes:
    body = json.dumps({"question": question}).encode()
    head = (f"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
            f"{ASK_HEADER}: {tag}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    return head + body


async def raw_ask(port: int, question: str, tag: str, *, leave_after_s: float, reset: bool) -> None:
    """Send the request on a bare socket and close it ``leave_after_s`` later, before any response byte is read.
    ``reset`` closes with an RST (``SO_LINGER`` 0) instead of a FIN."""
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw_ask_bytes(port, question, tag))
    await writer.drain()
    await asyncio.sleep(leave_after_s)
    if reset:
        writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


# ---- 1: forty concurrent streams

async def _burst(server: Server, count: int, topic: str) -> dict:
    """Warm up, zero the counters, then run ``count`` asks of distinct questions at once (distinct, and distinct from
    every other burst's ``topic``, so every one is a real 0.3 s embedding: the vector cache cannot collapse them),
    with a ``/healthz`` prober beside them."""
    base = server.base
    questions = [f"Which suppliers does company number {i} depend on for {topic} capacity?" for i in range(count)]
    tags = [new_tag("many") for _ in questions]
    async with new_client() as client, new_client() as probe:
        await ask_all(client, base, "Warm up the first request through every route of the service", new_tag("warm"))
        await wait_for(client, base, is_quiet)
        await client.post(f"{base}/_test/reset")
        health, running = [], True

        async def prober() -> None:
            while running:
                started = time.perf_counter()
                reply = await probe.get(f"{base}/healthz")
                health.append((reply.status_code, time.perf_counter() - started))
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(prober())
        started = time.perf_counter()
        results = await asyncio.gather(*(ask_all(client, base, q, t) for q, t in zip(questions, tags, strict=True)))
        wall_s = time.perf_counter() - started
        running = False
        await watcher
        state, _ = await wait_for(client, base, lambda s: is_quiet(s) and s["finalized"] == count, timeout=5.0)
    return {"questions": questions, "tags": tags, "results": results, "health": health, "state": state,
            "wall_s": wall_s}


def _check_burst(run: dict) -> float:
    """Everything a burst must get right whatever the embedder; returns the worst ``/healthz`` time."""
    count, state = len(run["questions"]), run["state"]
    for question, events in zip(run["questions"], run["results"], strict=True):
        assert [data for _name, data in events] == expected_events(question)
        assert [name for name, _data in events] == [d["event"] for d in expected_events(question)]
    health = run["health"]
    assert len(health) >= 5 and {status for status, _ in health} == {200}, health
    worst_health = max(seconds for _status, seconds in health)
    assert worst_health < 1.0, f"/healthz took {worst_health:.2f} s while {count} streams ran"
    # The most threads a burst can have: starlette's pool (capped at 40; the route's gate store calls use it) plus the
    # named limiters (4 + 1 + 1) and the process's own. This bound alone would not tell a thread per stream from this
    # design (the old streams came from the same 40-thread pool): the parked-streams test below does.
    assert state["peak_thread_count"] <= 40 + 8, state["peak_thread_count"]
    assert state["embed_calls"] == count and state["embed_concurrent_max"] == 1
    assert state["ledger_row_count"] == count and state["cache_puts"] == count, state["ledger_row_count"]
    per_ask = Counter(r["ip_hash"] for r in state["ledger_rows"])
    assert per_ask == Counter(ask_key(t) for t in run["tags"]), "an ask has no ledger row, or has two"
    assert all(r["usage"] and r["cost_usd"] is not None and r["outcome"] == "done" for r in state["ledger_rows"])
    assert state["twins_started"] == state["upstream_closed"] == count
    assert state["twin_exits"] == {"completed": count}
    assert is_quiet(state)
    return worst_health


def _record_burst(record_property, run: dict, worst_health: float) -> None:
    state = run["state"]
    record_property("max_loop_lag_ms", state["max_loop_lag_ms"])
    record_property("loop_lag_warnings", state["loop_lag_warnings"])
    record_property("peak_thread_count", f"{state['peak_thread_count']} (baseline {state['baseline_thread_count']})")
    record_property("worst_healthz_s", round(worst_health, 3))
    record_property("burst_wall_s", round(run["wall_s"], 2))


def test_forty_concurrent_streams_all_finish_and_the_loop_stays_free(server, record_property):
    """40 streams, each a real 0.3 s CPU-bound embedding in native code that releases the GIL, as onnxruntime does:
    every stream reaches ``done`` with the fake's events, ``/healthz`` stays fast, the loop-lag monitor stays silent.
    (One-off check, not part of the suite because it needs the 1 GB model: the same burst through the repo's real
    ``OnnxBackend``, 2 threads, about 0.94 s per embedding, gave a worst loop lag of 16 ms, no warning, a worst
    ``/healthz`` of 24 ms, at 20 and at 40 streams.)"""
    httpx.post(f"{server.base}/_test/embedder", params={"kind": "native"}, timeout=5.0, trust_env=False)
    run = asyncio.run(_burst(server, STREAMS, "advanced packaging"))
    worst_health = _check_burst(run)
    state = run["state"]
    assert state["loop_lag_warnings"] == 0 and state["max_loop_lag_ms"] < 100, state["max_loop_lag_ms"]
    _record_burst(record_property, run, worst_health)


def test_a_pure_python_embedder_slows_the_loop_but_every_stream_still_finishes(server, record_property):
    """The pessimistic stand-in: the same 0.3 s embedding as a pure-Python loop (20 streams, to bound the run). A worker
    thread running Python bytecode holds the GIL, so moving it off the loop cannot keep the loop free: the monitor shows
    tens to a couple of hundred ms of lag and warnings (measured: 120 to 190 ms at 40 streams, 4 ms with no embedding at
    all). What must still hold is correctness and bounded slowdown: every stream finishes with the fake's events,
    ``/healthz`` answers in under a second, every ledger row is there once. The 100 ms criterion is therefore a property
    of the embedder (native code that releases the GIL passes it, test above), not of the wiring."""
    httpx.post(f"{server.base}/_test/embedder", params={"kind": "python"}, timeout=5.0, trust_env=False)
    try:
        run = asyncio.run(_burst(server, PYTHON_SPIN_STREAMS, "wafer fabrication"))
    finally:
        httpx.post(f"{server.base}/_test/embedder", params={"kind": "native"}, timeout=5.0, trust_env=False)
    worst_health = _check_burst(run)
    assert run["state"]["max_loop_lag_ms"] < 500, run["state"]["max_loop_lag_ms"]
    _record_burst(record_property, run, worst_health)


# ---- 2: the money rule, and streams parked at the model

def test_the_ledger_row_and_the_cache_write_exist_when_the_client_sees_done(server):
    asyncio.run(_ledger_before_done(server.base))


async def _ledger_before_done(base: str) -> None:
    """With every ledger write slowed to 150 ms (a slow database), the row must exist the moment ``done`` arrives."""
    question, tag = "How does Nvidia describe its dependence on advanced packaging suppliers?", new_tag("money")
    async with new_client() as client:
        await client.post(f"{base}/_test/ledger-delay", params={"ms": 150})
        try:
            async with ask_request(client, base, question, tag) as response:
                await Events(response).until("done")
                state = await astate(client, base)       # the event has been received: the row must already exist
        finally:
            await client.post(f"{base}/_test/ledger-delay", params={"ms": 0})
    rows = rows_of(state, tag)
    assert len(rows) == 1 and rows[0]["usage"] and rows[0]["cost_usd"] is not None, rows
    assert state["cache_puts"] == 1


def test_forty_streams_waiting_for_the_model_hold_no_thread_and_all_leave_cleanly(server):
    asyncio.run(_forty_waiting(server.base))


async def _forty_waiting(base: str) -> None:
    """40 streams parked at the model (retrieval and one delta delivered, then silence): every one holds a lease and NO
    thread (no named limiter and not starlette's pool), and ``/healthz`` is fast. Then all 40 clients leave at once:
    exactly one ledger row each, settled abandoned without usage, every lease back, every twin closed by a
    cancellation."""
    tags = [new_tag("parked") for _ in range(STREAMS)]
    async with new_client() as probe:
        async with contextlib.AsyncExitStack() as clients:
            readers = []
            for tag in tags:
                client = await clients.enter_async_context(new_client())
                readers.append(Events(await clients.enter_async_context(ask_request(client, base, HANG, tag))))
            await asyncio.gather(*(reader.until("delta") for reader in readers))
            parked = await astate(probe, base)
            await asyncio.sleep(0.3)
            steady = await astate(probe, base)
            started = time.perf_counter()
            healthz = await probe.get(f"{base}/healthz")
            health_s = time.perf_counter() - started
        state, _ = await wait_for(probe, base, lambda s: is_quiet(s) and s["finalized"] == STREAMS, timeout=5.0)
    for seen in (parked, steady):
        assert seen["inflight_leases"] == STREAMS and seen["reserved_rows"] == STREAMS
        threads_held = (seen["db_limiter_borrowed"], seen["embed_limiter_borrowed"], seen["state_limiter_borrowed"],
                        seen["default_limiter_borrowed"])
        assert threads_held == (0, 0, 0, 0)
    assert healthz.status_code == 200 and health_s < 1.0
    assert Counter(r["ip_hash"] for r in state["ledger_rows"]) == Counter(ask_key(t) for t in tags)
    assert all(r["usage"] is None and r["outcome"] == "abandoned" for r in state["ledger_rows"])
    assert state["twin_exits"] == {"CancelledError": STREAMS} and state["cache_puts"] == 0


# ---- 3: a client that leaves after the first delta

def test_a_client_that_leaves_after_the_first_delta_costs_exactly_one_row_and_frees_everything(server, record_property):
    cleanups = asyncio.run(_leave_after_first_delta_repeatedly(server.base))
    record_property("cleanup_after_disconnect_ms_median", round(1000 * statistics.median(cleanups)))
    record_property("cleanup_after_disconnect_ms_max", round(1000 * max(cleanups)))


async def _leave_after_first_delta_repeatedly(base: str) -> list[float]:
    cleanups, tags = [], []
    async with new_client() as client:
        await ask_all(client, base, NORMAL, new_tag("warm"))     # embed the question text once
        await wait_for(client, base, is_quiet)
        for rep in range(DISCONNECT_REPEATS):
            tags.append(new_tag(f"leave{rep}"))
            cleanups.append(await _leave_once(client, base, LONG if rep % 2 else HANG, tags[-1]))
        await asyncio.sleep(0.2)
        state = await astate(client, base)
    ours = Counter(r["ip_hash"] for r in state["ledger_rows"] if r["ip_hash"] in {ask_key(t) for t in tags})
    assert ours == Counter({ask_key(t): 1 for t in tags}), "an ask has no ledger row, or has two"
    assert state["twins_started"] == state["upstream_closed"] == DISCONNECT_REPEATS + 1
    assert state["twin_exits"]["CancelledError"] == DISCONNECT_REPEATS
    assert state["background_calls"] >= DISCONNECT_REPEATS
    assert is_quiet(state)
    return cleanups


async def _leave_once(client: httpx.AsyncClient, base: str, question: str, tag: str) -> float:
    """One ask that leaves after its first delta; returns the seconds until its cleanup was complete. A ``[long]``
    stream would keep producing for 15 s: a model step after cleanup is a paid call after the disconnect."""
    before = await astate(client, base)
    await leave_after_first_delta(base, question, tag)
    left_at = time.perf_counter()
    state, _ = await wait_for(client, base, lambda s: s["finalized"] == before["finalized"] + 1 and is_quiet(s))
    cleanup_s = time.perf_counter() - left_at
    rows = rows_of(state, tag)
    assert len(rows) == 1, f"{len(rows)} ledger rows for one abandoned ask"
    row = rows[0]
    assert row["usage"] is None and row["outcome"] == "abandoned" and row["cached"] is False, row
    assert state["ledger_row_count"] == before["ledger_row_count"] + 1 and state["cache_puts"] == before["cache_puts"]
    assert state["twin_exits"].get("CancelledError", 0) == before["twin_exits"].get("CancelledError", 0) + 1
    assert state["inflight_leases"] == 0
    if mode_of(question) == "long":
        await asyncio.sleep(0.1)
        later = await astate(client, base)
        assert later["model_steps"] == state["model_steps"], "the model kept producing after the client left"
    return cleanup_s


# ---- 4: a client that leaves before the first byte

def test_a_client_that_leaves_before_the_first_byte_leaks_nothing_and_never_costs_two_rows(server):
    asyncio.run(_leave_before_the_first_byte(server))


async def _leave_before_the_first_byte(server: Server) -> None:
    """16 raw-socket asks (alternating FIN and RST) that close 0 to 300 ms after sending: before any response byte, some
    before the route has even run its gates, some while the embedding runs, some after the stream began. Whatever phase
    each one was in: no leaked lease, no leaked twin, at most one row per ask and exactly one per reserve the backend
    granted (an ask that left before the route reserved has no row and no twin; one that left between the reserve and
    the twin has a row and no twin)."""
    tags = [new_tag(f"early{i}") for i in range(EARLY_ASKS)]
    await asyncio.gather(*(raw_ask(server.port, HANG, tag, leave_after_s=EARLY_DELAYS_S[i % len(EARLY_DELAYS_S)],
                                   reset=bool(i % 2)) for i, tag in enumerate(tags)))
    await asyncio.sleep(0.5)                       # a request still in the route's gates has started its stream by now
    async with new_client() as client:
        state, _ = await wait_for(client, server.base, is_quiet, timeout=5.0)
        await asyncio.sleep(0.3)
        later = await astate(client, server.base)
    assert later["ledger_row_count"] == state["ledger_row_count"], "a ledger row appeared after the server was quiet"
    per_ask = Counter(r["ip_hash"] for r in later["ledger_rows"])
    assert set(per_ask) <= {ask_key(t) for t in tags} and max(per_ask.values(), default=0) <= 1, per_ask
    assert later["ledger_row_count"] == later["paid_today"], "a reserve has no settled row, or a row has no reserve"
    assert later["twins_started"] == later["upstream_closed"] <= later["ledger_row_count"], later["twin_exits"]
    assert all(r["usage"] is None for r in later["ledger_rows"]) and later["cache_puts"] == 0
    held = (later["inflight_leases"], later["reserved_rows"], later["db_limiter_borrowed"],
            later["embed_limiter_borrowed"], later["state_limiter_borrowed"])
    assert held == (0, 0, 0, 0, 0)


# ---- 5: the in-flight cap and the per-address daily cap over HTTP

@pytest.fixture(scope="module")
def busy_server(tmp_path_factory):
    server = Server(tmp_path_factory.mktemp("serve_async_busy"), max_answers=1, send_timeout_s=SEND_TIMEOUT_S,
                    embed_slots=1, db_threads=4).start()
    yield server
    server.stop()


@pytest.fixture
def one_slot_server(busy_server):
    wait_until_quiet(busy_server)
    busy_server.reset()
    return busy_server


def test_a_full_cap_answers_429_before_any_stream_without_a_ledger_row_and_the_holder_is_counted_once(one_slot_server):
    asyncio.run(_busy_path(one_slot_server.base))


async def _busy_path(base: str) -> None:
    holder_tag, busy_tags = new_tag("holder"), [new_tag(f"busy{i}") for i in range(4)]
    async with new_client() as client, new_client() as other:
        async with ask_request(client, base, HANG, holder_tag) as held:
            holder = Events(held)
            await holder.until("delta")
            busy = await asyncio.gather(*(ask_refused(other, base, HANG, t) for t in busy_tags))
            during = await astate(other, base)
        await wait_for(other, base, is_quiet)
        again = await ask_all(other, base, "Which suppliers does Nvidia list for advanced packaging capacity?",
                              new_tag("after"))
        state = await astate(other, base)
    for refused in busy:
        assert_refused_without_a_stream(refused, 429, MSG_BUSY)
    assert (during["inflight_leases"], during["twins_started"], during["ledger_row_count"]) == (1, 1, 0), during
    assert during["reserved_rows"] == 1 and during["paid_today"] == 1, during      # a refused ask reserves nothing
    assert [name for name, _ in again][-1] == "done", "the lease was not reusable after the holder left"
    assert len(rows_of(state, holder_tag)) == 1 and rows_of(state, holder_tag)[0]["usage"] is None
    assert not any(rows_of(state, t) for t in busy_tags)
    assert state["ledger_row_count"] == 2 and state["inflight_leases"] == 0 and state["paid_today"] == 2


def test_five_simultaneous_asks_for_one_lease_leave_exactly_one_holder(one_slot_server):
    asyncio.run(_one_slot_five_ways(one_slot_server.base))


async def _one_slot_five_ways(base: str) -> None:
    tags, first, release = [new_tag(f"race{i}") for i in range(5)], asyncio.Queue(), asyncio.Event()

    async def contend(client: httpx.AsyncClient, tag: str) -> None:
        async with ask_request(client, base, HANG, tag) as response:
            if response.status_code != 200:
                body = await response.aread()
                await first.put((tag, response.status_code, response.headers["content-type"], body))
                return
            events = Events(response)
            name, data = await events.next()
            await first.put((tag, 200, name, data))
            await release.wait()                          # the holder keeps the connection until released

    async with new_client() as client, new_client() as watch:
        tasks = [asyncio.create_task(contend(client, tag)) for tag in tags]
        outcomes = [await asyncio.wait_for(first.get(), 30) for _ in tags]
        during = await astate(watch, base)
        release.set()
        await asyncio.gather(*tasks)
        state, _ = await wait_for(watch, base, is_quiet)
    holders = [tag for tag, status, *_rest in outcomes if status == 200]
    refused = [(status, content_type, body) for _tag, status, content_type, body in outcomes if status != 200]
    assert len(holders) == 1 and len(refused) == 4
    for one in refused:
        assert_refused_without_a_stream(one, 429, MSG_BUSY)
    assert (during["inflight_leases"], during["twins_started"], during["ledger_row_count"]) == (1, 1, 0), during
    assert [r["ip_hash"] for r in state["ledger_rows"]] == [ask_key(holders[0])]
    assert state["ledger_rows"][0]["usage"] is None and state["inflight_leases"] == 0 and state["paid_today"] == 1


def test_the_21st_paid_ask_of_one_address_in_a_day_is_a_429_with_its_own_message_and_another_address_is_served(server):
    asyncio.run(_per_address_daily_cap(server.base))


async def _per_address_daily_cap(base: str) -> None:
    tag, other_tag = new_tag("daily"), new_tag("other")
    async with new_client() as client:
        for _ in range(20):
            events = await ask_all(client, base, NORMAL, tag)
            assert [name for name, _ in events][-1] == "done"
        refused = await ask_refused(client, base, NORMAL, tag)
        elsewhere = await ask_all(client, base, NORMAL, other_tag)
        state, _ = await wait_for(client, base, is_quiet)
    assert_refused_without_a_stream(refused, 429, MSG_IP_BUDGET)
    assert [name for name, _ in elsewhere][-1] == "done"
    assert len(rows_of(state, tag)) == 20 and len(rows_of(state, other_tag)) == 1
    assert state["per_ip_max"] == 20 and state["paid_today"] == 21         # the refused one reserved nothing


# ---- 6: a client that stops reading

def test_a_client_that_stops_reading_is_dropped_on_the_send_timeout_and_costs_one_row(server, record_property):
    dropped_after_s = asyncio.run(_stalled_reader(server))
    record_property("send_timeout_drop_s", round(dropped_after_s, 2))


async def _stalled_reader(server: Server) -> float:
    """A raw socket with a 2 KB receive buffer reads the response headers, then nothing. The ``[fat]`` stream (256 KB
    per delta) fills every buffer on the way, the server's send blocks, and the 2 s send timeout drops the client:
    the ``SendTimeoutError`` skips sse-starlette's background task, so the cleanup is ``PaidResponse``'s ``finally``."""
    base, tag = server.base, new_tag("stalled")
    async with new_client() as client:
        await ask_all(client, base, NORMAL, new_tag("warm"))    # embed the question text once
        await wait_for(client, base, is_quiet)
        await client.post(f"{base}/_test/reset")
        log_mark = server.log_size()
        reader, writer = await open_stalled(server.port, FAT, tag)
        try:
            await reader.readuntil(b"\r\n\r\n")           # the headers; the body is never read
            stalled_at = time.perf_counter()
            state, _ = await wait_for(client, base, lambda s: s["finalized"] == 1 and is_quiet(s), timeout=15.0)
            dropped_after_s = time.perf_counter() - stalled_at
            log = await log_after_drop(server, log_mark)   # while the client is still connected
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        healthz = await client.get(f"{base}/healthz")
    assert 1.5 <= dropped_after_s <= 8.0, f"dropped after {dropped_after_s:.2f} s with send_timeout_s=2"
    rows = rows_of(state, tag)
    assert len(rows) == 1, f"{len(rows)} ledger rows for the dropped ask"
    assert rows[0]["usage"] is None and rows[0]["outcome"] == "abandoned"
    assert state["ledger_row_count"] == 1 and state["inflight_leases"] == 0
    assert state["twins_started"] == state["upstream_closed"] == 1 and state["background_calls"] == 0
    assert state["twin_exits"] == {TIMEOUT_EXIT: 1}, state["twin_exits"]
    assert healthz.status_code == 200
    # The drop is one warning line, not an ``Exception in ASGI application`` traceback per slow client. uvicorn still
    # logs its own one-line error (no traceback) because the response was started and never completed: pinned.
    assert "Traceback" not in log, log[-2000:]
    assert log.count(SEND_TIMEOUT_LINE) == 1 and log.count(UVICORN_INCOMPLETE_LINE) == 1, log[-2000:]
    return dropped_after_s


async def log_after_drop(server: Server, mark: int, timeout: float = 5.0) -> str:
    """The server log written since ``mark``, once the drop has been logged: polls until ``PaidResponse``'s warning line
    (or a traceback, which a server without that line writes) shows, then waits ``QUIET_FOR_S`` for what uvicorn logs
    right after the app returns."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = server.log_since(mark)
        if SEND_TIMEOUT_LINE in log or "Traceback" in log:
            break
        await asyncio.sleep(POLL_S)
    await asyncio.sleep(QUIET_FOR_S)
    return server.log_since(mark)


async def open_stalled(port: int, question: str,
                       tag: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connected raw socket with a tiny receive buffer that has sent the ask and will read only what it is told to."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
    reader, writer = await asyncio.open_connection(sock=sock, limit=1024)
    writer.write(raw_ask_bytes(port, question, tag))
    await writer.drain()
    return reader, writer


# ---- 7: SIGTERM during a stream (no drain logic yet)

def send_terminate(server: Server) -> None:
    """SIGTERM on POSIX; on Windows, where a subprocess has no SIGTERM, CTRL_BREAK_EVENT to the server's own process
    group (``CREATE_NEW_PROCESS_GROUP``), which uvicorn handles as SIGBREAK just as it handles SIGTERM."""
    try:
        server.proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
    except (OSError, ValueError) as error:
        pytest.skip(f"the termination signal could not be sent to the server ({error!r})")


def test_sigterm_during_a_stream_under_stock_uvicorn_cuts_it_and_costs_one_row_without_usage(tmp_path_factory):
    """Pins what a termination signal does to a server that is NOT run by ``serve.drain`` (stock uvicorn, which this
    test app is): the stream is cut (no ``done``, a protocol error on the client), the twin is cancelled, and the ask
    costs exactly one ledger row, settled abandoned without usage, written before the server exits. The drain
    (``tests/test_serve_drain.py``, run by the image's CMD) changes this for the real service. Skipped, saying why,
    where the signal cannot be delivered (Windows without a console)."""
    server = Server(tmp_path_factory.mktemp("serve_async_sigterm"), max_answers=4, send_timeout_s=30,
                    embed_slots=1, db_threads=4).start()
    tag = new_tag("sigterm")
    try:
        outcome = asyncio.run(_stream_through_sigterm(server, tag))
    finally:
        returncode = server.stop()
    if outcome["signal_ignored"]:
        pytest.skip("the termination signal was sent but the server did not react to it (no console to deliver it to)")
    ledger = [e for e in server.journal() if e["event"] == "ledger" and e["ip_hash"] == ask_key(tag)]
    exits = [e["outcome"] for e in server.journal() if e["event"] == "twin_exit"]
    assert outcome["events"][-1][0] != "done" and outcome["error"], outcome      # cut, not finished
    assert [name for name, _ in outcome["events"]][:2] == ["retrieval", "delta"]
    assert len(ledger) == 1 and ledger[0]["usage"] is None and ledger[0]["outcome"] == "abandoned", ledger
    assert exits == [SIGTERM_TWIN_EXIT], exits
    assert outcome["exited"], f"the server did not exit within {SIGTERM_EXIT_WAIT_S} s of the signal ({returncode})"


async def _stream_through_sigterm(server: Server, tag: str) -> dict:
    """Open a ``[medium]`` stream (about 2.6 s), read its first delta, signal the server, read on; report what arrived,
    how the connection ended and whether the server reacted to the signal at all."""
    events, error = [], None
    async with new_client() as client:
        try:
            async with ask_request(client, server.base, MEDIUM, tag) as response:
                reader = Events(response)
                while (item := await reader.next()) is not None:
                    events.append(item)
                    if item[0] == "delta" and len(events) == 2:
                        send_terminate(server)
        except (httpx.TransportError, OSError) as exc:
            error = type(exc).__name__
    try:
        await asyncio.to_thread(server.proc.wait, SIGTERM_EXIT_WAIT_S)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    ignored = not exited and error is None and bool(events) and events[-1][0] == "done"
    return {"events": events, "error": error, "exited": exited, "signal_ignored": ignored}


def test_sigterm_during_a_stream_under_the_drain_entry_point_lets_it_finish_and_refuses_the_next_paid_ask(
        tmp_path_factory):
    """The same app started as the image starts the service (``python -m semigraph.serve.drain``), over the REAL
    ``PaidStream`` / ``PaidResponse`` / ``InProcessBackend``: the signal begins the drain, the stream that was running
    ends with ``done`` (not cut), its ledger row is settled ``done`` with its usage (not abandoned), a NEW paid ask
    during the drain is a 503 ``MSG_DRAINING`` with ``Retry-After`` (the listener stays open: ``/healthz`` answers), and
    the process exits 0 once nothing is counted. Skipped, saying why, where the signal cannot be delivered."""
    server = Server(tmp_path_factory.mktemp("serve_async_drain"), drained=True, max_answers=4, send_timeout_s=30,
                    embed_slots=1, db_threads=4).start()
    tag, late_tag = new_tag("drain"), new_tag("late")
    try:
        outcome = asyncio.run(_stream_through_the_drain(server, tag, late_tag))
    finally:
        returncode = server.stop()
    if outcome["signal_ignored"]:
        pytest.skip("the termination signal was sent but the server did not react to it (no console to deliver it to)")
    ledger = [e for e in server.journal() if e["event"] == "ledger" and e["ip_hash"] == ask_key(tag)]
    assert outcome["error"] is None and outcome["events"][-1][0] == "done", outcome          # finished, not cut
    assert [name for name, _ in outcome["events"]][:2] == ["retrieval", "delta"]
    assert [(e["outcome"], bool(e["usage"])) for e in ledger] == [("done", True)], ledger
    assert_refused_without_a_stream(outcome["refused"], 503, MSG_DRAINING)
    assert outcome["retry_after"] == "30" and outcome["healthz"] == 200
    assert outcome["exited"] and returncode == 0, f"the drain did not end in a clean exit: {returncode}"


async def _stream_through_the_drain(server: Server, tag: str, late_tag: str) -> dict:
    """Open a ``[medium]`` stream, read its first delta, signal the server, then ask again until the server refuses (the
    signal is handled a moment after it is sent) and read the first stream to its end."""
    events, error, probe = [], None, {"refused": None, "retry_after": None, "healthz": None}
    async with new_client() as client, new_client() as other:
        try:
            async with ask_request(client, server.base, MEDIUM, tag) as response:
                reader = Events(response)
                while (item := await reader.next()) is not None:
                    events.append(item)
                    if item[0] == "delta" and len(events) == 2:
                        send_terminate(server)
                        probe = await _ask_until_refused(other, server.base, late_tag)
        except (httpx.TransportError, OSError) as exc:
            error = type(exc).__name__
    try:
        await asyncio.to_thread(server.proc.wait, SIGTERM_EXIT_WAIT_S)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    ignored = not exited and error is None and bool(events) and events[-1][0] == "done"
    return {"events": events, "error": error, "exited": exited, "signal_ignored": ignored, **probe}


async def _ask_until_refused(client: httpx.AsyncClient, base: str, tag: str, timeout: float = 10.0) -> dict:
    """Ask (a new question each time: a repeat would be a cached answer, which is served during a drain) until the
    server answers something but 200. The first asks may be served: the signal has not been handled yet."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with ask_request(client, base, f"[normal] {BODY} {uuid.uuid4().hex[:8]}", tag) as response:
            body = await response.aread()
            if response.status_code != 200:
                refused = (response.status_code, response.headers.get("content-type", ""), body)
                healthz = (await client.get(f"{base}/healthz")).status_code
                return {"refused": refused, "retry_after": response.headers.get("retry-after"), "healthz": healthz}
        await asyncio.sleep(0.05)
    raise AssertionError(f"the server never refused a paid ask in {timeout} s after the signal")


# ---- 8: what sse-starlette does on a client disconnect

def test_the_order_in_which_sse_starlette_runs_a_disconnect(server):
    """Pins the order in which sse-starlette runs the generator's cancellation, the BackgroundTask and the ``finally``
    around the ASGI call when the client disconnects, for a generator awaiting and for one suspended at its ``yield``,
    and the same for the real ``PaidStream`` (twin exit, background ``finalize`` writing the row, a no-op second
    ``finalize``). The module docstring says what each order means for ``PaidResponse``."""
    asyncio.run(_disconnect_order(server))


async def _probe_order(client: httpx.AsyncClient, base: str, run: str, hold_send_s: float) -> list[str]:
    params = {"run": run, "hold_send_s": hold_send_s}
    async with client.stream("POST", f"{base}/_test/order", params=params) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                break
    await wait_for(client, base, lambda s: f"probe:{run}:call_finally" in s["order"], timeout=5.0)
    await asyncio.sleep(0.5)                       # let anything that runs later (a collected generator) show
    order = (await astate(client, base))["order"]
    return [name.split(":", 2)[2] for name in order if name.startswith(f"probe:{run}:")]


async def _disconnect_order(server: Server) -> None:
    base = server.base
    async with new_client() as client:
        awaiting = await _probe_order(client, base, "awaiting", 0.0)
        suspended = await _probe_order(client, base, "suspended", 0.5)
        await client.post(f"{base}/_test/reset")
        await leave_after_first_delta(base, HANG, new_tag("order"))
        await wait_for(client, base, lambda s: s["finalized"] == 1 and is_quiet(s))
        await asyncio.sleep(0.3)
        order = (await astate(client, base))["order"]
    real = [name for name in order if not name.startswith("probe:")]      # a dropped probe generator may close late
    assert awaiting == AWAITING_ORDER, awaiting
    # What follows ``call_finally`` for a generator dropped at its ``yield`` is up to the garbage collector: not pinned.
    assert suspended[:len(SUSPENDED_ORDER)] == SUSPENDED_ORDER, suspended
    assert real == PAID_ORDER, real


# ---- 9: a client that stops reading in the last frame

def test_the_servers_run_the_production_event_loop(main_server, busy_server):
    """Production runs ``uvicorn --loop asyncio`` (uvloop is not in its requirements); a machine that has uvloop must
    not silently run these tests on another loop. Observed on a Windows box only: there ``auto`` is asyncio as well."""
    for server in (main_server, busy_server):
        assert server.loop.startswith("asyncio") and "uvloop" not in server.loop, server.loop


def test_a_stalled_closing_chunk_frees_the_lease_at_once_and_the_response_ends_on_the_send_timeout(
        one_slot_server, record_property):
    freed_after_s, dropped_after_s = asyncio.run(_stalled_closing_chunk(one_slot_server))
    record_property("closing_stall_slot_freed_s", round(freed_after_s, 3))
    record_property("closing_stall_response_ended_s", round(dropped_after_s, 2))


async def _stalled_closing_chunk(server: Server) -> tuple[float, float]:
    """The reviewer's scenario without 150 KiB of real backlog: ``done`` goes out, then the transport stops draining, so
    sse-starlette's closing send (which holds its send lock and has no timeout of its own) never completes. The client
    stays connected throughout. The lease must be back before the send timeout fires (the end of ``events()`` released
    it), a second ask on the one-lease server must be served rather than refused as busy, and the response must end on
    the send timeout with the one warning line and no traceback."""
    base, tag, next_tag = server.base, new_tag("closing"), new_tag("after")
    async with new_client() as client, new_client() as other:
        await ask_all(client, base, NORMAL, new_tag("warm"))    # embed the question text once
        await wait_for(client, base, is_quiet)
        await client.post(f"{base}/_test/reset")
        log_mark = server.log_size()
        async with ask_request(client, base, NORMAL, tag, {PAUSE_HEADER: "1"}) as response:
            reader = Events(response)           # kept alive: a collected line iterator would close the connection
            await reader.until("done")
            done_at = time.perf_counter()
            state, _ = await wait_for(client, base, lambda s: s["finalized"] == 1 and is_quiet(s),
                                      timeout=SEND_TIMEOUT_S + 1)
            freed_after_s = time.perf_counter() - done_at
            again = await ask_all(other, base, NORMAL, next_tag)    # the one slot is free: served, not busy
            deadline = done_at + SEND_TIMEOUT_S + 1
            while SEND_TIMEOUT_LINE not in server.log_since(log_mark) and time.perf_counter() < deadline:
                await asyncio.sleep(POLL_S)
            dropped_after_s = time.perf_counter() - done_at
            await asyncio.sleep(QUIET_FOR_S)                        # what uvicorn logs right after the app returns
            log = server.log_since(log_mark)
        final, _ = await wait_for(client, base, is_quiet)
    assert state["write_pauses"] == 1, "the stall was not injected: nothing here tested the closing send"
    assert freed_after_s < SEND_TIMEOUT_S, f"the lease came back after {freed_after_s:.2f} s: the send timeout freed it"
    rows = rows_of(state, tag)
    assert len(rows) == 1 and rows[0]["usage"] and rows[0]["cost_usd"] is not None, rows    # the answer's own row
    assert [name for name, _ in again][-1] == "done", "the second ask was not served while the first response hung"
    assert len(rows_of(final, next_tag)) == 1 and final["ledger_row_count"] == 2
    assert final["inflight_leases"] == 0 and final["twin_exits"] == {"completed": 2}, final["twin_exits"]
    assert dropped_after_s <= SEND_TIMEOUT_S + 1, f"the response ended {dropped_after_s:.2f} s after its last frame"
    assert "Traceback" not in log, log[-2000:]
    assert log.count(SEND_TIMEOUT_LINE) == 1 and log.count(UVICORN_INCOMPLETE_LINE) == 1, log[-2000:]
    return freed_after_s, dropped_after_s
