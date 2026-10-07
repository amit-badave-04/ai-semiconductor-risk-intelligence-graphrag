"""The SIGTERM drain (M5a I4 item D, docs/v2/M5A_BUILD_PLAN.md): ``semigraph.serve.drain``.

Three layers, cheapest first: (1) ``Drain``, the process-wide ``DRAIN`` (counter, flag, ``wait_idle``, ``await_idle``);
(2) ``DrainingServer`` without a process, driven by hand over a fresh ``Drain``, a fake clock and a recorder in place
of the "stop the SSE streams" action (nothing there may touch the singleton or sse-starlette's ``AppStatus``: either
would break every other in-process SSE test); (3) the real thing, ``python -m semigraph.serve.drain`` (the Dockerfile
CMD) in a subprocess over ``tests/serve_drain_app.py``, with a real signal on a real socket: a 3 s stream is open when
the signal arrives; within 1 s a new ask gets 503 while a read and ``/healthz`` answer (each on a FRESH connection, so
a pooled one cannot hide a closed listener); the stream still ends with ``done``; the process exits with code 0 well
before ``DRAIN_TIMEOUT_S`` and "driver closed" comes after the stream's ``finalize``. A stream that never ends is cut
when ``DRAIN_TIMEOUT_S`` runs out, not before and not never; a second signal ends everything at once. Three tests run
stock ``uvicorn`` (and a grace period) over the same app to pin why the module exists and why the wiring adds no grace.

The signal is SIGTERM on POSIX. A Windows subprocess has no SIGTERM: the server runs in its own process group and gets
CTRL_BREAK_EVENT, which uvicorn handles as SIGBREAK. A test skips, saying so, when the signal cannot be sent or has no
effect on Windows (no console to deliver it to); on POSIX no effect is a failure.
"""

import asyncio
import importlib
import inspect
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from serve_drain_app import MSG_DRAINING, Journal, create_app, journal_events
from sse_starlette.sse import AppStatus

from semigraph.config import DRAIN_TIMEOUT_CEILING_S, Settings
from semigraph.serve import drain as drain_module
from semigraph.serve.drain import (
    DEFAULT_DRAIN_TIMEOUT_S,
    DRAIN,
    FORCED_SHUTDOWN_S,
    KILL_TIMEOUT_S,
    LIFESPAN_IDLE_WAIT_S,
    LIFESPAN_SHUTDOWN_BUDGET_S,
    MIN_SHUTDOWN_S,
    SHUTDOWN_MARGIN_S,
    Drain,
    DrainingServer,
    build_config,
    graceful_timeout_s,
    post_drain_window_s,
)

REPO = Path(__file__).resolve().parents[1]
SRC, TESTS = REPO / "src", REPO / "tests"
IS_WINDOWS = sys.platform == "win32"
SERVER_START_TIMEOUT_S = 90.0
EXIT_WAIT_S = 20.0
STREAM_DEADLINE_S = 40.0          # a stream that should have ended fails its test after this long instead of hanging
DRAINING_WITHIN_S = 1.0           # the signal must show (an ask answered 503) within this long
STREAM_S = 3.0                    # the "short" stream of the app: 30 deltas 0.1 s apart
ENV_WHITELIST = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME",
                 "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "VIRTUAL_ENV")


# ---- 1: Drain

def test_a_fresh_drain_is_idle_and_not_draining():
    drain = Drain()

    assert drain.active == 0 and not drain.draining and drain.began_at is None


def test_enter_and_leave_count_the_active_streams():
    drain = Drain()

    drain.enter()
    drain.enter()
    assert drain.active == 2
    drain.leave()
    assert drain.active == 1
    drain.leave()
    assert drain.active == 0


def test_leave_without_enter_never_goes_negative_and_says_so():
    drain, records = Drain(), []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    try:
        drain.leave()
        drain.leave()
        drain.enter()                  # the next stream is counted from zero, not from -1: it stays visible
    finally:
        logger.removeHandler(handler)

    assert drain.active == 1
    assert sum("without a matching enter" in message for message in records) == 2


def test_begin_is_idempotent_and_records_the_start_once():
    drain = Drain()

    assert drain.begin() is True
    started = drain.began_at
    time.sleep(0.01)
    assert drain.begin() is False
    assert drain.draining and drain.began_at == started


def test_try_enter_refuses_once_the_drain_has_begun_but_enter_still_counts():
    drain = Drain()

    assert drain.try_enter() is True
    drain.begin()
    assert drain.try_enter() is False
    assert drain.active == 1           # the refused one was not counted
    drain.enter()                      # an unconditional enter (a stream admitted earlier) still counts
    assert drain.active == 2


