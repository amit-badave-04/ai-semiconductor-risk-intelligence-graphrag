"""``python -m tools.mockllm``: the listener the mock binds (``tools/mockllm/__main__.py``).

Seen on Fly 2026-11-05: the mock bound host ``::`` through ``uvicorn.run``, and asyncio sets ``IPV6_V6ONLY`` on a socket it
creates for ``::``, so the listener took [::]:8000 only (``/proc/net/tcp6`` held it, ``/proc/net/tcp`` did not). Fly's HTTP
health check arrives over IPv4, got "connection refused", and the deploy reported a health-check timeout although the mock
worked over 6PN. What is pinned:

* the listening socket the module creates is dual-stack (``IPV6_V6ONLY == 0``) with ``SO_REUSEADDR``, bound and listening;
* a REAL ``python -m tools.mockllm`` process (the exact Fly entry point) answers ``GET /healthz`` 200 on BOTH 127.0.0.1 and ::1;
* ``MOCKLLM_HOST`` still overrides the host, and an IPv4 host gets an IPv4 socket.

Loopback only: no provider, no database, no network beyond this machine. The IPv6 half skips where the OS has no IPv6 loopback.
"""

import contextlib
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                    # ``tools`` is not an installed package: the repository root makes it importable
    sys.path.insert(0, str(ROOT))

from tools.mockllm import __main__ as entry  # noqa: E402

STARTUP_DEADLINE_S = 40.0


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(not _has_ipv6_loopback(), reason="this OS has no IPv6 loopback")


def _free_port() -> int:
    family, host = (socket.AF_INET6, "::") if _has_ipv6_loopback() else (socket.AF_INET, "127.0.0.1")
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


# --- the socket itself ------------------------------------------------------------------------------------------------------------

@needs_ipv6
def test_the_wildcard_ipv6_listener_is_dual_stack_so_ipv4_health_checks_reach_it():
    sock = entry.open_listener("::", 0)
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        port = sock.getsockname()[1]
        assert port != 0
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:      # listening: the kernel accepts the connect
            assert client.getpeername()[1] == port
    finally:
        sock.close()


def test_an_ipv4_host_gets_an_ipv4_socket():
    sock = entry.open_listener("127.0.0.1", 0)
    try:
        assert sock.family == socket.AF_INET and sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    finally:
        sock.close()


class _SocketWithoutDualStack:
    def __init__(self, error):
        self.error, self.calls = error, []

    def setsockopt(self, *args):
        self.calls.append(args)
        raise self.error


@pytest.mark.parametrize("error", [OSError("not allowed here"), AttributeError("no IPV6_V6ONLY")])
def test_a_platform_that_refuses_dual_stack_keeps_its_ipv6_listener_and_says_so(caplog, error):
    sock = _SocketWithoutDualStack(error)
    with caplog.at_level("WARNING"):
        assert entry.allow_ipv4_clients(sock) is False
    assert "IPv4" in caplog.text and "health check" in caplog.text


# --- the real process, as Fly starts it -----------------------------------------------------------------------------------------

def _get_status(host: str, port: int, path: str = "/healthz") -> int:
    connection = http.client.HTTPConnection(host, port, timeout=3)       # direct: no proxy variable is read
    try:
        connection.request("GET", path)
        return connection.getresponse().status
    finally:
        connection.close()


def _probe(host: str, port: int) -> int | str:
    try:
        return _get_status(host, port)
    except OSError as error:
        return f"{type(error).__name__}: {error}"


@contextlib.contextmanager
def _running_mock(tmp_path: Path, **extra_env: str):
    port = _free_port()
    env = {key: value for key, value in os.environ.items() if key != "MOCKLLM_HOST"}
    env.update({"MOCKLLM_PORT": str(port), "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
                "PYTHONUNBUFFERED": "1", **extra_env})
    log_path = tmp_path / "mockllm.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "tools.mockllm"], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            yield process, port, lambda: log_path.read_text(encoding="utf-8", errors="replace")
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)


def _wait_until_answering(process, port: int, hosts: tuple[str, ...], read_log) -> None:
    deadline = time.monotonic() + STARTUP_DEADLINE_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the mock exited with {process.returncode} before it answered:\n{read_log()}")
        if any(_probe(host, port) == 200 for host in hosts):
            return
        time.sleep(0.25)
    pytest.fail(f"the mock did not answer on any of {hosts} within {STARTUP_DEADLINE_S:.0f} s:\n{read_log()}")


def test_the_default_listener_answers_healthz_over_ipv4_and_ipv6(tmp_path):
    hosts = ("127.0.0.1", "::1") if _has_ipv6_loopback() else ("127.0.0.1",)
    with _running_mock(tmp_path) as (process, port, read_log):
        _wait_until_answering(process, port, hosts, read_log)
        results = {host: _probe(host, port) for host in hosts}
    assert results == {host: 200 for host in hosts}, f"{results}\n{read_log()}"


def test_mockllm_host_still_overrides_the_default(tmp_path):
    with _running_mock(tmp_path, MOCKLLM_HOST="127.0.0.1") as (process, port, read_log):
        _wait_until_answering(process, port, ("127.0.0.1",), read_log)
        assert _probe("127.0.0.1", port) == 200
        if _has_ipv6_loopback():
            assert _probe("::1", port) != 200, "an IPv4-only override must not listen on IPv6"
