"""``python -m tools.mockllm``: serve the mock on ``MOCKLLM_HOST`` and ``MOCKLLM_PORT`` (default 8000).

Default host: ``::`` when this machine can bind an IPv6 socket (on Linux that listener is dual-stack, so IPv4 clients reach
it too), else ``0.0.0.0``. It matters on Fly: a ``<app>.internal`` name resolves to an IPv6 (6PN) address only, so a mock
bound to ``0.0.0.0`` would be unreachable from the service under test. Set ``MOCKLLM_HOST`` to override.

Access logging is off: a log line per streamed request is CPU the load test must not pay.
"""

import os
import socket
from collections.abc import Callable, Mapping

import uvicorn

from .server import create_app

DEFAULT_PORT = 8000


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


def main() -> None:
    uvicorn.run(create_app(), host=choose_host(os.environ), port=int(os.environ.get("MOCKLLM_PORT", str(DEFAULT_PORT))),
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
