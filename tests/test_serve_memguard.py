"""The upload memory guard and the memory log line (docs/specs/upload-memory-guard.md, M5a decision 21).

A NEW upload is refused (503, ``Retry-After: 120``, ``MSG_UPLOAD_BUSY``) while ``MemAvailable`` in ``/proc/meminfo`` is below
``upload_min_mem_available_kb``; nothing is started, stored or charged, and asks, cached answers and every read route are not
touched. The guard is a safeguard, not a security control: where ``/proc/meminfo`` does not exist it is off (one WARNING at boot),
and a read error never refuses an upload. The serving process also logs ``memory vmrss_kb=... vmhwm_kb=... mem_available_kb=...
swap_used_kb=...`` every 60 s from the maintenance thread.

No real ``/proc`` is read anywhere: ``memguard.MEMINFO_PATH`` / ``STATUS_PATH`` are module constants the tests point at files of
their own. The route tests reuse the fakes of ``tests/test_serve_workspace_routes.py`` and ``tests/test_serve_state_wiring.py``
(imported fixtures), so the guard is exercised through the real ``workspace_routes`` code and the real gate order. This file runs
in the serve-shipped CI job: stdlib, FastAPI and the serve package only.
"""

import ast
import inspect
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from serve_state_fakes import RouteSettings, fresh_drain  # noqa: F401 - fresh_drain: a fixture
from test_serve_state_wiring import ask, cached_rows, events_of, make_client, store_fakes  # noqa: F401 - fixtures
from test_serve_workspace_routes import (  # noqa: F401 - the fixtures of the route tests, and their request helpers
    DOC, TOKEN, WS, _auth, _plain_slot_is_free, _turnstile, _upload, backend, client, fake_jobs, fake_repo, fake_store)
from test_state_inprocess import FakeClock
from test_state_maintenance import FakeBackend

from semigraph.serve import guard, memguard, store, workspace_routes as wr
from semigraph.serve.state.maintenance import MEMORY_LOG_EVERY_S, MaintenanceThread
from semigraph.uploads import jobs, repo

pytestmark = pytest.mark.usefixtures("fresh_drain")

THRESHOLD = 1_250_000                # the default: the parse child's 1 GiB RLIMIT_AS plus a margin
ENOUGH_KB = THRESHOLD + 500_000
LOW_KB = 777_123                     # a figure no message or header may carry
MEMORY_LINE = re.compile(r"^memory vmrss_kb=\d+ vmhwm_kb=\d+ mem_available_kb=\d+ swap_used_kb=\d+$")
REFUSAL_LINE = re.compile(r"^upload refused: memory mem_available_kb=\d+ threshold_kb=\d+$")
GUARD_LOGGER = "semigraph.serve.memguard"


def meminfo_text(available_kb: int, swap_total_kb: int = 0, swap_free_kb: int = 0) -> str:
    """What ``/proc/meminfo`` looks like (the lines around the ones the guard reads are there on purpose)."""
    return ("MemTotal:        4021784 kB\nMemFree:          212340 kB\n"
            f"MemAvailable:   {available_kb:>8} kB\nBuffers:           10244 kB\nCached:          1912012 kB\n"
            f"SwapCached:            0 kB\nSwapTotal:      {swap_total_kb:>8} kB\nSwapFree:       {swap_free_kb:>8} kB\n"
            "Dirty:                 0 kB\n")


def status_text(vmrss_kb: int, vmhwm_kb: int) -> str:
    return (f"Name:\tuvicorn\nUmask:\t0022\nState:\tS (sleeping)\nVmPeak:\t 2400000 kB\nVmHWM:\t{vmhwm_kb:>8} kB\n"
            f"VmRSS:\t{vmrss_kb:>8} kB\nThreads:\t12\n")


@pytest.fixture
def meminfo(tmp_path, monkeypatch):
    """A fake ``/proc/meminfo`` (enough memory at first); ``write(...)`` replaces its content."""
    path = tmp_path / "meminfo"
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(path))

    def write(available_kb: int, swap_total_kb: int = 0, swap_free_kb: int = 0) -> None:
        path.write_text(meminfo_text(available_kb, swap_total_kb, swap_free_kb), encoding="utf-8")

    write(ENOUGH_KB)
    return SimpleNamespace(path=path, write=write)


