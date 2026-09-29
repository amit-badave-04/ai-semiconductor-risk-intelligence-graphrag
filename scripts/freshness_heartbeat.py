"""Freshness heartbeat: is the deployed monitor still healthy? (M4, docs/v2/M4_PLAN.md 4.1, revision 3 section 14.3)

    python scripts/freshness_heartbeat.py https://semigraph.fly.dev/api/freshness
    python scripts/freshness_heartbeat.py <url> --body-file recorded.json   # test a recorded body, no network

Exit codes:
- 0  the public ``GET /api/freshness`` body reports ``status`` in ``ok``/``never`` AND ``configured`` is true.
- 1  ``status`` is ``error`` / ``stale`` / ``unconfigured`` (or the body is otherwise not what a healthy monitor
     reports) — the ONLY case a CI run should fail on.
- 0, with a ``::warning::`` line  the app could not be reached, or the Fly edge proxy answered with a 5xx. The
     owner's ``STOP`` scales the API machine to zero on purpose, so "currently asleep" is not a heartbeat failure;
     the next scheduled run (or the first real request) wakes it.

This is exactly the logic ``.github/workflows/freshness-heartbeat.yml`` runs — no separate copy to drift from it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "semigraph-freshness-heartbeat"
HEALTHY_STATUSES = ("ok", "never")


class Unreachable(Exception):
    """The app could not be reached, or the Fly edge answered 5xx — an intentional scale-to-zero symptom, never a
    heartbeat failure (see the module docstring)."""


def fetch_status(url: str, timeout: float = 20.0) -> dict:
    """The parsed JSON body of ``url``. Raises :class:`Unreachable` for a network failure or a 5xx response; any
    other HTTP error (a 4xx — the route itself is wrong) propagates as a genuine, hard failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - a fixed, operator-supplied URL
            if resp.status >= 500:
                raise Unreachable(f"the app answered HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise Unreachable(f"the app answered HTTP {e.code}") from e
        raise
    except urllib.error.URLError as e:
        raise Unreachable(f"the app is unreachable: {e.reason}") from e
    except TimeoutError as e:
        raise Unreachable(f"the app is unreachable: timed out ({e})") from e
    except OSError as e:
        # A connection that dropped mid-read (a Fly wake tearing down the half-open socket, a reset, ...) raises a
        # raw OSError here, not a URLError — urlopen() already succeeded before the connection failed on .read().
        raise Unreachable(f"the app is unreachable: {e}") from e


def evaluate(body: dict) -> int:
    """The process exit code for an already-fetched ``/api/freshness`` body.

    ``status: "disabled"`` (``FRESHNESS_ENABLED=false``, docs/v2/M4_PLAN.md 15.10) is the SAME kind of intentional
    operator choice as the scale-to-zero case above: a heartbeat failure would mean "someone should investigate",
    and nobody needs to investigate a feature switched off on purpose. It is checked before, and independently of,
    ``configured`` — an explicitly disabled monitor's ``SEC_USER_AGENT`` state is not a health signal either way.
    """
    status = body.get("status")
    configured = bool(body.get("configured"))
    if status == "disabled":
        print("freshness ok: status=disabled (FRESHNESS_ENABLED=false, an intentional operator choice)")
        return 0
    if status in HEALTHY_STATUSES and configured:
        print(f"freshness ok: status={status} configured={configured} pending_count={body.get('pending_count')}")
        return 0
    print(f"freshness NOT ok: status={status!r} configured={configured} error={body.get('error')!r}",
          file=sys.stderr)
    return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", help="the GET /api/freshness URL to check")
    ap.add_argument("--body-file", help="read the response body from this JSON file instead of a network call "
                                        "(a recorded ok/stale/error body, for testing without hitting the network)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        body = (json.loads(Path(args.body_file).read_text(encoding="utf-8")) if args.body_file
                else fetch_status(args.url))
    except Unreachable as e:
        print(f"::warning::{e}")
        return 0
    return evaluate(body)


if __name__ == "__main__":
    sys.exit(main())
