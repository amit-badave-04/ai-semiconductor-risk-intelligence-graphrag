"""Local end-to-end smoke of the upload workspace (gate G1, docs/v2/M4_PLAN.md 9):

    python -m scripts.workspace_smoke --base-url http://127.0.0.1:8080 --max-usd 0.50

Against a LOCAL running service (the serve-shipped venv, the local graph): creates a workspace, uploads a small
text document as v1, asks a question (a real, PAID call — bounded by ``--max-usd``), asserts the answer cites a
``doc:`` id from v1; re-uploads the SAME bytes and asserts ``unchanged``; uploads a changed v2 and asserts BOTH that
the job reports ``ready``/v2 AND that v1's own row in ``GET /api/workspace`` flipped to
``is_current=false``/``status=superseded`` (finding 19); asks again with ``as_of`` set to v1's OWN ``created_at``
instant (section 15.1) — which deterministically retrieves v1's now-superseded chunk, so the answer's
``stale_citations`` reliably names it (no longer a hope that a cooperating model happens to echo an id from the
question text, and now a REQUIRED G1 check, not an observation); separately asks with ``as_of`` set to the day
before the workspace existed (the day before: it must retrieve NO uploaded passage); fetches the change report and checks it against
the known edit set baked into :data:`MD_V1` / :data:`MD_V2`; fetches evidence for the v1 chunk and asserts it shows
``is_current=false``/``status=superseded``/``superseded_by_version=2``; deletes the workspace and asserts EVERY
workspace route (GET/DELETE workspace, POST documents, GET jobs, GET changes, GET evidence, POST /api/ask) now
404s. Writes ``artifacts/workspace_smoke.json`` — :func:`redact` strips every text-bearing field first, so no
uploaded or answered text ever lands in the committed artifact (docs/v2/M4_PLAN.md 11). This script is NOT run by
this worker (docs/v2/M4_PLAN.md: "the main session runs G1"); :func:`redact`, :func:`evaluate_g1` and
:func:`within_budget` are pure and unit-tested with fakes (``tests/test_serve_workspace_smoke.py``); only
:func:`_run_g1` (and the real ``run``/``main`` that wrap it) touch the network — a real server is never contacted
by this worker's test suite, which instead drives :func:`_run_g1`, :func:`_run_as_of_and_cleanup` and :func:`_ask`
with an injected fake client (the same pattern :func:`_all_workspace_routes_404` was already tested with).

Round-2 review finding C6: a SECOND G1 run within the 10-minute ask rate-limit window (plan G8 allows exactly this)
previously let any non-2xx response — most commonly a 429 on the third, merely observational, ``as_of=yesterday``
ask — raise straight out of ``run()``. That skipped the DELETE at the end of the happy path entirely (the
workspace then lingered until its 24 h TTL) and never wrote ``artifacts/workspace_smoke.json``, so a transient or
rate-limited run left no G1 verdict at all. Two independent fixes close this: (1) :func:`_ask` never raises on a
non-2xx response any more — it returns an ``{"error": ..., "status_code": ...}`` marker, so a single rate-limited
or transient-error ask degrades only the ONE G1 check that ask feeds, never the checks after it; and (2)
:func:`_run_g1` wraps every step in ``try/except/finally``: on ANY exception (an upload, a workspace fetch, a
malformed response) it still deletes the workspace it created and returns a report — carrying an ``"error"`` field
and whatever partial results were gathered — instead of letting the exception propagate out of ``run()``/``main()``.
"""

from __future__ import annotations

import argparse
import hashlib
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
_SECRET_FIELDS = frozenset({"token"})          # the workspace token: never in a committed artifact, even when dead


def _ws_hash(workspace_id: str) -> str:
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


