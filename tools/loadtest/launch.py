"""The generator image's entry point: the CPU sampler next to Locust, as one container command. Pure stdlib.

    LOADTEST_ROLE=master|worker|standalone  python -m tools.loadtest.launch

* ``master``: ``locust --master --headless --expect-workers N`` (the staged shape on the master ends the run when it is over);
* ``worker``: ``locust --worker --master-host H`` (``H`` is the master's ``.internal`` name on Fly's private network);
* ``standalone``: one process, no workers (a local or pilot run).

Next to it runs ``tools.loadtest.cpu_watch --role generator`` as its own process, writing ``$LOADTEST_OUT_DIR/cpu/gen-<name>.jsonl``;
the Locust logs and the cpu file are what ``scripts/loadtest_report.py`` reads. Secrets (``LOADTEST_ORIGIN_AUTH``) reach Locust through the
environment only: they are never put in an argument, so they cannot appear in a process listing or in a log of the command.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Mapping

LOCUSTFILE = "tools/loadtest/locustfile.py"
ROLES = ("master", "worker", "standalone")


class LaunchError(ValueError):
    pass


def build_commands(env: Mapping[str, str], *, python: str = sys.executable) -> tuple[list[str], list[str]]:
    """``(cpu_watch_command, locust_command)`` for this environment, or :class:`LaunchError`."""
    role = env.get("LOADTEST_ROLE", "")
    if role not in ROLES:
        raise LaunchError(f"LOADTEST_ROLE must be one of {ROLES}, got {role!r}")
    out = env.get("LOADTEST_OUT_DIR", "/out")
    name = env.get("LOADTEST_WORKER_NAME") or socket.gethostname()
    cpu = [python, "-m", "tools.loadtest.cpu_watch", "--name", f"gen-{name}", "--role", "generator", "--match", "locust",
           "--out", f"{out}/cpu/gen-{name}.jsonl"]
    locust = [python, "-m", "locust", "-f", LOCUSTFILE]
    host = env.get("LOADTEST_HOST", "")
    if role in ("master", "standalone"):
        if not host:
            raise LaunchError("LOADTEST_HOST (the staging API URL) is required for the master / standalone role")
        locust += ["--headless", "--host", host, "--only-summary"]
    if role == "master":
        locust += ["--master", "--expect-workers", str(int(env.get("LOADTEST_EXPECT_WORKERS") or 2))]
    elif role == "worker":
        master = env.get("LOADTEST_MASTER_HOST", "")
        if not master:
            raise LaunchError("LOADTEST_MASTER_HOST is required for the worker role")
        digit = env.get("LOADTEST_WORKER", "")
        if not (digit.isdigit() and len(digit) == 1):
            raise LaunchError("LOADTEST_WORKER (the salt's worker digit 0-9, unique per generator machine) is required for the "
                              "worker role: two workers defaulting to the same digit would send the same salted questions")
        locust += ["--worker", "--master-host", master]
    return cpu, locust


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        cpu_cmd, locust_cmd = build_commands(env)
    except LaunchError as e:
        print(f"launch: {e}", file=sys.stderr)
        return 2
    sampler = subprocess.Popen(cpu_cmd)                                      # noqa: S603 - fixed argv built above
    try:
        return subprocess.call(locust_cmd)                                   # noqa: S603
    finally:
        sampler.terminate()
        try:
            sampler.wait(10)
        except subprocess.TimeoutExpired:
            sampler.kill()


if __name__ == "__main__":
    sys.exit(main())