def test_wait_idle_returns_true_at_once_when_nothing_is_active():
    started = time.monotonic()

    assert Drain().wait_idle(5.0) is True
    assert time.monotonic() - started < 0.5


def test_wait_idle_times_out_while_a_stream_is_active():
    drain = Drain()
    drain.enter()
    started = time.monotonic()

    assert drain.wait_idle(0.2) is False
    assert 0.15 <= time.monotonic() - started < 2.0


def test_wait_idle_wakes_when_another_thread_leaves():
    drain = Drain()
    drain.enter()
    threading.Timer(0.15, drain.leave).start()
    started = time.monotonic()

    assert drain.wait_idle(10.0) is True
    assert time.monotonic() - started < 2.0


def test_the_counter_survives_many_threads():
    drain = Drain()

    def work() -> None:
        for _ in range(2000):
            drain.enter()
            drain.leave()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert drain.active == 0 and drain.wait_idle(1.0)


def test_begin_does_not_deadlock_when_the_calling_thread_holds_the_lock():
    """White-box. uvicorn calls ``handle_exit`` -> ``begin()`` in a signal handler, on the thread that may be INSIDE
    ``enter()`` or ``leave()`` at that moment; a plain lock would never come back."""
    drain, result = Drain(), []

    def interrupted_enter() -> None:
        with drain._cond:
            result.append(drain.begin())

    thread = threading.Thread(target=interrupted_enter, daemon=True)
    thread.start()
    thread.join(5)

    assert not thread.is_alive() and result == [True]


def test_await_idle_returns_true_at_once_when_nothing_is_active():
    assert asyncio.run(Drain().await_idle(5.0)) is True


def test_await_idle_times_out_while_a_stream_is_active():
    drain = Drain()
    drain.enter()
    started = time.monotonic()

    assert asyncio.run(drain.await_idle(0.2)) is False
    assert 0.15 <= time.monotonic() - started < 2.0


def test_await_idle_wakes_when_a_worker_thread_leaves():
    drain = Drain()
    drain.enter()
    threading.Timer(0.15, drain.leave).start()

    assert asyncio.run(drain.await_idle(10.0)) is True


def test_await_idle_is_cancellable_and_holds_no_thread():
    drain = Drain()
    drain.enter()
    before = threading.active_count()

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(drain.await_idle(None), 0.2)

    asyncio.run(scenario())

    assert threading.active_count() <= before


def test_the_lifespan_waits_for_a_stream_that_leaves_late():
    """The lifespan shutdown must not record "driver closed" while a stream still counts as active. uvicorn already
    waits for request tasks, so this is what protects the driver when something finishes outside them."""
    drain, journal = Drain(), Journal()
    app = create_app(drain, journal)

    def leave_late() -> None:
        journal.note("left")
        drain.leave()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            drain.enter()
            threading.Timer(0.3, leave_late).start()

    asyncio.run(scenario())

    assert journal.names() == ["started", "left", "driver_closed"]
    closed = journal.entries[-1]
    assert closed["idle"] is True and closed["active"] == 0


# ---- 2: DrainingServer without a process

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """A ``DrainingServer`` over a private ``Drain``, a fake clock and a recorder instead of the SSE-stopping action."""

    def __init__(self, drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S):
        self.clock, self.drain, self.ended = FakeClock(), Drain(), []
        config = uvicorn.Config("serve_drain_app:app", log_config=None,
                                timeout_graceful_shutdown=graceful_timeout_s(drain_timeout_s))
        self.server = DrainingServer(config, drain=self.drain, drain_timeout_s=drain_timeout_s,
                                     end_streams=lambda: self.ended.append(self.clock.now), clock=self.clock)

    def signal(self, sig: int = signal.SIGTERM) -> None:
        self.server.handle_exit(sig, None)

    def tick(self, advance_s: float = 0.0) -> bool:
        self.clock.now += advance_s
        return asyncio.run(self.server.on_tick(1))


def test_the_first_signal_begins_the_drain_and_nothing_else():
    h = Harness()
    app_status = AppStatus.should_exit

    h.signal()

    assert h.drain.draining
    assert not h.server.should_exit and not h.server.force_exit
    assert h.ended == []                                    # no stream is told to stop
    assert AppStatus.should_exit == app_status              # sse-starlette's own handler was not run
    assert h.server._captured_signals == []                 # so the signal is not re-raised at exit (exit code 0)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_tick_while_a_stream_is_active_keeps_the_drain_going(sig):
    h = Harness()
    h.drain.enter()
    h.signal(sig)

    assert [h.tick(1.0) for _ in range(5)] == [False] * 5
    assert not h.server.should_exit and h.ended == []


