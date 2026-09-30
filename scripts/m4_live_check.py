"""Read-only live checks for M4 gate G9 (docs/v2/M4_PLAN.md 9, 10.6) against a deployed Semigraph.

Every request is free: no LLM call, no upload, no Turnstile token. The agent + workspace ask is refused by the strategy
guard (400) before Turnstile, the answer slot and any spend; the workspace create without a token is refused by the
upload Turnstile check (403). Writes ``artifacts/m4_live_gates.json`` (status codes and counts only, never a body that
could carry user text).

    uv run python -m scripts.m4_live_check --base-url https://semigraph.fly.dev \
        --expect-examples <pre-deploy examples.json> --expect-snapshot <pre-deploy snapshot id>

Both expectations are read from the live service BEFORE the deploy (``GET /api/examples``, ``GET /api/stats``
``snapshot.id``): G9 requires the same example ids and the same snapshot afterwards (the API redeploy never touches the
graph). The 2 vCPU / 4 GB machine check is taken separately over ``flyctl ssh`` (``nproc``, ``MemTotal``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "artifacts" / "m4_live_gates.json"
REQUEST_TIMEOUT_S = 60
PUBLIC_DOC_ID = "doc:0123456789ab:v1:0001"
FAKE_WORKSPACE_ID = "0" * 32
DOSSIER_TICKER = "NVDA"
AGENT_QUESTION = "What export-control risks does Nvidia report?"
FRESHNESS_OFF_STATES = frozenset({"disabled", "unconfigured"})
DOSSIER_LISTS = ("filings", "metrics", "active_risks", "edges", "rules")


def example_ids(payload: dict) -> list[str]:
    """The sorted example ids of a ``GET /api/examples`` payload."""
    return sorted(e["id"] for e in (payload or {}).get("examples", []) if isinstance(e, dict) and "id" in e)


def evaluate_g9(obs: dict, expected_ids: list[str], expected_snapshot: str) -> list[str]:
    """Every G9 assertion that FAILED, given the observations :func:`observe` records; ``[]`` means G9 passes. A
    missing observation counts as failed."""
    return [msg for ok, msg in _g9_checks(obs, expected_ids, expected_snapshot) if not ok]


def _g9_checks(obs: dict, expected_ids: list[str], expected_snapshot: str) -> list[tuple[bool, str]]:
    stats = obs.get("stats") or {}
    freshness = obs.get("freshness") or {}
    dossier = obs.get("dossier") or {}
    risk_changes = obs.get("risk_changes") or {}
    labels = stats.get("node_labels") or []
    checks = [
        (obs.get("healthz") == 200, "/healthz did not answer 200"),
        (obs.get("index") == 200 and obs.get("index_site_key_filled") is True,
         "the page did not answer 200 with the Turnstile site key filled in"),
        (stats.get("status") == 200 and bool(expected_snapshot) and stats.get("snapshot_id") == expected_snapshot,
         "/api/stats snapshot id changed across the deploy"),
        (stats.get("status") == 200 and stats.get("uploads_enabled") is True, "/api/stats does not report uploads enabled"),
        (stats.get("status") == 200 and bool(labels) and not any(label.startswith("User") for label in labels),
         "/api/stats graph counts are missing or include User* labels"),
        (stats.get("freshness_status") not in FRESHNESS_OFF_STATES | {None},
         "the /api/stats freshness block says the monitor is off or unconfigured"),
        (freshness.get("status") == 200 and freshness.get("enabled") is True and freshness.get("configured") is True,
         "/api/freshness is not enabled and configured"),
        (obs.get("examples_status") == 200 and obs.get("example_ids") == sorted(expected_ids) and bool(expected_ids),
         "/api/examples does not return the same example ids as before the deploy"),
        (dossier.get("status") == 200 and dossier.get("filings", 0) > 0 and dossier.get("active_risks", 0) > 0,
         f"/api/company/{DOSSIER_TICKER}/dossier did not answer 200 with filings and active risks"),
        (risk_changes.get("status") == 200 and risk_changes.get("pairs", 0) > 0,
         f"/api/company/{DOSSIER_TICKER}/risk-changes did not answer 200 with at least one filing pair"),
        (obs.get("public_evidence_doc_id") == 404, "a doc: id on the public evidence route did not answer 404"),
        (obs.get("workspace_create_no_token") == 403, "creating a workspace without a Turnstile token did not answer 403"),
        (obs.get("agent_with_workspace") == 400, "strategy=agent with a workspace did not answer 400"),
    ]
    return checks


def observe(client: httpx.Client, base_url: str) -> dict:
    """One free request per check; records status codes and the few fields :func:`evaluate_g9` needs."""
    def get(path: str) -> httpx.Response:
        return client.get(f"{base_url}{path}", timeout=REQUEST_TIMEOUT_S)

    obs: dict = {"healthz": get("/healthz").status_code}
    page = get("/")
    obs["index"] = page.status_code
    obs["index_site_key_filled"] = "__TURNSTILE_SITE_KEY__" not in page.text
    r = get("/api/stats")
    body = r.json() if r.status_code == 200 else {}
    obs["stats"] = {"status": r.status_code, "snapshot_id": (body.get("snapshot") or {}).get("id"),
                    "uploads_enabled": body.get("uploads_enabled"),
                    "node_labels": sorted((body.get("graph") or {}).get("nodes") or {}),
                    "freshness_status": (body.get("freshness") or {}).get("status")}
    r = get("/api/freshness")
    body = r.json() if r.status_code == 200 else {}
    obs["freshness"] = {"status": r.status_code, "enabled": body.get("enabled"), "configured": body.get("configured"),
                        "monitor_status": body.get("status"), "pending_count": body.get("pending_count")}
    r = get("/api/examples")
    obs["examples_status"] = r.status_code
    obs["example_ids"] = example_ids(r.json()) if r.status_code == 200 else []
    r = get(f"/api/company/{DOSSIER_TICKER}/dossier")
    body = r.json() if r.status_code == 200 else {}
    obs["dossier"] = {"status": r.status_code, **{k: len(body.get(k) or []) for k in DOSSIER_LISTS}}
    r = get(f"/api/company/{DOSSIER_TICKER}/risk-changes")
    body = r.json() if r.status_code == 200 else {}
    obs["risk_changes"] = {"status": r.status_code, "pairs": len(body.get("pairs") or [])}
    obs["public_evidence_doc_id"] = get(f"/api/evidence/{PUBLIC_DOC_ID}").status_code
    obs["workspace_create_no_token"] = client.post(f"{base_url}/api/workspace", json={},
                                                   timeout=REQUEST_TIMEOUT_S).status_code
    obs["agent_with_workspace"] = client.post(
        f"{base_url}/api/ask", json={"question": AGENT_QUESTION, "strategy": "agent",
                                     "workspace_id": FAKE_WORKSPACE_ID}, timeout=REQUEST_TIMEOUT_S).status_code
    return obs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only live checks for M4 gate G9.")
    ap.add_argument("--base-url", default="https://semigraph.fly.dev")
    ap.add_argument("--expect-examples", required=True, help="GET /api/examples saved before the deploy")
    ap.add_argument("--expect-snapshot", required=True, help="the /api/stats snapshot id before the deploy")
    args = ap.parse_args(argv)

    expected = example_ids(json.loads(Path(args.expect_examples).read_text(encoding="utf-8")))
    with httpx.Client() as client:
        obs = observe(client, args.base_url.rstrip("/"))
    failures = evaluate_g9(obs, expected, args.expect_snapshot)
    total = len(_g9_checks(obs, expected, args.expect_snapshot))
    out = {"gate": "G9", "base_url": args.base_url, "expected_example_count": len(expected),
           "expected_snapshot": args.expect_snapshot, "observations": obs,
           "failures": failures, "passed": not failures, "generated_at": datetime.now(UTC).isoformat()}
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    for f in failures:
        print(f"FAIL: {f}")
    print(f"G9 {'PASS' if not failures else 'FAIL'}: {total - len(failures)} of {total} checks; wrote {ARTIFACT_PATH}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
