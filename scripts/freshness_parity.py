"""Freshness parity (D2, docs/v2/M4_PLAN.md 4.1, G6): does the live monitor's "pending" set agree with the data
lake's own idea of pending filings?

    python -m scripts.freshness_parity --as-of 2026-09-24

Runs ``semigraph.serve.monitor.check_once(..., today=as_of)`` against the LOCAL graph and
``semigraph.ingestion.freshness.pending_filings`` / ``federal_register_pending`` against the LOCAL DATA LAKE, with
ONE memoizing fetch shared by both paths — so a symmetric difference reflects a logic disagreement between the two
readers, never two different network reads of a fast-moving EDGAR feed. Prints both pending sets, the Federal
Register counts, and the symmetric difference (each differing row carries its form, so an unparsed amendment — the
expected, owner-accepted cause, docs/v2/M4_PLAN.md risk 2 — is visible at a glance); writes the same JSON to
``artifacts/freshness_parity.json``.

Reads the LOCAL graph (``NEO4J_URI`` in ``.env``) and the LOCAL data lake (``data/raw/...``); never run in CI, and
never against the throwaway integration-test instance — this compares real data, not a fixture. :func:`compare` and
:func:`memoizing_fetch` are pure and unit-tested with fakes (``tests/test_serve_freshness_parity.py``); only
:func:`run` touches the network or a database.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from semigraph.config import Settings, get_settings
from semigraph.graph.client import get_driver
from semigraph.ingestion import federal_register
from semigraph.ingestion.freshness import federal_register_pending, pending_filings
from semigraph.serve.monitor import check_once

ARTIFACT_PATH = Path("artifacts/freshness_parity.json")
SEC_FETCH_TIMEOUT_S = 20
SEC_FETCH_PAUSE_S = 0.15


def memoizing_fetch(real_fetch):
    """Wraps ``real_fetch(url) -> dict`` so the SAME url is never fetched twice across the two comparison paths."""
    cache: dict[str, dict] = {}

    def fetch(url: str) -> dict:
        if url not in cache:
            cache[url] = real_fetch(url)
        return cache[url]

    return fetch


def _sec_fetch(settings: Settings):
    """The same retrying, paced, identity-declaring fetcher ``serve.monitor`` builds for the real network path."""
    identity = settings.sec_user_agent.strip() or "semigraph"

    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": identity})
        with urllib.request.urlopen(req, timeout=SEC_FETCH_TIMEOUT_S) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(SEC_FETCH_PAUSE_S)
        return data

    return federal_register.with_retries(_get)


def _accession_set(rows: list[dict]) -> set[str]:
    return {r["accession_no"] for r in rows}


def _row_by_accession(rows: list[dict]) -> dict[str, dict]:
    return {r["accession_no"]: r for r in rows}


def compare(monitor_pending: list[dict], lake_pending: list[dict]) -> dict:
    """The pure comparison at the heart of the parity gate: ``(pending accessions the monitor sees, the lake sees)``
    -> agreement + the symmetric difference, each row carrying enough (ticker, form, accession) to explain a
    disagreement by eye."""
    monitor_set, lake_set = _accession_set(monitor_pending), _accession_set(lake_pending)
    by_accession = {**_row_by_accession(lake_pending), **_row_by_accession(monitor_pending)}
    return {
        "monitor_count": len(monitor_set),
        "lake_count": len(lake_set),
        "agree": monitor_set == lake_set,
        "only_in_monitor": [by_accession[a] for a in sorted(monitor_set - lake_set)],
        "only_in_lake": [by_accession[a] for a in sorted(lake_set - monitor_set)],
    }


def run(as_of: str) -> dict:
    settings = get_settings()
    fetch = memoizing_fetch(_sec_fetch(settings))
    driver = get_driver(settings)
    try:
        monitor_result = check_once(driver, settings, fetch=fetch, today=as_of)
    finally:
        driver.close()
    lake_pending = pending_filings(settings, as_of=as_of, fetch=fetch)
    fr = federal_register_pending(settings, as_of=as_of, fetch=fetch)
    diff = compare(monitor_result["pending_filings"], lake_pending)
    return {
        "as_of": as_of,
        "monitor": {"pending_filings": monitor_result["pending_filings"],
                    "federal_register": monitor_result["federal_register"], "unresolved": monitor_result["unresolved"]},
        "lake": {"pending_filings": lake_pending, "federal_register": fr},
        "diff": diff,
        "federal_register_agrees": monitor_result["federal_register"]["graph_count"] == fr["stored_count"],
    }


def _print_report(result: dict) -> None:
    diff = result["diff"]
    print(f"monitor pending: {diff['monitor_count']}  lake pending: {diff['lake_count']}")
    print(f"symmetric difference: {len(diff['only_in_monitor'])} only in monitor, "
          f"{len(diff['only_in_lake'])} only in lake")
    for label, rows in (("only in monitor", diff["only_in_monitor"]), ("only in lake", diff["only_in_lake"])):
        for row in rows:
            print(f"  {label}: {row.get('ticker')} {row.get('form')} {row.get('accession_no')}")
    fr_graph = result["monitor"]["federal_register"]["graph_count"]
    fr_stored = result["lake"]["federal_register"]["stored_count"]
    print(f"federal register: graph_count={fr_graph} stored_count={fr_stored} agree={result['federal_register_agrees']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Freshness parity: the live monitor's pending set vs the data lake's.")
    ap.add_argument("--as-of", required=True, help="YYYY-MM-DD, the parity snapshot date (D2, G6)")
    args = ap.parse_args(argv)

    result = run(args.as_of)
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    _print_report(result)
    print(f"wrote {ARTIFACT_PATH}")
    return 0 if result["diff"]["agree"] and result["federal_register_agrees"] else 1


if __name__ == "__main__":
    sys.exit(main())