def test_a_tick_without_any_signal_never_ends_the_server():
    h = Harness()

    assert h.tick(10_000.0) is False
    assert h.server.exit_code == 0 and h.server.exit_reason == ""


def test_the_drain_ends_as_soon_as_the_last_stream_has_left():
    h = Harness()
    h.drain.enter()
    h.signal()
    h.tick(30.0)

    h.drain.leave()

    assert h.tick(0.1) is True
    assert h.server.should_exit and not h.server.force_exit
    assert h.server.exit_reason == "idle" and h.server.exit_code == 0
    assert len(h.ended) == 1


def test_an_idle_server_ends_the_drain_on_the_first_tick():
    h = Harness()
    h.signal()

    assert h.tick() is True and h.server.exit_reason == "idle"


def test_the_drain_ends_when_the_timeout_runs_out_with_a_stream_still_active():
    h = Harness(drain_timeout_s=240.0)
    h.drain.enter()
    h.signal()

    assert h.tick(239.0) is False
    assert h.tick(1.0) is True
    assert h.server.exit_reason == "timeout" and h.server.exit_code == 1
    assert len(h.ended) == 1                                # the streams that cannot finish are cut now


def test_the_shutdown_window_after_a_timeout_leaves_the_margin_and_the_lifespan_before_the_kill_timeout():
    h = Harness(drain_timeout_s=240.0)
    h.drain.enter()
    h.signal()
    h.tick(240.0)

    assert (h.server.config.timeout_graceful_shutdown
            == KILL_TIMEOUT_S - SHUTDOWN_MARGIN_S - LIFESPAN_SHUTDOWN_BUDGET_S - 240.0 == 15.0)


def test_an_early_idle_end_keeps_uvicorns_full_graceful_timeout():
    h = Harness(drain_timeout_s=240.0)
    h.signal()
    h.tick(5.0)

    assert h.server.config.timeout_graceful_shutdown == graceful_timeout_s(240.0) == 250.0


def test_a_second_signal_exits_without_waiting():
    h = Harness()
    h.drain.enter()
    h.signal()
    h.tick(1.0)

    h.signal()

    assert h.server.force_exit and h.server.should_exit
    assert h.server.exit_reason == "forced" and h.server.exit_code == 1
    assert len(h.ended) == 1                                # the streams are told to stop at once
    assert h.server.config.timeout_graceful_shutdown == FORCED_SHUTDOWN_S


def test_a_signal_after_a_clean_drain_does_not_turn_it_into_a_failure():
    h = Harness()
    h.signal()
    h.tick()

    h.signal()

    assert h.server.force_exit and h.server.exit_reason == "idle" and h.server.exit_code == 0


@pytest.mark.parametrize(("elapsed", "graceful", "expected"), [
    (0.0, 250.0, 250.0),            # an idle end at once: uvicorn's whole graceful timeout
    (5.0, 250.0, 250.0),            # 300 - 10 - 35 - 5 = 250 is still the whole of it
    (6.0, 250.0, 249.0),
    (240.0, 250.0, 15.0),           # the timeout path: what is left of the kill timeout after the margin and the lifespan
    (254.0, 264.0, MIN_SHUTDOWN_S),                # the ceiling of DRAIN_TIMEOUT_S: only the floor is left
    (289.5, 250.0, MIN_SHUTDOWN_S),                # nothing left: the floor
    (1000.0, 250.0, MIN_SHUTDOWN_S),
])
def test_post_drain_window_never_runs_past_the_kill_timeout(elapsed, graceful, expected):
    assert post_drain_window_s(elapsed, graceful) == pytest.approx(expected)


# ---- the shutdown budget: drain + the window of uvicorn's wait + the lifespan shutdown + the margin <= kill_timeout

