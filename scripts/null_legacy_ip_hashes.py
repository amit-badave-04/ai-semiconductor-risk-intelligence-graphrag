"""Null the unsalted IP hashes still stored in the query ledger (M5a I3; docs/v2/M5_DECISIONS.md decision 12).
IRREVERSIBLE.

    python null_legacy_ip_hashes.py [--yes] [--batch N]

Before the pepper, every ``SvcQuery`` row stored the client address as a 64-bit unsalted SHA-256, which the whole IPv4
space can brute-force. This sets ``ip_hash = null`` and ``ip_hash_v = 0`` on every row that has an ``ip_hash`` and no
``ip_hash_v`` (rows written with a pepper are never touched), committing ``--batch`` rows at a time. The per-address
history of those rows is gone for good: there is no undo. Without ``--yes`` it only counts and prints how many rows it
WOULD null, and which database it is pointed at.

Run it once, at the cutover, on the owner's go, after the image that writes ``ip_hash_v`` is live (a writer without the
pepper would keep adding legacy rows). It must run on the serve machine against its own Neo4j (the database is private
on Fly) and reads its connection settings from that machine's environment. The runtime image ships only the installed
package, so put the script on the machine first (production refuses to start without IP_HASH_PEPPER, so the machine's
environment must carry it), then run it there:

    flyctl ssh sftp shell -a semigraph
        put scripts/null_legacy_ip_hashes.py /tmp/null_legacy_ip_hashes.py
    flyctl ssh console -a semigraph -C "python /tmp/null_legacy_ip_hashes.py"        # dry run: counts, changes nothing
    flyctl ssh console -a semigraph -C "python /tmp/null_legacy_ip_hashes.py --yes"  # the IRREVERSIBLE null

Check afterwards:  MATCH (q:SvcQuery) WHERE q.ip_hash IS NOT NULL AND q.ip_hash_v IS NULL RETURN count(q)  ->  0.
Exit status: 0 on success (or a dry run); 1 when it could not connect, a query failed, or legacy rows remain. Only the
class of an error is printed, never its text: a driver error can carry the connection URI.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from neo4j import Driver

from semigraph.serve import store

DEFAULT_BATCH = 5000


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true",
                        help="really null the legacy hashes (IRREVERSIBLE); without it, count and change nothing")
    parser.add_argument("--batch", type=_positive_int, default=DEFAULT_BATCH,
                        help=f"rows per committed batch (default {DEFAULT_BATCH})")
    return parser


def _target(uri: str, database: str) -> str:
    """Where the null would act, for the owner to check before ``--yes``: the host and port, never the user info."""
    parts = urlsplit(uri)
    host = f"{parts.hostname}:{parts.port}" if parts.hostname and parts.port else parts.hostname or "unknown host"
    return f"{host}, database {database or '<server default>'}"


def _connect_with_app_settings() -> Driver:
    from semigraph.config import get_settings
    from semigraph.graph.client import get_driver

    settings = get_settings()
    print(f"target: {_target(settings.neo4j_uri, settings.neo4j_database)}")
    return get_driver(settings)


def _fail(what: str, error: Exception) -> int:
    print(f"ERROR: {what}: {type(error).__name__}", file=sys.stderr)
    return 1


def _run(driver: Driver, batch: int, really: bool) -> int:
    legacy = store.count_legacy_ip_hashes(driver)
    print(f"ledger rows with an unsalted ip_hash and no ip_hash_v: {legacy}")
    if not really:
        print("dry run: nothing was changed. --yes nulls those hashes (ip_hash = null, ip_hash_v = 0); "
              "that is IRREVERSIBLE.")
        return 0
    nulled = store.null_legacy_ip_hashes(driver, batch=batch)
    remaining = store.count_legacy_ip_hashes(driver)
    print(f"nulled: {nulled} (IRREVERSIBLE: those rows no longer carry an address hash)")
    print(f"legacy rows remaining: {remaining}")
    if remaining:
        print("ERROR: legacy rows remain; a writer without the pepper may still be running. "
              "Run this again once it is gone.", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None, *, connect: Callable[[], Driver] | None = None) -> int:
    """``connect`` returns the driver (a test passes its own); the default opens the app's configured Neo4j."""
    args = _parser().parse_args(argv)
    try:
        driver = (connect or _connect_with_app_settings)()
    except Exception as e:  # noqa: BLE001 - report the class only, exit non-zero
        return _fail("could not connect to Neo4j", e)
    try:
        return _run(driver, args.batch, args.yes)
    except Exception as e:  # noqa: BLE001
        return _fail("the ledger query failed", e)
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