def redact(value):
    """``value`` with every :data:`_TEXT_FIELDS` key replaced by a fixed placeholder, every :data:`_SECRET_FIELDS` key
    by ``<secret>`` and every ``workspace_id`` by its hash, recursively — the one place this script's report can be
    trusted to carry no uploaded bytes, generated text, token or raw workspace id (docs/v2/M4_PLAN.md 5, 11)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _TEXT_FIELDS:
                out[k] = "<redacted>"
            elif k in _SECRET_FIELDS:
                out[k] = "<secret>"
            elif k == "workspace_id" and isinstance(v, str):
                out[k] = f"<ws:{_ws_hash(v)}>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def within_budget(spent_usd: float, max_usd: float) -> bool:
    return spent_usd <= max_usd


def evaluate_g1(results: dict) -> list[str]:
    """Every G1 assertion (docs/v2/M4_PLAN.md 9, 15.13) that FAILED, given the recorded step outcomes; ``[]`` means
    G1 passes. Each key is set by a step in :func:`run`; a missing key counts as failed (the step never ran).

    ``stale_citation_named`` is a REQUIRED check (finding 19): since section 15.1, ``as_of`` accepts a version's own
    ``created_at`` instant, so the follow-up ask in :func:`_run_versions` deterministically retrieves v1's
    now-superseded chunk instead of hoping a model happens to echo an id from the question text — it is no longer
    merely observational."""
    checks = [
        ("v1_ask_cites_doc", "the v1 answer did not cite a doc: id from v1"),
        ("reupload_unchanged", "an identical re-upload was not reported unchanged"),
        ("v2_supersedes_v1", "v2 did not flip the v1 chunk(s) to is_current=false / status=superseded"),
        ("changes_match_edit_set", "the change report did not match the known edit set"),
        ("evidence_ok", "the workspace evidence route did not return the expected, now-superseded chunk"),
        ("stale_citation_named", "the as-of-v1 ask did not produce a stale citation naming the v1 chunk"),
        ("as_of_date_before_creation_empty", "an as_of date before the workspace existed still retrieved uploaded text"),
        ("deleted_then_404", "a workspace route still answered after DELETE"),
    ]
    return [msg for key, msg in checks if not results.get(key)]


def _budget_notes(results: dict) -> list[str]:
    """Non-fatal observations for the report (none are defined today; every G1 check is required)."""
    return []


def _post_json(client, base_url: str, path: str, json_body: dict, headers: dict | None = None):
    return client.post(f"{base_url}{path}", json=json_body, headers=headers or {}, timeout=REQUEST_TIMEOUT_S)


def _create_workspace(client, base_url: str) -> dict:
    r = _post_json(client, base_url, "/api/workspace", {"turnstile_token": None})
    r.raise_for_status()
    return r.json()


def _upload(client, base_url: str, ws: str, token: str, content: bytes, *, document_id: str | None = None) -> dict:
    files = {"file": ("smoke.md", content, "text/markdown")}
    data = {}
    if document_id:
        data["document_id"] = document_id
    # The bot-check token travels in a HEADER, verified before the body is read (docs/v2/M4_PLAN.md 15.2).
    r = client.post(f"{base_url}/api/workspace/{ws}/documents", files=files, data=data,
                    headers={"X-Workspace-Token": token, "X-Turnstile-Token": ""}, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _get_workspace(client, base_url: str, ws: str, token: str) -> dict:
    r = client.get(f"{base_url}/api/workspace/{ws}", headers={"X-Workspace-Token": token},
                   timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _version_row(workspace: dict, document_id: str, version: int) -> dict | None:
    for doc in workspace.get("documents") or []:
        if doc.get("document_id") == document_id:
            for v in doc.get("versions") or []:
                if v.get("version") == version:
                    return v
    return None


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
    """C6: never raises on a non-2xx response (a rate limit, a transient 5xx) — it returns an ``{"error":
    True, "status_code": ...}`` marker instead, so a failed ask never aborts the change-report/evidence/delete checks
    that follow it. :func:`inconclusive_asks` then marks the whole run INCONCLUSIVE (not failed): an ask the service
    refused (for example a 429 from the per-address window on a second run within 10 minutes) says nothing about
    whether the checks it feeds would pass."""
    body = {"question": question, "workspace_id": ws, "as_of": as_of}
    r = client.post(f"{base_url}/api/ask", json=body, headers={"X-Workspace-Token": token},
                    timeout=REQUEST_TIMEOUT_S)
    if r.status_code >= 400:
        return {"error": True, "status_code": r.status_code}
    done: dict = {}
    for block in r.text.split("\n\n"):
        data = "".join(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            event = json.loads(data)
            if event.get("event") == "done":
                done = event
    return done


def _run_versions(client, base_url: str, ws: str, token: str) -> dict:
    """Steps 1-5 of G1: v1 upload, an ask that should cite it, an identical re-upload, v2 upload (asserting the
    currency flip on v1, not just the job's own reported state), and a follow-up ask AS OF v1's own instant, which
    deterministically retrieves v1's now-superseded chunk and so reliably produces a stale citation (finding 19 /
    docs/v2/M4_PLAN.md 15.1, 15.13) — no longer a hope that the model happens to echo an id from the question text."""
    results: dict = {}
    spend = 0.0
    v1 = _upload(client, base_url, ws, token, MD_V1.encode("utf-8"))
    job1 = _wait_for_job(client, base_url, ws, token, v1["job_id"])
    document_id = job1.get("document_id", v1["document_id"])

    done1 = _ask(client, base_url, ws, token, "What does my document say about the market outlook?")
    spend += done1.get("cost_usd") or 0.0
    v1_doc_ids = [c for c in done1.get("citations", []) if c.startswith(f"doc:{document_id}:v1:")]
    results["v1_ask_cites_doc"] = bool(v1_doc_ids)

    workspace_after_v1 = _get_workspace(client, base_url, ws, token)
    v1_row_before_v2 = _version_row(workspace_after_v1, document_id, 1)
    v1_created_at = (v1_row_before_v2 or {}).get("created_at")

    reup = _upload(client, base_url, ws, token, MD_V1.encode("utf-8"), document_id=document_id)
    results["reupload_unchanged"] = reup.get("unchanged") is True

    v2 = _upload(client, base_url, ws, token, MD_V2.encode("utf-8"), document_id=document_id)
    job2 = _wait_for_job(client, base_url, ws, token, v2["job_id"])
    job2_ready = job2.get("state") == "ready" and job2.get("version") == 2

    workspace_after_v2 = _get_workspace(client, base_url, ws, token)
    v1_row_after_v2 = _version_row(workspace_after_v2, document_id, 1)
    v1_flipped = (v1_row_after_v2 is not None and v1_row_after_v2.get("is_current") is False and
                 v1_row_after_v2.get("status") == "superseded")
    results["v2_supersedes_v1"] = job2_ready and v1_flipped

    v1_id_hint = v1_doc_ids[0] if v1_doc_ids else f"doc:{document_id}:v1:0000"
    done2 = _ask(client, base_url, ws, token, "What does the market outlook say?", as_of=v1_created_at)
    spend += done2.get("cost_usd") or 0.0
    cited_v1 = [c for c in done2.get("citations", []) if c.startswith(f"doc:{document_id}:v1:")]
    stale = (done2.get("workspace") or {}).get("stale_citations", [])
    results["stale_citation_named"] = bool(cited_v1) and any(c in stale for c in cited_v1)
    if cited_v1:
        v1_id_hint = cited_v1[0]

    return {"results": results, "spend": spend, "document_id": document_id, "v1_id_hint": v1_id_hint,
           "steps": {"v1_upload": v1, "job1": job1, "ask1": done1, "reupload": reup, "v2_upload": v2, "job2": job2,
                     "ask2": done2}}


AFTER_DELETE_QUESTION = "What does my document say about the market outlook?"   # passes question validation


def _all_workspace_routes_404(client, base_url: str, ws: str, token: str, document_id: str,
                              chunk_id_hint: str, job_id: str) -> tuple[bool, dict[str, int]]:
    """Every workspace route, after DELETE, must answer 404 — not just the one this smoke script happened to probe
    before (finding 19 / docs/v2/M4_PLAN.md 15.13). Each probe is a request that would SUCCEED on a live workspace (a
    real job id, a valid question), so a 404 proves the deletion, not a validation error. Returns ``(all 404,
    {route: status})`` so a failure names its route."""
    headers = {"X-Workspace-Token": token}
    base = f"{base_url}/api/workspace/{ws}"
    probes = [
        ("GET /api/workspace/{ws}", lambda: client.get(base, headers=headers, timeout=REQUEST_TIMEOUT_S)),
        ("POST documents", lambda: client.post(f"{base}/documents", files={"file": ("x.md", b"# a\nbody\n",
                                                                                   "text/markdown")},
                                               headers={**headers, "X-Turnstile-Token": ""}, timeout=REQUEST_TIMEOUT_S)),
        ("GET jobs", lambda: client.get(f"{base}/jobs/{job_id}", headers=headers, timeout=REQUEST_TIMEOUT_S)),
        ("GET changes", lambda: client.get(f"{base}/changes", params={"document_id": document_id, "from": 1, "to": 2},
                                           headers=headers, timeout=REQUEST_TIMEOUT_S)),
        ("GET evidence", lambda: client.get(f"{base}/evidence/{chunk_id_hint}", headers=headers,
                                            timeout=REQUEST_TIMEOUT_S)),
        ("POST /api/ask", lambda: client.post(f"{base_url}/api/ask", json={"question": AFTER_DELETE_QUESTION,
                                                                           "workspace_id": ws},
                                              headers=headers, timeout=REQUEST_TIMEOUT_S)),
        ("DELETE /api/workspace/{ws}", lambda: client.delete(base, headers=headers, timeout=REQUEST_TIMEOUT_S)),
    ]
    statuses = {name: probe().status_code for name, probe in probes}
    return all(s == 404 for s in statuses.values()), statuses


def _run_as_of_and_cleanup(client, base_url: str, ws: str, token: str, document_id: str, v1_id_hint: str,
                           job_id: str) -> dict:
    """Steps 6-9 of G1: an ``as_of`` DATE before the workspace existed (the day before: it must retrieve no uploaded
    passage, the date form's end-of-day cutoff), the change report against the known edit set, SUPERSEDED evidence for the v1 chunk (not
    just its existence), then delete and confirm EVERY workspace route 404s (finding 19)."""
    results: dict = {}
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    done3 = _ask(client, base_url, ws, token, "What does the market outlook say?", as_of=yesterday)
    results["as_of_date_before_creation_empty"] = (
        (done3.get("workspace") or {}).get("doc_chunks") == 0
        and not any(c.startswith("doc:") for c in done3.get("citations", [])))

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
    ev_json = ev.json() if ev.status_code == 200 else {}
    results["evidence_ok"] = (ev.status_code == 200 and ev_json.get("document_id") == document_id and
                              ev_json.get("is_current") is False and ev_json.get("status") == "superseded" and
                              ev_json.get("superseded_by_version") == 2)

    client.delete(f"{base_url}/api/workspace/{ws}", headers={"X-Workspace-Token": token}, timeout=REQUEST_TIMEOUT_S)
    results["deleted_then_404"], after_delete = _all_workspace_routes_404(client, base_url, ws, token, document_id,
                                                                         v1_id_hint, job_id)

    return {"results": results, "spend": done3.get("cost_usd") or 0.0,
           "steps": {"ask3": done3, "changes": changes, "after_delete_statuses": after_delete,
                     "evidence": ev.json() if ev.status_code == 200 else {"status_code": ev.status_code}}}


def _redact_exception_text(exc: Exception, ws: str | None, token: str | None) -> str:
    """``str(exc)`` with the raw workspace id and token scrubbed (docs/v2/M4_PLAN.md 5, 11): an ``httpx`` error
    from a failed step routinely embeds the REQUEST URL it was raised for (``"...for url
    'http://host/api/workspace/<ws>/documents'..."``), which would otherwise put a raw workspace id straight into
    the committed artifact — the one thing every other value here goes through :func:`redact` to avoid."""
    text = f"{type(exc).__name__}: {exc}"
    if ws:
        text = text.replace(ws, f"<ws:{_ws_hash(ws)}>")
    if token:
        text = text.replace(token, "<secret>")
    return text


def _run_g1(client, base_url: str, max_usd: float) -> dict:
    """The whole G1 run against an already-open ``client`` (real ``httpx.Client`` from :func:`run`, or a fake in
    tests). C6: EVERY step from workspace creation through the final delete-and-404 probes runs inside one
    try/except/finally, so a step that raises (a transient 5xx, a malformed response, anything :func:`_ask`'s own
    non-raising fix does not already cover) still reaches the ``finally`` — which deletes the workspace it created,
    best-effort, exactly once — and this function still RETURNS a report (never raises), with an ``"error"`` key
    and whatever partial ``results``/``steps`` were gathered before the failure. ``evaluate_g1`` already treats a
    missing result key as failed, so a partial report still yields a correct (all-failing) G1 verdict instead of no
    verdict — and no artifact — at all."""
    created: dict | None = None
    ws = token = None
    first: dict = {"results": {}, "spend": 0.0, "steps": {}}
    second: dict = {"results": {}, "spend": 0.0, "steps": {}}
    error: str | None = None
    try:
        created = _create_workspace(client, base_url)
        ws, token = created["workspace_id"], created["token"]
        first = _run_versions(client, base_url, ws, token)
        second = _run_as_of_and_cleanup(client, base_url, ws, token, first["document_id"], first["v1_id_hint"],
                                        first["steps"]["job1"].get("job_id") or first["steps"]["v1_upload"]["job_id"])
    except Exception as exc:   # noqa: BLE001 — C6: ANY step failing must still delete the workspace and report
        error = _redact_exception_text(exc, ws, token)
    finally:
        if ws is not None:
            try:
                client.delete(f"{base_url}/api/workspace/{ws}", headers={"X-Workspace-Token": token},
                             timeout=REQUEST_TIMEOUT_S)
            except Exception:
                pass   # best-effort: an unreachable service here still leaves the workspace to its own 24h TTL

    results = {**first.get("results", {}), **second.get("results", {})}
    spend = round(first.get("spend", 0.0) + second.get("spend", 0.0), 6)
    steps = redact({"created": created, **first.get("steps", {}), **second.get("steps", {})})
    out = {"results": results, "spend_usd": spend, "within_budget": within_budget(spend, max_usd), "steps": steps}
    if error is not None:
        out["error"] = error
    return out


def run(base_url: str, max_usd: float) -> dict:
    import httpx

    with httpx.Client() as client:
        return _run_g1(client, base_url, max_usd)


def inconclusive_asks(steps: dict) -> list[str]:
    """``"<step> answered <status>"`` for every ask the service refused (an error marker from :func:`_ask`)."""
    return [f"{name} answered {step.get('status_code')}" for name, step in sorted((steps or {}).items())
            if name.startswith("ask") and isinstance(step, dict) and step.get("error")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local end-to-end smoke of the upload workspace (gate G1).")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--max-usd", type=float, default=0.50)
    args = ap.parse_args(argv)

    report = run(args.base_url, args.max_usd)
    failures = evaluate_g1(report["results"])
    notes = _budget_notes(report["results"])
    # C6: `report["error"]` is only present when a step raised partway (_run_g1's try/finally still ran the
    # workspace's DELETE and returned whatever partial results it had) — the artifact and the exit code both
    # reflect that instead of this process crashing with no artifact written at all.
    refused = inconclusive_asks(report["steps"])
    out = {"base_url": args.base_url, "max_usd": args.max_usd, "spend_usd": report["spend_usd"],
          "within_budget": report["within_budget"], "results": report["results"], "failures": failures,
          "notes": notes, "error": report.get("error"), "inconclusive": refused, "steps": report["steps"],
          "generated_at": datetime.now(UTC).isoformat()}
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"spend: ${report['spend_usd']:.4f} (budget ${args.max_usd:.2f}) — within budget: {report['within_budget']}")
    if report.get("error"):
        print(f"ERROR: a step raised partway through the run: {report['error']}")
    # every failed check is printed even when the run is inconclusive: a refused ask explains only the checks that
    # depended on it, never the others
    for f in failures:
        print(f"FAIL: {f}")
    for n in notes:
        print(f"note: {n}")
    print(f"wrote {ARTIFACT_PATH}")
    if refused:
        print(f"INCONCLUSIVE: {', '.join(refused)} — rerun on a fresh server process or after 10 minutes")
        return 2
    return 0 if not failures and report["within_budget"] and not report.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