def lifespan_worst_case_s() -> float:
    """``main.lifespan`` after ``yield`` with every step at its bound, read from the code that sets each bound, so a
    bump anywhere fails the sum test below. uvicorn runs this AFTER ``timeout_graceful_shutdown`` (``Server.shutdown``:
    the wait for connections and tasks, then ``lifespan.shutdown``), so it is not inside that window. Steps, in order:
    the wait for stragglers on the drain, the maintenance thread join, its settle flush plus the one state operation that
    can overrun it (the server-side timeout and a connection attempt), the monitor stop, the sweeper stop, the tracer."""
    from semigraph.serve import main, monitor
    from semigraph.serve.state import maintenance
    from semigraph.uploads import jobs

    state = Settings(_env_file=None)
    one_operation = state.state_op_timeout_s + state.state_connection_acquisition_s
    monitor_stop = inspect.signature(monitor.FreshnessMonitor.stop).parameters["timeout"].default
    return (LIFESPAN_IDLE_WAIT_S + maintenance.STOP_TIMEOUT_S + maintenance.FLUSH_BUDGET_S + one_operation
            + monitor_stop + jobs.SWEEP_STOP_TIMEOUT_S + main.TRACER_SHUTDOWN_TIMEOUT_S)


def test_the_lifespan_shutdown_budget_covers_every_step_of_the_lifespan():
    assert 30.0 < lifespan_worst_case_s() <= LIFESPAN_SHUTDOWN_BUDGET_S        # 32.5 s today, 35 s budgeted: the closes


@pytest.mark.parametrize("drain_timeout_s", [1.0, 60.0, DEFAULT_DRAIN_TIMEOUT_S, float(DRAIN_TIMEOUT_CEILING_S)])
@pytest.mark.parametrize("ends_after_s", ["at once", "halfway", "at the timeout"])
def test_drain_plus_window_plus_lifespan_plus_margin_fit_in_the_kill_timeout(drain_timeout_s, ends_after_s):
    """The overrun the panel found: 240 + 50 + 32 = 322 s against a kill_timeout of 300 s, so SIGKILL landed in the settle
    flush or the driver close. Summed here over the drain's whole range, from fly.toml's own kill_timeout."""
    fly = tomllib.loads((REPO / "fly.toml").read_text(encoding="utf-8"))
    elapsed = {"at once": 0.0, "halfway": drain_timeout_s / 2, "at the timeout": drain_timeout_s}[ends_after_s]

    window = post_drain_window_s(elapsed, graceful_timeout_s(drain_timeout_s))

    assert elapsed + window + lifespan_worst_case_s() + SHUTDOWN_MARGIN_S <= fly["kill_timeout"]


def test_the_ceiling_of_the_drain_timeout_setting_is_what_the_budget_leaves():
    assert DRAIN_TIMEOUT_CEILING_S == KILL_TIMEOUT_S - SHUTDOWN_MARGIN_S - LIFESPAN_SHUTDOWN_BUDGET_S - MIN_SHUTDOWN_S == 254


# ---- the configuration around it

@pytest.fixture
def no_drain_timeout_variable(monkeypatch):
    monkeypatch.delenv("DRAIN_TIMEOUT_S", raising=False)


def test_drain_timeout_defaults_and_reads_the_environment(monkeypatch, no_drain_timeout_variable):
    assert Settings(_env_file=None).drain_timeout_s == DEFAULT_DRAIN_TIMEOUT_S == 240.0
    for raw, expected in (("3", 3.0), ("12.5", 12.5), ("254", 254.0)):
        monkeypatch.setenv("DRAIN_TIMEOUT_S", raw)
        assert Settings(_env_file=None).drain_timeout_s == expected


@pytest.mark.parametrize("raw", ["0", "0.5", "-5", "abc", "nan", "inf", "-inf", "255", "289", "1000"])
def test_drain_timeout_refuses_values_that_cannot_work(monkeypatch, no_drain_timeout_variable, raw):
    monkeypatch.setenv("DRAIN_TIMEOUT_S", raw)
    with pytest.raises(ValueError, match="drain_timeout_s"):                   # a ValidationError is a ValueError
        Settings(_env_file=None)


def test_main_takes_the_drain_timeout_from_the_validated_setting_and_from_nowhere_else(monkeypatch):
    """One source of truth (the dead-setting finding): ``Settings.drain_timeout_s`` is what ``main`` hands to both uvicorn's
    configuration and the server; the module no longer reads ``DRAIN_TIMEOUT_S`` itself."""
    seen: dict = {}

    class FakeConfig:
        def load_app(self) -> None:
            seen["loaded"] = True

    class FakeServer:
        started, exit_code = True, 0

        def __init__(self, config, *, drain_timeout_s):
            seen["server"] = drain_timeout_s

        def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setenv("DRAIN_TIMEOUT_S", "999")                                    # must be ignored: not read here
    monkeypatch.setattr(drain_module, "get_settings", lambda: SimpleNamespace(drain_timeout_s=77.0))
    monkeypatch.setattr(drain_module, "build_config",
                        lambda app, host, port, timeout, loop="auto": seen.update(config=timeout) or FakeConfig())
    monkeypatch.setattr(drain_module, "DrainingServer", FakeServer)

    assert drain_module.main(["--port", "1"]) == 0
    assert seen == {"config": 77.0, "server": 77.0, "loaded": True, "ran": True}
    assert not hasattr(drain_module, "drain_timeout_from_env") and not hasattr(drain_module, "DRAIN_TIMEOUT_ENV")


