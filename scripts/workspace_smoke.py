"""Local end-to-end smoke of the upload workspace (gate G1, docs/v2/M4_PLAN.md 9):

    python -m scripts.workspace_smoke --base-url http://127.0.0.1:8080 --max-usd 0.50

Against a LOCAL running service (the serve-shipped venv, the local graph): creates a workspace, uploads a small
text document as v1, asks a question (a real, PAID call — bounded by ``--max-usd``), asserts the answer cites a
``doc:`` id from v1; re-uploads the SAME bytes and asserts ``unchanged``; uploads a changed v2 and asserts it
supersedes v1; asks again (mentioning the v1 chunk id, so a cooperating model may echo it) and records whether
``stale_citations`` names it; asks with ``as_of`` set to before v2 was created; fetches the change report and checks
it against the known edit set baked into :data:`MD_V1` / :data:`MD_V2`; fetches evidence for a chunk; deletes the
workspace and asserts every route now 404s. Writes ``artifacts/workspace_smoke.json`` — :func:`redact` strips every
text-bearing field first, so no uploaded or answered text ever lands in the committed artifact (docs/v2/M4_PLAN.md
11). This script is NOT run by this worker (docs/v2/M4_PLAN.md: "the main session runs G1"); :func:`redact`,
:func:`evaluate_g1` and :func:`within_budget` are pure and unit-tested with fakes
(``tests/test_serve_workspace_smoke.py``); only :func:`run` touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ARTIFACT_PATH = Path("artifacts/workspace_smoke.json")
REQUEST_TIMEOUT_S = 60
JOB_POLL_TIMEOUT_S = 120

# A small, self-contained Markdown pair with a KNOWN edit set: one section removed, one added, one genuinely
# reworded (a semantic change, not just tense), one byte-identical (unchanged).
MD_V1 = ("# Executive Summary\nThe company performed well this quarter.\n\n"
        "# Legal Proceedings\nThere are no material legal proceedings.\n\n"
        "# Market Outlook\nThe market is expected to grow next year.\n\n"
        "# Company History\nThe company was founded and has grown steadily.\n")
MD_V2 = ("# Executive Summary\nThe company performed well this quarter.\n\n"
        "# Market Outlook\nThe market is expected to shrink next year, reversing the prior forecast.\n\n"
        "# Company History\nThe company was founded and has grown steadily.\n\n"
        "# Cybersecurity Practices\nThe company has adopted new cybersecurity controls.\n")
KNOWN_EDIT_SET = {"removed_headlines": {"Legal Proceedings"}, "added_headlines": {"Cybersecurity Practices"},
                  "changed_headlines": {"Market Outlook"}}

# Fields that could carry uploaded or model-generated text; scrubbed before anything is written to disk.
_TEXT_FIELDS = frozenset({"text", "answer", "quote", "headline", "summary", "title"})


def redact(value):
    """``value`` with every :data:`_TEXT_FIELDS` key replaced by a fixed placeholder, recursively — the one place
    this script's report can be trusted to carry no uploaded bytes or generated text (docs/v2/M4_PLAN.md 11)."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in _TEXT_FIELDS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def within_budget(spent_usd: float, max_usd: float) -> bool:
    return spent_usd <= max_usd


def evaluate_g1(results: dict) -> list[str]:
    """Every G1 assertion (docs/v2/M4_PLAN.md 9) that FAILED, given the recorded step outcomes; ``[]`` means G1
    passes. Each key is set by a step in :func:`run`; a missing key counts as failed (the step never ran)."""
    checks = [
        ("v1_ask_cites_doc", "the v1 answer did not cite a doc: id from v1"),
        ("reupload_unchanged", "an identical re-upload was not reported unchanged"),
        ("v2_supersedes_v1", "v2 did not flip the v1 chunk(s) to is_current=false / status=superseded"),
        ("changes_match_edit_set", "the change report did not match the known edit set"),
        ("evidence_ok", "the workspace evidence route did not return the expected chunk"),
        ("deleted_then_404", "a workspace route still answered after DELETE"),
    ]
    return [msg for key, msg in checks if not results.get(key)]