@pytest.fixture
def status(tmp_path, monkeypatch):
    """A fake ``/proc/self/status``."""
    path = tmp_path / "status"
    monkeypatch.setattr(memguard, "STATUS_PATH", str(path))

    def write(vmrss_kb: int = 2_100_000, vmhwm_kb: int = 2_322_556) -> None:
        path.write_text(status_text(vmrss_kb, vmhwm_kb), encoding="utf-8")

    write()
    return SimpleNamespace(path=path, write=write)


@pytest.fixture
def probe(monkeypatch):
    """Records every consultation of the guard (the thresholds it was given) and then runs the real check."""
    calls: list = []
    real = memguard.check_upload

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(memguard, "check_upload", spy)
    return calls


def arm(client, meminfo, available_kb: int) -> None:
    """Turn the guard on at the default threshold and set what the machine has left."""
    client.app.state.settings.upload_min_mem_available_kb = THRESHOLD
    meminfo.write(available_kb)


def guard_lines(caplog, level: int = logging.INFO) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == GUARD_LOGGER and record.levelno >= level]


# ---------------------------------------------------------------- the readers and the decision

def test_mem_available_is_read_from_the_meminfo_file(meminfo):
    meminfo.write(1_471_596)
    assert memguard.read_mem_available_kb() == 1_471_596


@pytest.mark.parametrize("available,refused", [(THRESHOLD - 1, True), (THRESHOLD, False), (THRESHOLD + 1, False)],
                         ids=["below", "equal", "above"])
def test_the_check_refuses_only_strictly_below_the_threshold(meminfo, available, refused):
    meminfo.write(available)
    assert memguard.check_upload(THRESHOLD) is refused


def test_a_threshold_of_zero_is_off_and_reads_nothing(meminfo):
    meminfo.path.unlink()                            # not even a file: a read would be a fault, and none happens
    assert memguard.check_upload(0) is False


def test_without_a_meminfo_file_the_guard_is_off_and_silent_per_request(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path / "no-such-meminfo"))
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is False
    assert guard_lines(caplog, logging.DEBUG) == []   # the boot WARNING says so once; a request says nothing


def test_a_read_error_accepts_the_upload_with_one_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path))            # a directory: Linux-style EISDIR / Windows EACCES
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is False
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.WARNING
    assert re.fullmatch(r"upload memory guard unavailable for this request \(\w+\): the upload is accepted", line.getMessage())


@pytest.mark.parametrize("content", ["", "garbage\nwith no colon", "MemTotal: 4021784 kB\n", "MemAvailable: lots kB\n"],
                         ids=["empty", "garbage", "no-memavailable", "not-a-number"])
def test_a_meminfo_that_cannot_be_parsed_accepts_the_upload_with_one_warning(meminfo, caplog, content):
    meminfo.path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is False
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.WARNING and "unavailable for this request" in line.getMessage()


def test_any_failure_inside_the_guard_accepts_the_upload(meminfo, monkeypatch, caplog):
    def broken() -> int:
        raise RuntimeError("the guard's own bug")

    monkeypatch.setattr(memguard, "read_mem_available_kb", broken)
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is False
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.WARNING and "RuntimeError" in line.getMessage()
    assert "own bug" not in line.getMessage()                                    # the type only: a message could carry anything


def test_a_refusal_logs_one_warning_with_the_figure_and_the_threshold(meminfo, caplog):
    meminfo.write(LOW_KB)
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is True
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.WARNING
    assert line.getMessage() == f"upload refused: memory mem_available_kb={LOW_KB} threshold_kb={THRESHOLD}"


def test_an_accepted_upload_logs_nothing_from_the_guard(meminfo, caplog):
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        assert memguard.check_upload(THRESHOLD) is False
    assert guard_lines(caplog, logging.DEBUG) == []


# ---------------------------------------------------------------- the boot posture