def test_the_timeouts_nest_drain_then_uvicorns_graceful_then_the_kill_timeout():
    assert graceful_timeout_s(DEFAULT_DRAIN_TIMEOUT_S) == 250.0 > DEFAULT_DRAIN_TIMEOUT_S
    assert KILL_TIMEOUT_S > graceful_timeout_s(DEFAULT_DRAIN_TIMEOUT_S)
    assert 0 < LIFESPAN_IDLE_WAIT_S <= SHUTDOWN_MARGIN_S


def test_build_config_sets_only_the_address_the_loop_and_the_graceful_timeout(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: seen.update(app=app, **kwargs))

    build_config("some.module:app", "0.0.0.0", 8080, 240.0)

    assert seen == {"app": "some.module:app", "host": "0.0.0.0", "port": 8080, "loop": "auto",
                    "timeout_graceful_shutdown": 250.0}


def test_the_config_defaults_the_old_command_relied_on_are_uvicorns_command_line_defaults():
    """``uvicorn semigraph.serve.main:app --host 0.0.0.0 --port 8080`` becomes ``Config(app, host, port)``: every
    other option has to mean the same. Compares the click command's defaults with ``Config``'s constructor's."""
    command = importlib.import_module("uvicorn.main").main
    constructor = inspect.signature(uvicorn.Config.__init__).parameters
    sentinels = {"app", "host", "port", "reload_dirs", "reload_includes", "reload_excludes", "headers", "log_config"}

    differing = [p.name for p in command.params
                 if p.name in constructor and p.name not in sentinels and constructor[p.name].default != p.default]

    assert differing == []
    assert constructor["log_config"].default == uvicorn.config.LOGGING_CONFIG     # the CLI substitutes it for None


def test_parse_args_defaults_are_the_production_command_and_the_port_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    args = drain_module._parse_args([])
    monkeypatch.setenv("PORT", "9191")

    assert (args.app, args.host, args.port, args.loop, args.app_dir) == (
        "semigraph.serve.main:app", "0.0.0.0", 8080, "auto", None)
    assert drain_module._parse_args([]).port == 9191


def test_fly_toml_sends_sigterm_and_waits_the_longest_fly_allows():
    fly = tomllib.loads((REPO / "fly.toml").read_text(encoding="utf-8"))

    assert fly["kill_signal"] == "SIGTERM"                  # top level (not under a table): Fly reads it from there
    assert fly["kill_timeout"] == KILL_TIMEOUT_S == 300
    assert "kill_signal" not in fly["checks"]["health"] and "kill_timeout" not in fly["http_service"]
    assert DEFAULT_DRAIN_TIMEOUT_S + SHUTDOWN_MARGIN_S < fly["kill_timeout"]


def test_the_dockerfile_runs_the_drain_server_in_exec_form_so_it_is_pid_1():
    lines = [ln.strip() for ln in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines() if ln.strip()]

    assert lines[-1].startswith("CMD ")
    assert json.loads(lines[-1][len("CMD "):]) == ["python", "-m", "semigraph.serve.drain"]
    assert sum(ln.startswith("CMD ") for ln in lines) == 1


def test_the_module_run_as_main_hands_over_to_the_one_imported_module():
    """``python -m semigraph.serve.drain`` loads this file as ``__main__``; the routes import it as
    ``semigraph.serve.drain``. If ``main`` ran from ``__main__``, the server would flip a DIFFERENT ``DRAIN`` than the
    one the routes read. The guard at the bottom re-imports the canonical module (the process tests below start the
    real entry point and would fail otherwise)."""
    source = Path(drain_module.__file__).read_text(encoding="utf-8")

    assert "from semigraph.serve.drain import main" in source.split('if __name__ == "__main__":')[1]
    assert importlib.import_module("semigraph.serve.drain").DRAIN is DRAIN


