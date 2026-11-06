"""``python -m tools.mockllm``: serve the mock on ``MOCKLLM_HOST`` and ``MOCKLLM_PORT`` (default 8000).

Default host: ``::`` when this machine can bind an IPv6 socket, else ``0.0.0.0``. It matters on Fly: a ``<app>.internal`` name
resolves to an IPv6 (6PN) address only, so a mock bound to ``0.0.0.0`` would be unreachable from the service under test, while
Fly's HTTP health check arrives over IPv4, so a mock bound to IPv6 only fails its deploy. The listener must take both. Set
``MOCKLLM_HOST`` to override.

The listening socket is created HERE, not by uvicorn: asyncio sets ``IPV6_V6ONLY`` on a socket it creates for ``::`` (uvicorn
asks it to), which makes the listener IPv6-only on Linux too (seen on Fly: /proc/net/tcp6 held it, /proc/net/tcp did not).
:func:`open_listener` clears the option and hands the bound, listening socket to uvicorn.

Access logging is off: a log line per streamed request is CPU the load test must not pay.
"""

import logging
import os
import socket
from collections.abc import Callable, Mapping

import uvicorn

from .server import create_app

DEFAULT_PORT = 8000
LISTEN_BACKLOG = 2048

logger = logging.getLogger(__name__)


def ipv6_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::", 0))
    except OSError:
        return False
    return True


def choose_host(env: Mapping[str, str], can_bind_ipv6: Callable[[], bool] = ipv6_available) -> str:
    if env.get("MOCKLLM_HOST"):
        return env["MOCKLLM_HOST"]
    return "::" if can_bind_ipv6() else "0.0.0.0"  # noqa: S104 - private network only


def allow_ipv4_clients(sock: socket.socket) -> bool:
    """Make an IPv6 listening socket dual-stack (``IPV6_V6ONLY = 0``). False, with a warning, where the platform refuses: the
    listener then stays IPv6-only, which still serves 6PN, and the only loss is an IPv4 client such as Fly's health check."""
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except (OSError, AttributeError) as error:
        logger.warning("could not make the IPv6 listener dual-stack (%s): IPv4 clients, such as Fly's HTTP health check, "
                       "will be refused", error)
        return False
    return True


def open_listener(host: str, port: int, backlog: int = LISTEN_BACKLOG) -> socket.socket:
    """A bound, listening TCP socket for ``host:port``: IPv6 when ``host`` holds a colon (dual-stack, so ``::`` also takes
    IPv4 clients), else IPv4. ``port`` 0 picks a free one (``sock.getsockname()[1]``). The caller owns the socket."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            allow_ipv4_clients(sock)
        sock.bind((host, port))
        sock.listen(backlog)
    except OSError:
        sock.close()
        raise
    return sock


def main() -> None:
    host, port = choose_host(os.environ), int(os.environ.get("MOCKLLM_PORT", str(DEFAULT_PORT)))
    sock = open_listener(host, port)
    try:
        config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning", access_log=False)
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