def test_a_host_without_proc_meminfo_logs_one_boot_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path / "no-such-meminfo"))
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        memguard.log_boot_posture()
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.WARNING
    assert line.getMessage() == "upload memory guard is OFF: /proc/meminfo is not available on this host"


def test_a_host_with_proc_meminfo_logs_nothing_at_boot(meminfo, caplog):
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        memguard.log_boot_posture()
    assert guard_lines(caplog, logging.DEBUG) == []


# ---------------------------------------------------------------- the memory log line

def test_the_memory_line_carries_the_four_fields_from_the_two_files(meminfo, status):
    meminfo.write(1_471_596, swap_total_kb=4096, swap_free_kb=3096)
    status.write(vmrss_kb=2_100_000, vmhwm_kb=2_322_556)
    assert memguard.memory_line() == "memory vmrss_kb=2100000 vmhwm_kb=2322556 mem_available_kb=1471596 swap_used_kb=1000"


def test_a_kernel_without_swap_lines_reports_no_swap_used(meminfo, status):
    meminfo.path.write_text("MemAvailable: 1471596 kB\n", encoding="utf-8")
    assert memguard.memory_line().endswith("mem_available_kb=1471596 swap_used_kb=0")


def test_the_memory_line_is_none_where_proc_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path / "no-meminfo"))
    monkeypatch.setattr(memguard, "STATUS_PATH", str(tmp_path / "no-status"))
    assert memguard.memory_line() is None


def test_log_memory_logs_the_line_at_info_in_the_allow_listed_format(meminfo, status, caplog):
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        memguard.log_memory()
    (line,) = guard_lines(caplog, logging.DEBUG)
    assert line.levelno == logging.INFO and MEMORY_LINE.fullmatch(line.getMessage())


def test_log_memory_off_linux_logs_nothing_and_does_not_raise(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path / "no-meminfo"))
    monkeypatch.setattr(memguard, "STATUS_PATH", str(tmp_path / "no-status"))
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        memguard.log_memory()
    assert guard_lines(caplog, logging.DEBUG) == []


def test_a_status_file_without_the_memory_fields_raises_for_the_scheduler_to_log(meminfo, status):
    status.path.write_text("Name:\tuvicorn\n", encoding="utf-8")
    with pytest.raises(ValueError):
        memguard.log_memory()


@pytest.mark.skipif(not (os.path.exists("/proc/meminfo") and os.path.exists("/proc/self/status")),
                    reason="needs the real Linux /proc files (runs in the ubuntu serve-shipped CI job)")