# ---- 3: a real process, a real signal

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ServerProcess:
    """The app under ``python -m semigraph.serve.drain`` (or, ``stock``, under plain ``python -m uvicorn``) on
    127.0.0.1. Its working directory is a temporary one and its environment holds no secrets: a whitelist of what an
    interpreter needs, plus ``PYTHONPATH``."""

    def __init__(self, workdir: Path, *, drain_timeout_s: float = 30.0, stock: bool = False):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = workdir / f"server-{self.port}.log"
        self.journal_path = workdir / f"journal-{self.port}.jsonl"
        self.workdir, self.stock = workdir, stock
        self.env = {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}
        self.env.update({"PYTHONPATH": str(SRC), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                         "DRAIN_APP_JOURNAL": str(self.journal_path), "DRAIN_TIMEOUT_S": str(drain_timeout_s)})
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        common = ["--app-dir", str(TESTS), "--host", "127.0.0.1", "--port", str(self.port), "--loop", "asyncio"]
        if self.stock:
            return [sys.executable, "-m", "uvicorn", "serve_drain_app:app", *common, "--log-level", "warning"]
        return [sys.executable, "-m", "semigraph.serve.drain", "--app", "serve_drain_app:app", *common]

    def start(self) -> "ServerProcess":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(self.command(), cwd=self.workdir, env=self.env, stdout=log,
                                         stderr=subprocess.STDOUT, creationflags=flags)
        self._wait_ready()
        return self

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the server exited with {self.proc.returncode} before it was ready:\n{self.log()}")
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1.0, trust_env=False).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"the server was not ready in {SERVER_START_TIMEOUT_S} s:\n{self.log()}")

    def log(self, chars: int = 3000) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-chars:] if self.log_path.exists() else ""

    def journal(self) -> list[dict]:
        return journal_events(self.journal_path)

    def names(self) -> list[str]:
        return [e["event"] for e in self.journal()]

    def terminate(self) -> None:
        """SIGTERM on POSIX; CTRL_BREAK_EVENT to the server's own process group on Windows (skips if unsendable)."""
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
        except (OSError, ValueError) as error:
            pytest.skip(f"the termination signal could not be sent to the server ({error!r})")

    def wait(self, timeout: float = EXIT_WAIT_S) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the server did not exit within {timeout} s of the signal; log:\n{self.log()}")

    def stop(self) -> None:
        """Kill what is left (a failed test) and check that nothing is left running and nothing still listens."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        assert proc is None or proc.poll() is not None, "the server process is still running"
        deadline = time.monotonic() + 5.0           # a venv launcher on Windows takes its child down a moment later
        while _port_open(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _port_open(self.port), f"something still listens on port {self.port}"


def new_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout))


class Events:
    """The event names of one SSE response, read one at a time."""

    def __init__(self, response: httpx.Response):
        self._lines = response.aiter_lines()

    async def next(self) -> str | None:
        async for line in self._lines:
            if line.startswith("event:"):
                return line.split(":", 1)[1].strip()
        return None

    async def until_end(self, deadline_s: float = STREAM_DEADLINE_S) -> tuple[list[str], str | None]:
        """Every remaining event name, and the exception class name when the connection broke instead of ending. A
        stream still running after ``deadline_s`` fails the test (instead of hanging it)."""
        names, error = [], None
        try:
            async with asyncio.timeout(deadline_s):
                while (name := await self.next()) is not None:
                    names.append(name)
        except TimeoutError:                       # first: TimeoutError is an OSError
            pytest.fail(f"the stream was still running {deadline_s:g} s later; it saw {len(names)} more events, "
                        f"the last {names[-1:]}")
        except (httpx.TransportError, OSError) as exc:
            error = type(exc).__name__
        return names, error


async def read_until(events: Events, wanted: str) -> list[str]:
    seen = []
    while (name := await events.next()) is not None:
        seen.append(name)
        if name == wanted:
            return seen
    raise AssertionError(f"the stream ended before a {wanted!r} event: {seen}")


async def poll_state(server: ServerProcess, within_s: float) -> dict | None:
    """``/_test/state`` on a fresh connection until it says draining (or the process has exited); None if it never
    does."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            return {"draining": True, "exited": True}
        try:
            async with new_client(1.0) as client:
                state = (await client.get(f"{server.base}/_test/state")).json()
            if state["draining"]:
                return state
        except (httpx.TransportError, ValueError):
            pass
        await asyncio.sleep(0.02)
    return None


def require_effect(state: dict | None, server: ServerProcess | None = None) -> dict:
    """A signal that had no effect is a skip on Windows (nothing to deliver it to) and a failure elsewhere. A server
    whose log shows the handler ran while the app's ``DRAIN`` is not draining is a bug on every platform (two
    ``DRAIN`` objects)."""
    if state is None:
        if server is not None and "received: draining" in server.log(10_000):
            pytest.fail(f"the signal handler ran but the application's DRAIN is not draining:\n{server.log()}")
        if IS_WINDOWS:
            pytest.skip("the termination signal was sent but the server did not react to it (no console to deliver "
                        "it to)")
        pytest.fail("the termination signal had no effect on the server")
    return state