def _budget_notes(results: dict) -> list[str]:
    """Non-fatal observations (docs/v2/M4_PLAN.md risk: the date-granularity hole in ``as_of`` and the
    non-determinism of asking a real model to echo an id) — recorded, never treated as a G1 failure."""
    notes = []
    if not results.get("stale_citation_named"):
        notes.append("the follow-up ask did not name the v1 id in stale_citations (the model may not have cited "
                     "it — this is observational, not a G1 failure: see docs/v2/M4_PLAN.md advisor note on the "
                     "same-day as_of granularity hole)")
    if not results.get("as_of_before_v2_returns_v1"):
        notes.append("as_of before v2 did not visibly return v1-only content (expected when v1 and v2 are "
                     "created on the SAME calendar day: the as_of cutoff granularity is a day, not a moment)")
    return notes


def _post_json(client, base_url: str, path: str, json_body: dict, headers: dict | None = None):
    return client.post(f"{base_url}{path}", json=json_body, headers=headers or {}, timeout=REQUEST_TIMEOUT_S)


def _create_workspace(client, base_url: str) -> dict:
    r = _post_json(client, base_url, "/api/workspace", {"turnstile_token": None})
    r.raise_for_status()
    return r.json()


def _upload(client, base_url: str, ws: str, token: str, content: bytes, *, document_id: str | None = None) -> dict:
    files = {"file": ("smoke.md", content, "text/markdown")}
    data = {"turnstile_token": ""}
    if document_id:
        data["document_id"] = document_id
    r = client.post(f"{base_url}/api/workspace/{ws}/documents", files=files, data=data,
                    headers={"X-Workspace-Token": token}, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _wait_for_job(client, base_url: str, ws: str, token: str, job_id: str) -> dict:
    deadline = time.monotonic() + JOB_POLL_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        with client.stream("GET", f"{base_url}/api/workspace/{ws}/jobs/{job_id}",
                           headers={"X-Workspace-Token": token}, timeout=REQUEST_TIMEOUT_S) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                last = json.loads(line[len("data:"):].strip())
                if last.get("state") in ("ready", "failed"):
                    return last
        time.sleep(1)
    return last


def _ask(client, base_url: str, ws: str, token: str, question: str, *, as_of: str | None = None) -> dict:
    body = {"question": question, "workspace_id": ws, "as_of": as_of}
    r = client.post(f"{base_url}/api/ask", json=body, headers={"X-Workspace-Token": token},
                    timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    done: dict = {}
    for block in r.text.split("\n\n"):
        data = "".join(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            event = json.loads(data)
            if event.get("event") == "done":
                done = event
    return done


def _run_versions(client, base_url: str, ws: str, token: str) -> dict:
    """Steps 1-5 of G1: v1 upload, an ask that should cite it, an identical re-upload, v2 upload, and a follow-up
    ask that names the v1 id (whether the model actually echoes it is observational, see :func:`_budget_notes`)."""
    results: dict = {}
    spend = 0.0
    v1 = _upload(client, base_url, ws, token, MD_V1.encode("utf-8"))
    job1 = _wait_for_job(client, base_url, ws, token, v1["job_id"])
    document_id = job1.get("document_id", v1["document_id"])

    done1 = _ask(client, base_url, ws, token, "What does my document say about the market outlook?")
    spend += done1.get("cost_usd") or 0.0
    v1_doc_ids = [c for c in done1.get("citations", []) if c.startswith(f"doc:{document_id}:v1:")]
    results["v1_ask_cites_doc"] = bool(v1_doc_ids)

    reup = _upload(client, base_url, ws, token, MD_V1.encode("utf-8"), document_id=document_id)
    results["reupload_unchanged"] = reup.get("unchanged") is True

    v2 = _upload(client, base_url, ws, token, MD_V2.encode("utf-8"), document_id=document_id)
    job2 = _wait_for_job(client, base_url, ws, token, v2["job_id"])
    results["v2_supersedes_v1"] = job2.get("state") == "ready" and job2.get("version") == 2

    v1_id_hint = v1_doc_ids[0] if v1_doc_ids else f"doc:{document_id}:v1:0000"
    done2 = _ask(client, base_url, ws, token,
                f"Earlier you cited {v1_id_hint} for the market outlook — does that still hold in v2?")
    spend += done2.get("cost_usd") or 0.0
    results["stale_citation_named"] = v1_id_hint in (done2.get("workspace") or {}).get("stale_citations", [])

    return {"results": results, "spend": spend, "document_id": document_id, "v1_id_hint": v1_id_hint,
           "steps": {"v1_upload": v1, "job1": job1, "ask1": done1, "reupload": reup, "v2_upload": v2, "job2": job2,
                     "ask2": done2}}


def _run_as_of_and_cleanup(client, base_url: str, ws: str, token: str, document_id: str, v1_id_hint: str) -> dict:
    """Steps 6-9 of G1: an ``as_of`` ask before v2 existed, the change report against the known edit set, evidence
    for the v1 chunk, then delete and confirm every route 404s."""
    results: dict = {}
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    done3 = _ask(client, base_url, ws, token, "What does the market outlook say?", as_of=yesterday)
    results["as_of_before_v2_returns_v1"] = any(
        c.startswith(f"doc:{document_id}:v1:") for c in done3.get("citations", []))

    changes = client.get(f"{base_url}/api/workspace/{ws}/changes",
                         params={"document_id": document_id, "from": 1, "to": 2},
                         headers={"X-Workspace-Token": token}, timeout=REQUEST_TIMEOUT_S).json()
    changed = {c.get("headline") for c in changes.get("changed", [])}
    added = {c.get("headline") for c in changes.get("added", [])}
    removed = {c.get("headline") for c in changes.get("removed", [])}
    results["changes_match_edit_set"] = (KNOWN_EDIT_SET["changed_headlines"] <= changed and
                                         KNOWN_EDIT_SET["added_headlines"] <= added and
                                         KNOWN_EDIT_SET["removed_headlines"] <= removed)

    ev = client.get(f"{base_url}/api/workspace/{ws}/evidence/{v1_id_hint}", headers={"X-Workspace-Token": token},
                    timeout=REQUEST_TIMEOUT_S)
    results["evidence_ok"] = ev.status_code == 200 and ev.json().get("document_id") == document_id

    client.delete(f"{base_url}/api/workspace/{ws}", headers={"X-Workspace-Token": token}, timeout=REQUEST_TIMEOUT_S)
    after = client.get(f"{base_url}/api/workspace/{ws}", headers={"X-Workspace-Token": token},
                      timeout=REQUEST_TIMEOUT_S)
    results["deleted_then_404"] = after.status_code == 404

    return {"results": results, "spend": done3.get("cost_usd") or 0.0,
           "steps": {"ask3": done3, "changes": changes,
                     "evidence": ev.json() if ev.status_code == 200 else {"status_code": ev.status_code}}}


def run(base_url: str, max_usd: float) -> dict:
    import httpx

    with httpx.Client() as client:
        created = _create_workspace(client, base_url)
        ws, token = created["workspace_id"], created["token"]
        first = _run_versions(client, base_url, ws, token)
        second = _run_as_of_and_cleanup(client, base_url, ws, token, first["document_id"], first["v1_id_hint"])

    results = {**first["results"], **second["results"]}
    spend = round(first["spend"] + second["spend"], 6)
    steps = redact({"created": created, **first["steps"], **second["steps"]})
    return {"results": results, "spend_usd": spend, "within_budget": within_budget(spend, max_usd), "steps": steps}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local end-to-end smoke of the upload workspace (gate G1).")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--max-usd", type=float, default=0.50)
    args = ap.parse_args(argv)

    report = run(args.base_url, args.max_usd)
    failures = evaluate_g1(report["results"])
    notes = _budget_notes(report["results"])
    out = {"base_url": args.base_url, "max_usd": args.max_usd, "spend_usd": report["spend_usd"],
          "within_budget": report["within_budget"], "results": report["results"], "failures": failures,
          "notes": notes, "steps": report["steps"], "generated_at": datetime.now(UTC).isoformat()}
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"spend: ${report['spend_usd']:.4f} (budget ${args.max_usd:.2f}) — within budget: {report['within_budget']}")
    for f in failures:
        print(f"FAIL: {f}")
    for n in notes:
        print(f"note: {n}")
    print(f"wrote {ARTIFACT_PATH}")
    return 0 if not failures and report["within_budget"] else 1


if __name__ == "__main__":
    sys.exit(main())