def test_the_real_proc_files_parse_in_a_process_marked_non_dumpable():
    """Every other test reads a fake file. Production reads the kernel's, and it marks itself non-dumpable (its /proc entries
    become root-only) BEFORE the maintenance thread first reads /proc/self/status: that order, on the real files."""
    code = ("from semigraph.serve import hardening, memguard\n"
            "hardening.make_process_non_dumpable()\n"
            "print(memguard.read_mem_available_kb())\n"
            "print(memguard.memory_line())\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-500:]
    available, line = done.stdout.split("\n")[:2]
    assert int(available) > 0 and MEMORY_LINE.fullmatch(line)


# ---------------------------------------------------------------- the 60 s log line on the maintenance thread

def thread_with(memory_log):
    clock = FakeClock()
    settings = SimpleNamespace(kill_switch_refresh_s=10.0, lease_renew_s=15.0)
    return MaintenanceThread(FakeBackend(), settings, clock=clock.mono, wall=clock.wall, memory_log=memory_log), clock


def test_the_interval_is_sixty_seconds():
    assert MEMORY_LOG_EVERY_S == 60.0


def test_the_memory_line_is_logged_at_the_first_tick_then_every_sixty_seconds():
    calls: list[float] = []
    thread, clock = thread_with(lambda: calls.append(clock.mono()))
    thread._first_read()                                                          # noqa: SLF001
    thread.tick()
    assert len(calls) == 1                                                        # a baseline line right at boot
    for _ in range(125):                                                          # a tick every second for 125 s
        clock.advance(1)
        thread.tick()
    assert len(calls) == 3                                                        # t=60 and t=120
    assert calls[1] - calls[0] == pytest.approx(60.0) and calls[2] - calls[1] == pytest.approx(60.0)


def test_the_memory_log_does_not_change_when_the_kill_level_and_the_leases_are_due():
    thread, clock = thread_with(lambda: None)
    thread._first_read()                                                          # noqa: SLF001
    assert thread.tick() == pytest.approx(10.0)                                   # the kill refresh, not the 60 s line
    clock.advance(10)
    assert thread.tick() == pytest.approx(5.0)                                    # the leases at t=15


def test_a_memory_log_that_raises_is_logged_and_never_ends_the_thread(caplog):
    def broken() -> None:
        raise ValueError("no VmHWM in /proc/self/status")

    thread, clock = thread_with(broken)
    thread._first_read()                                                          # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.state"):
        thread.tick()
        clock.advance(10)
        thread.tick()                                                             # the kill refresh still runs afterwards
    assert any("state_maintenance_failed" in r.getMessage() and "task=memory_log" in r.getMessage() for r in caplog.records)
    assert [call[0] for call in thread._backend.calls].count("kill") == 2        # noqa: SLF001


# ---------------------------------------------------------------- the route: below / equal / above

def test_the_busy_message_is_a_plain_sentence_without_figures():
    assert wr.MSG_UPLOAD_BUSY == "Uploads are busy right now. Please try again in a few minutes."


@pytest.mark.parametrize("available,status_code", [(THRESHOLD - 1, 503), (THRESHOLD, 202), (THRESHOLD + 1, 202)],
                         ids=["below", "equal", "above"])
def test_an_upload_is_refused_only_when_memory_is_below_the_threshold(client, meminfo, available, status_code):
    arm(client, meminfo, available)
    r = _upload(client)
    assert r.status_code == status_code
    assert r.headers["cache-control"] == "no-store"
    if status_code == 503:
        assert r.headers["retry-after"] == "120"
        assert r.json() == {"detail": wr.MSG_UPLOAD_BUSY}


def test_a_refusal_starts_no_job_stores_nothing_charges_no_upload_and_gives_the_slot_back(client, meminfo, fake_jobs,
                                                                                         fake_store, fresh_drain):
    client.app.state.upload_slots = wr.DrainCountedSlot(1)
    arm(client, meminfo, LOW_KB)
    r = _upload(client)
    assert r.status_code == 503
    assert fake_jobs == []                                                 # no job, so nothing is parsed, embedded or stored
    assert fake_store["reserve_calls"] == 0                                # the daily upload count is not charged
    assert fresh_drain.active == 0 and _plain_slot_is_free(client.app.state.upload_slots)


def test_the_next_upload_is_accepted_once_memory_recovers(client, meminfo, fake_jobs):
    arm(client, meminfo, LOW_KB)
    assert _upload(client).status_code == 503
    meminfo.write(ENOUGH_KB)
    r = _upload(client)
    assert r.status_code == 202 and len(fake_jobs) == 1
    assert client.app.state.upload_slots.acquire(blocking=False) is False  # the accepted job holds the slot (its fake never frees it)


# ---------------------------------------------------------------- asks and reads are unaffected

def test_an_ask_is_admitted_while_memory_is_low(make_client, meminfo, probe):
    class GuardedRouteSettings(RouteSettings):
        upload_min_mem_available_kb = THRESHOLD

    meminfo.write(LOW_KB)
    asked = make_client(settings=GuardedRouteSettings(), routers=[wr.router])
    r = ask(asked)
    assert r.status_code == 200 and events_of(r)[-1]["event"] == "done"
    assert probe == []


def test_workspace_reads_and_creation_are_served_while_memory_is_low(client, meminfo, probe, fake_repo):
    arm(client, meminfo, LOW_KB)
    assert client.get(f"/api/workspace/{WS}", headers=_auth()).status_code == 200
    assert client.get(f"/api/workspace/{WS}/changes?document_id={DOC}&from=1&to=2", headers=_auth()).status_code == 200
    assert client.get(f"/api/workspace/{WS}/evidence/doc:0123456789ab:v1:0001", headers=_auth()).status_code == 200
    assert client.post("/api/workspace", json={}).status_code == 201
    assert client.delete(f"/api/workspace/{WS}", headers=_auth()).status_code == 204
    assert probe == []


def test_only_the_upload_route_consults_the_guard():
    serve_dir = Path(memguard.__file__).parent
    users = sorted(path.name for path in serve_dir.rglob("*.py")
                   if path.name != "memguard.py" and "check_upload" in path.read_text(encoding="utf-8"))
    assert users == ["workspace_routes.py"]
    assert "memguard.check_upload" in Path(wr.__file__).read_text(encoding="utf-8")   # via the module: tests can spy on it


# ---------------------------------------------------------------- S1: the readers take no request data

def test_s1_the_readers_take_no_argument_and_open_only_the_two_path_constants():
    assert memguard.MEMINFO_PATH == "/proc/meminfo" and memguard.STATUS_PATH == "/proc/self/status"
    for reader in (memguard.read_mem_available_kb, memguard.memory_line, memguard.log_memory, memguard.log_boot_posture):
        assert list(inspect.signature(reader).parameters) == []
    assert list(inspect.signature(memguard.check_upload).parameters) == ["threshold_kb"]      # a setting, never request data

    tree = ast.parse(Path(memguard.__file__).read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    opens = [(owner, call) for owner, node in functions.items() for call in ast.walk(node)
             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "open"]
    assert opens and {owner for owner, _ in opens} == {"_read_kb_fields"}                    # one place opens a file
    reads = [call for node in functions.values() for call in ast.walk(node)
             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_read_kb_fields"]
    assert reads and all(isinstance(call.args[0], ast.Name) and call.args[0].id in {"MEMINFO_PATH", "STATUS_PATH"}
                         for call in reads)                                                    # ... and only with a constant
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"logging", "os"}                                                       # no framework, no request type


def test_s1_the_route_hands_the_guard_the_setting_and_nothing_else(client, meminfo, probe):
    arm(client, meminfo, ENOUGH_KB)
    assert _upload(client, filename="secret-name.txt", content=b"secret content",
                   headers={"fly-client-ip": "203.0.113.99"}).status_code == 202
    assert probe == [((THRESHOLD,), {})]


# ---------------------------------------------------------------- S2: the refusal leaks no figure

def test_s2_the_refusal_reveals_no_memory_figure_or_threshold(client, meminfo):
    arm(client, meminfo, LOW_KB)
    r = _upload(client)
    assert r.status_code == 503
    assert r.json() == {"detail": wr.MSG_UPLOAD_BUSY}                                  # the message, and nothing else
    assert not re.search(r"\d", r.text)                                                # no digit anywhere in the body
    headers_text = " ".join(f"{name}={value}" for name, value in r.headers.items())
    assert str(LOW_KB) not in headers_text and str(THRESHOLD) not in headers_text


# ---------------------------------------------------------------- S3: log lines carry nothing about the visitor

def test_s3_the_guards_log_lines_match_an_allow_list_and_nothing_about_the_visitor_is_logged(client, meminfo, caplog):
    client.app.state.settings.client_ip_header = "fly-client-ip"
    arm(client, meminfo, LOW_KB)
    with caplog.at_level(logging.DEBUG):
        refused = _upload(client, filename="secret-name.txt", content=b"secret content",
                          headers={"fly-client-ip": "203.0.113.99"})
        meminfo.write(ENOUGH_KB)
        accepted = _upload(client, filename="secret-name.txt", content=b"another secret content",
                           headers={"fly-client-ip": "203.0.113.99"})
    assert refused.status_code == 503 and accepted.status_code == 202
    ours = guard_lines(caplog, logging.DEBUG)
    assert [record.getMessage() for record in ours if REFUSAL_LINE.fullmatch(record.getMessage())] and len(ours) == 1
    seen = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("semigraph"))
    for secret in (WS, "secret-name", "secret content", "203.0.113.99", TOKEN):
        assert secret not in seen                                       # (httpx's own client log names the URL: not ours)
    assert jobs.ws_hash_for_log(WS) not in "\n".join(record.getMessage() for record in ours)


def test_s3_every_line_the_guard_can_write_is_allow_listed(meminfo, status, tmp_path, monkeypatch, caplog):
    allowed = [REFUSAL_LINE, MEMORY_LINE,
               re.compile(r"^upload memory guard unavailable for this request \(\w+\): the upload is accepted$"),
               re.compile(r"^upload memory guard is OFF: /proc/meminfo is not available on this host$")]
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        meminfo.write(LOW_KB)
        memguard.check_upload(THRESHOLD)
        memguard.log_memory()
        meminfo.path.write_text("garbage", encoding="utf-8")
        memguard.check_upload(THRESHOLD)
        monkeypatch.setattr(memguard, "MEMINFO_PATH", str(tmp_path / "no-meminfo"))
        memguard.log_boot_posture()
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 4
    assert all(any(pattern.fullmatch(line) for pattern in allowed) for line in lines)


# ---------------------------------------------------------------- S4: review item (no new way to refuse service)

def test_s4_the_guard_keeps_no_state_per_visitor_so_only_real_memory_use_can_trip_it():
    """S4 is a review item (docs/specs/upload-memory-guard.md): the guard's whole input is one number the machine reports.
    Pinned structurally: the module holds no module-level dictionary, list or set (nothing a visitor could feed or fill),
    and the decision is a pure function of that number and the threshold."""
    tree = ast.parse(Path(memguard.__file__).read_text(encoding="utf-8"))
    containers = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                  and isinstance(node.value, (ast.Dict, ast.List, ast.Set))]
    assert containers == []
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Global)]