async def probe_during_drain(server: ServerProcess, signalled_at: float) -> dict:
    """A new ask, a read and the health check, each on a fresh connection, right after the server shows the drain."""
    state = require_effect(await poll_state(server, DRAINING_WITHIN_S + 2.0), server)
    try:
        async with new_client(1.0) as client:
            ask = await client.post(f"{server.base}/ask", params={"mode": "short"})
        async with new_client(1.0) as client:
            read = await client.get(f"{server.base}/read")
        async with new_client(1.0) as client:
            health = await client.get(f"{server.base}/healthz")
    except httpx.TransportError as exc:
        pytest.fail(f"the server stopped answering at the signal ({type(exc).__name__}):\n{server.log()}")
    return {"state": state, "ask": ask.status_code, "ask_body": ask.json(), "read": read.status_code,
            "retry_after": ask.headers.get("retry-after"), "health": health.status_code,
            "elapsed": time.monotonic() - signalled_at}


async def _short_stream_through_a_drain(server: ServerProcess) -> dict:
    async with new_client(30.0) as client:
        async with client.stream("POST", f"{server.base}/ask", params={"mode": "short"}) as response:
            events = Events(response)
            seen = await read_until(events, "delta")
            started = time.monotonic()
            server.terminate()
            probes = await probe_during_drain(server, started)
            rest, error = await events.until_end()
    return {"events": seen + rest, "error": error, "probes": probes, "stream_ended": time.monotonic() - started}


def test_a_stream_in_flight_finishes_while_new_asks_get_503_and_reads_are_served(tmp_path):
    server = ServerProcess(tmp_path, drain_timeout_s=30.0).start()
    try:
        started = time.monotonic()
        outcome = asyncio.run(_short_stream_through_a_drain(server))
        code = server.wait()
        took = time.monotonic() - started
    finally:
        server.stop()

    probes = outcome["probes"]
    assert (probes["ask"], probes["read"], probes["health"]) == (503, 200, 200), probes
    assert probes["ask_body"] == {"detail": MSG_DRAINING} and probes["retry_after"] == "30"
    assert probes["elapsed"] < DRAINING_WITHIN_S, probes
    assert outcome["error"] is None and outcome["events"][-1] == "done", outcome        # finished, not cut
    assert outcome["events"].count("delta") == 30
    assert outcome["stream_ended"] >= STREAM_S - 0.6                                    # it really ran its 3 s
    assert code == 0, server.log()
    assert took < STREAM_S + 8.0 < 30.0                                                 # well before DRAIN_TIMEOUT_S
    names = server.names()
    assert names.index("ledger_row") < names.index("finalize_done") < names.index("driver_closed"), names
    assert "stream_cancelled" not in names
    closed = [e for e in server.journal() if e["event"] == "driver_closed"][0]
    assert closed["idle"] is True and closed["active"] == 0
    log = server.log(10_000)                                                            # and it says what it did
    assert "received: draining (new paid asks and uploads get 503" in log, log
    assert log.index("received: draining") < log.index("drain finished after") < log.index("Shutting down"), log


async def _forever_stream_through_a_drain(server: ServerProcess, signals: int = 1, grace: float = 0.0,
                                          deadline_s: float = 20.0) -> dict:
    async with new_client(30.0) as client:
        params = {"mode": "forever", "grace": grace}
        async with client.stream("POST", f"{server.base}/ask", params=params) as response:
            events = Events(response)
            seen = await read_until(events, "delta")
            first = time.monotonic()
            server.terminate()
            require_effect(await poll_state(server, DRAINING_WITHIN_S + 2.0), server)
            second = first
            if signals > 1:
                await asyncio.sleep(0.5)
                second = time.monotonic()
                server.terminate()
            rest, error = await events.until_end(deadline_s)
    code = await asyncio.to_thread(server.wait)
    return {"events": seen + rest, "error": error, "first": first, "second": second, "exited": time.monotonic(),
            "code": code}


def test_a_stream_that_never_ends_is_cut_when_the_drain_timeout_runs_out(tmp_path):
    server = ServerProcess(tmp_path, drain_timeout_s=3.0).start()
    try:
        outcome = asyncio.run(_forever_stream_through_a_drain(server))
    finally:
        server.stop()

    took = outcome["exited"] - outcome["first"]
    assert 2.5 <= took <= 3.0 + 6.0, (took, server.log())           # not before the timeout, and not never
    assert "done" not in outcome["events"] and outcome["error"] is not None, outcome     # cut
    assert outcome["code"] == 1, server.log()                       # unclean: a stream was still active
    names = server.names()
    assert names.index("stream_cancelled") < names.index("finalize_done") < names.index("driver_closed"), names
    assert [e["kind"] for e in server.journal() if e["event"] == "stream_cancelled"] == ["CancelledError"]


def test_a_second_signal_exits_at_once(tmp_path):
    server = ServerProcess(tmp_path, drain_timeout_s=60.0).start()
    try:
        outcome = asyncio.run(_forever_stream_through_a_drain(server, signals=2))
    finally:
        server.stop()

    after_second = outcome["exited"] - outcome["second"]
    assert after_second < FORCED_SHUTDOWN_S + 6.0, (after_second, server.log())        # nowhere near the 60 s
    assert "done" not in outcome["events"]
    assert outcome["code"] == 1
    assert "stream_cancelled" in server.names()                     # the stream was ended, not abandoned


def test_an_idle_server_exits_at_once_with_code_zero(tmp_path):
    server = ServerProcess(tmp_path, drain_timeout_s=60.0).start()
    try:
        started = time.monotonic()
        server.terminate()
        code = server.wait(10.0)
        took = time.monotonic() - started
    finally:
        server.stop()

    assert code == 0 and took < 5.0, (code, took, server.log())
    assert "driver_closed" in server.names()


# ---- contrast: stock uvicorn over the same app (why this module exists, and what a grace period does)

async def _listener_closed_within(server: ServerProcess, within_s: float) -> bool:
    """True once a fresh connection to the server fails (refused, reset or timed out: Windows answers a closed
    loopback port slowly)."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            async with new_client(0.5) as client:
                await client.get(f"{server.base}/read")
        except httpx.TransportError:
            return True
        await asyncio.sleep(0.05)
    return False


async def _stock_uvicorn_through_a_signal(server: ServerProcess, grace: float) -> dict:
    async with new_client(30.0) as client:
        params = {"mode": "short", "grace": grace}
        async with client.stream("POST", f"{server.base}/ask", params=params) as response:
            events = Events(response)
            seen = await read_until(events, "delta")
            server.terminate()
            closed = await _listener_closed_within(server, 3.0)
            rest, error = await events.until_end()
    return {"events": seen + rest, "error": error, "listener_closed": closed}


def _run_stock_uvicorn(workdir: Path, grace: float) -> tuple[dict, ServerProcess]:
    server = ServerProcess(workdir, stock=True).start()
    try:
        outcome = asyncio.run(_stock_uvicorn_through_a_signal(server, grace))
        server.wait()
    finally:
        server.stop()
    if not outcome["listener_closed"]:
        require_effect(None)
    return outcome, server


def test_stock_uvicorn_closes_the_listener_and_cuts_the_stream_at_the_signal(tmp_path):
    """What the module replaces: with the old command the signal closes the listener (no 503, no reads) and
    sse-starlette cancels the stream (``shutdown_grace_period`` 0): ``done`` never arrives."""
    outcome, server = _run_stock_uvicorn(tmp_path, grace=0)

    assert "done" not in outcome["events"] and outcome["error"] is not None, outcome
    assert "stream_cancelled" in server.names()


def test_stock_uvicorn_with_a_shutdown_grace_period_saves_the_stream_but_still_closes_the_listener(tmp_path):
    """The plan's option (a): ``shutdown_grace_period`` on the response saves the stream, not the listener."""
    outcome, server = _run_stock_uvicorn(tmp_path, grace=10)

    assert outcome["events"][-1] == "done" and outcome["error"] is None, outcome
    assert "stream_cancelled" not in server.names()


def test_a_shutdown_grace_period_under_the_drain_server_only_delays_the_cut(tmp_path):
    """Why the wiring must NOT add one: the drain does not tell sse-starlette anything until it ends, so a grace
    period would stack on top of it (here 2 s + 2 s) for the streams still running at the drain timeout."""
    server = ServerProcess(tmp_path, drain_timeout_s=2.0).start()
    try:
        outcome = asyncio.run(_forever_stream_through_a_drain(server, grace=2.0))
    finally:
        server.stop()

    took = outcome["exited"] - outcome["first"]
    assert 3.5 <= took <= 4.0 + 6.0, (took, server.log())
    assert "done" not in outcome["events"] and outcome["code"] == 1