# ---------------------------------------------------------------- S5: the guard cannot be an oracle before the earlier gates

def _upload_refused_earlier_by(client, kind: str, monkeypatch, fresh_drain, fake_repo):
    """Make the named earlier gate refuse and post the upload (the machine is already short of memory); returns the
    response."""
    state = client.app.state
    if kind == "uploads_off":
        state.settings.uploads_enabled = False
    elif kind == "draining":
        fresh_drain.begin()
    elif kind == "wrong_token":
        return _upload(client, headers={"X-Workspace-Token": "wrong-token"})
    elif kind == "kill_level":
        state.state.kill = "on"
    elif kind == "bad_turnstile":
        state.settings.turnstile_secret_key = "secret"

        async def fail(*args, **kwargs):
            return False

        monkeypatch.setattr(guard, "verify_turnstile", fail)
    elif kind == "upload_window":
        state.upload_limiter = SimpleNamespace(allow=lambda key: False)
    elif kind == "content_length":
        state.settings.upload_max_bytes = 10
        return _upload(client, content=b"x" * 50_000)
    elif kind == "junk_file":
        return _upload(client, filename="a.bin", content=b"\x00\x01binary junk", content_type="application/octet-stream")
    elif kind == "malformed_body":
        return client.post(f"/api/workspace/{WS}/documents",
                           headers={**_auth(), **_turnstile(), "content-type": "multipart/form-data"}, content=b"garbage")
    elif kind == "unknown_document":
        return _upload(client, data={"document_id": "ffffffffffff"})
    elif kind == "unchanged":
        fake_repo.documents[DOC] = {"versions": {1: __import__("hashlib").sha256(b"hello world").hexdigest()}}
        return _upload(client, data={"document_id": DOC})
    elif kind == "max_documents":
        fake_repo.documents = {f"doc{i:09d}": {"versions": {1: "h"}} for i in range(3)}
    elif kind == "slot_busy":
        state.upload_slots.acquire()
    return _upload(client)


S5_CASES = {                                   # kind -> the status that gate answers on its own
    "uploads_off": 503, "draining": 503, "wrong_token": 404, "kill_level": 503, "bad_turnstile": 403, "upload_window": 429,
    "content_length": 413, "junk_file": 415, "malformed_body": 400, "unknown_document": 404, "unchanged": 200,
    "max_documents": 429, "slot_busy": 429}


@pytest.mark.parametrize("kind", list(S5_CASES))
def test_s5_every_earlier_gate_answers_for_itself_and_never_reaches_the_guard(client, meminfo, probe, fake_jobs, fake_store,
                                                                            fake_repo, monkeypatch, fresh_drain, kind):
    arm(client, meminfo, LOW_KB)
    r = _upload_refused_earlier_by(client, kind, monkeypatch, fresh_drain, fake_repo)
    assert r.status_code == S5_CASES[kind]
    assert wr.MSG_UPLOAD_BUSY not in r.text
    assert probe == []                                            # the guard was never consulted: no oracle before the gate
    assert fake_jobs == [] and fake_store["reserve_calls"] == 0


def test_s5_the_guard_runs_after_the_token_turnstile_and_slot_and_before_the_daily_charge_and_the_job(client, meminfo,
                                                                                                     fake_repo, monkeypatch):
    order: list[str] = []
    state = client.app.state
    state.settings.turnstile_secret_key = "secret"
    arm(client, meminfo, ENOUGH_KB)

    authenticate = fake_repo.authenticate

    def authenticate_noted(driver, ws, token):
        order.append("workspace token")
        return authenticate(driver, ws, token)

    async def turnstile(*args, **kwargs):
        order.append("turnstile")
        return True

    class NotedSlot(type(state.upload_slots)):
        def acquire(self, blocking=True, timeout=None):
            order.append("slot")
            return super().acquire(blocking, timeout)

    def reserve(driver, limit, day=None):
        order.append("daily budget")
        return True

    def run_upload_job(app, **kw):
        order.append("job")
        return "job-1"

    real_check = memguard.check_upload

    def check(threshold_kb):
        order.append("memory guard")
        return real_check(threshold_kb)

    monkeypatch.setattr(repo, "authenticate", authenticate_noted)
    monkeypatch.setattr(guard, "verify_turnstile", turnstile)
    monkeypatch.setattr(store, "reserve_daily_upload", reserve)
    monkeypatch.setattr(jobs, "run_upload_job", run_upload_job)
    monkeypatch.setattr(memguard, "check_upload", check)
    state.upload_slots = NotedSlot(1)

    assert _upload(client).status_code == 202
    assert order == ["workspace token", "turnstile", "slot", "memory guard", "daily budget", "job"]


# ---------------------------------------------------------------- S6: a fault of the guard never changes an earlier gate, never refuses

def test_s6_a_guard_that_cannot_read_never_accepts_what_an_earlier_gate_refused(client, meminfo, probe, monkeypatch):
    client.app.state.settings.upload_min_mem_available_kb = THRESHOLD
    meminfo.path.unlink()
    meminfo.path.mkdir()                                                                  # present but unreadable as a file
    client.app.state.settings.turnstile_secret_key = "secret"

    async def fail(*args, **kwargs):
        return False

    monkeypatch.setattr(guard, "verify_turnstile", fail)
    assert _upload(client).status_code == 403                                             # the bot check still refuses
    assert _upload(client, headers={"X-Workspace-Token": "wrong-token"}).status_code == 404
    assert probe == []


@pytest.mark.parametrize("breakage", ["directory", "garbage", "missing-file", "reader-raises"])
def test_s6_a_guard_that_cannot_read_never_refuses_an_upload(client, meminfo, fake_jobs, monkeypatch, caplog, breakage):
    client.app.state.settings.upload_min_mem_available_kb = THRESHOLD
    if breakage == "directory":
        meminfo.path.unlink()
        meminfo.path.mkdir()
    elif breakage == "garbage":
        meminfo.path.write_text("not a meminfo", encoding="utf-8")
    elif breakage == "missing-file":
        meminfo.path.unlink()
    else:
        monkeypatch.setattr(memguard, "read_mem_available_kb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.DEBUG, logger=GUARD_LOGGER):
        r = _upload(client)
    assert r.status_code == 202 and len(fake_jobs) == 1
    warnings = [record for record in guard_lines(caplog, logging.WARNING)]
    assert len(warnings) == (0 if breakage == "missing-file" else 1)


def test_s6_the_guard_off_by_setting_never_touches_proc_and_accepts(client, meminfo, fake_jobs):
    client.app.state.settings.upload_min_mem_available_kb = 0
    meminfo.write(1)
    assert _upload(client).status_code == 202 and len(fake_jobs) == 1
