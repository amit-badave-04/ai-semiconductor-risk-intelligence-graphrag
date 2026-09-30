"""Pure parts of ``scripts/workspace_smoke.py`` (M4 gate G1, docs/v2/M4_PLAN.md 9): redaction, budget arithmetic and
the G1 pass/fail evaluator. The real ``run`` (the part that touches a server) is NEVER called here; ``main`` is called
only with ``run`` replaced by a fake report. The main session runs the smoke script itself, against a real service."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_smoke():
    spec = importlib.util.spec_from_file_location("workspace_smoke", ROOT / "scripts" / "workspace_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


# ---------------------------------------------------------------- redact


def test_redact_replaces_every_text_bearing_field_recursively():
    payload = {"text": "the actual uploaded content", "document_id": "abc123",
              "nested": {"quote": "a cited sentence", "chunk_id": "doc:abc123:v1:0001"},
              "list": [{"answer": "a generated answer"}, {"headline": "Risk Factors"}]}
    out = smoke.redact(payload)
    assert out["text"] == "<redacted>" and out["document_id"] == "abc123"
    assert out["nested"]["quote"] == "<redacted>" and out["nested"]["chunk_id"] == "doc:abc123:v1:0001"
    assert out["list"][0]["answer"] == "<redacted>" and out["list"][1]["headline"] == "<redacted>"


def test_redact_leaves_non_text_scalars_and_shapes_alone():
    assert smoke.redact(42) == 42
    assert smoke.redact(None) is None
    assert smoke.redact([1, 2, 3]) == [1, 2, 3]
    assert smoke.redact({}) == {}


def test_no_known_text_field_survives_a_realistic_step_payload():
    step = {"job_id": "j1", "state": "ready", "document_id": "abc123", "version": 1,
           "text": "SECRET UPLOADED CONTENT", "answer": "Margin was 41.5% [doc:abc123:v1:0001].",
           "citations": ["doc:abc123:v1:0001"], "checks": {"has_citation": True}}
    out = smoke.redact(step)
    dumped = str(out)
    assert "SECRET UPLOADED CONTENT" not in dumped
    assert "Margin was 41.5%" not in dumped
    assert out["citations"] == ["doc:abc123:v1:0001"]     # ids are not text; they stay for debugging


# ---------------------------------------------------------------- within_budget


def test_within_budget_is_inclusive_of_the_exact_cap():
    assert smoke.within_budget(0.5, 0.5) is True
    assert smoke.within_budget(0.499999, 0.5) is True
    assert smoke.within_budget(0.500001, 0.5) is False
    assert smoke.within_budget(0.0, 0.0) is True


# ---------------------------------------------------------------- evaluate_g1


ALL_PASS = {"v1_ask_cites_doc": True, "reupload_unchanged": True, "v2_supersedes_v1": True,
           "changes_match_edit_set": True, "evidence_ok": True, "stale_citation_named": True,
           "deleted_then_404": True, "as_of_date_before_creation_empty": True}


def test_evaluate_g1_passes_when_every_check_is_true():
    assert smoke.evaluate_g1(ALL_PASS) == []


def test_evaluate_g1_reports_every_missing_or_false_check():
    assert smoke.evaluate_g1({}) == [
        "the v1 answer did not cite a doc: id from v1",
        "an identical re-upload was not reported unchanged",
        "v2 did not flip the v1 chunk(s) to is_current=false / status=superseded",
        "the change report did not match the known edit set",
        "the workspace evidence route did not return the expected, now-superseded chunk",
        "the as-of-v1 ask did not produce a stale citation naming the v1 chunk",
        "an as_of date before the workspace existed still retrieved uploaded text",
        "a workspace route still answered after DELETE",
    ]


def test_evaluate_g1_reports_exactly_the_failed_checks():
    partial = {**ALL_PASS, "evidence_ok": False, "deleted_then_404": False}
    failures = smoke.evaluate_g1(partial)
    assert failures == ["the workspace evidence route did not return the expected, now-superseded chunk",
                        "a workspace route still answered after DELETE"]


def test_evaluate_g1_fails_when_the_stale_citation_check_fails():
    """Finding 19: stale_citation_named is now REQUIRED (section 15.1's as_of=v1-instant makes it deterministic),
    not merely an observational note — a regression in the currency flip or the stale-citation logic must fail G1."""
    partial = {**ALL_PASS, "stale_citation_named": False}
    assert smoke.evaluate_g1(partial) == ["the as-of-v1 ask did not produce a stale citation naming the v1 chunk"]


# ---------------------------------------------------------------- _budget_notes (observational, never a G1 failure)


def test_an_as_of_date_before_the_workspace_existed_must_retrieve_nothing_uploaded():
    """The first live G1 run: asking as of the day BEFORE the workspace was created correctly retrieved no uploaded
    passage; that is a required check of the date form's cutoff, not an observation."""
    results = {**ALL_PASS, "as_of_date_before_creation_empty": False}
    assert smoke.evaluate_g1(results) == ["an as_of date before the workspace existed still retrieved uploaded text"]
    assert smoke._budget_notes(results) == []


# ---------------------------------------------------------------- _version_row (finding 19)


def test_version_row_finds_the_matching_document_and_version():
    workspace = {"documents": [{"document_id": "abc123456789",
                                "versions": [{"version": 1, "is_current": False, "status": "superseded"},
                                            {"version": 2, "is_current": True, "status": "current"}]}]}
    row = smoke._version_row(workspace, "abc123456789", 1)
    assert row == {"version": 1, "is_current": False, "status": "superseded"}


def test_version_row_returns_none_for_an_unknown_document_or_version():
    workspace = {"documents": [{"document_id": "abc123456789", "versions": [{"version": 1}]}]}
    assert smoke._version_row(workspace, "abc123456789", 9) is None
    assert smoke._version_row(workspace, "does-not-exist", 1) is None
    assert smoke._version_row({}, "abc123456789", 1) is None


# ---------------------------------------------------------------- the fixture's own known edit set is self-consistent


def test_the_md_fixture_pair_actually_differs_the_way_the_known_edit_set_claims():
    for headline in smoke.KNOWN_EDIT_SET["removed_headlines"]:
        assert f"# {headline}" in smoke.MD_V1 and f"# {headline}" not in smoke.MD_V2
    for headline in smoke.KNOWN_EDIT_SET["added_headlines"]:
        assert f"# {headline}" not in smoke.MD_V1 and f"# {headline}" in smoke.MD_V2
    for headline in smoke.KNOWN_EDIT_SET["changed_headlines"]:
        assert f"# {headline}" in smoke.MD_V1 and f"# {headline}" in smoke.MD_V2


# ---------------------------------------------------------------- the first live G1 run (2026-09-29) exposed three smoke defects

def test_a_refused_ask_makes_the_run_inconclusive_and_names_the_step():
    """Closing verification: a 429 from the per-address window on a second run scored a required check as FAIL with a
    misleading message; a refused ask now marks the run inconclusive (exit code 2), naming the step and its status."""
    steps = {"ask1": {"citations": ["doc:x"]}, "ask2": {"error": True, "status_code": 429}, "job1": {"state": "ready"},
             "ask3": {"error": True, "status_code": 503}}
    assert smoke.inconclusive_asks(steps) == ["ask2 answered 429", "ask3 answered 503"]
    assert smoke.inconclusive_asks({"ask1": {"citations": []}}) == []


def test_an_inconclusive_run_still_prints_its_failed_checks_before_exiting_2(monkeypatch, tmp_path, capsys):
    """Closing verification LOW: an INCONCLUSIVE run returned before printing the FAIL lines, hiding the other checks
    that failed for reasons unrelated to the refused ask. ``run`` is replaced here, so no server is touched."""
    results = {"v1_ask_cites_doc": True}                     # every other required check is missing, so it failed
    fake = {"results": results, "steps": {"ask2": {"error": True, "status_code": 429}}, "spend_usd": 0.0,
            "within_budget": True}
    monkeypatch.setattr(smoke, "run", lambda base_url, max_usd: fake)
    monkeypatch.setattr(smoke, "ARTIFACT_PATH", tmp_path / "workspace_smoke.json")
    assert smoke.main([]) == 2
    out = capsys.readouterr().out
    assert "INCONCLUSIVE: ask2 answered 429" in out
    for failure in smoke.evaluate_g1(results):
        assert f"FAIL: {failure}" in out


def test_redact_removes_the_workspace_token_and_hashes_the_workspace_id():
    """The artifact is committed: a workspace token must never be in it (even a deleted workspace's), and a raw
    workspace id is logged nowhere else either (docs/v2/M4_PLAN.md 5)."""
    out = smoke.redact({"created": {"workspace_id": "a" * 32, "token": "secret-token-value", "expires_at": "x"}})
    assert "secret-token-value" not in str(out) and "a" * 32 not in str(out)
    assert out["created"]["token"] == "<secret>" and out["created"]["workspace_id"].startswith("<ws:")


class _RecordingClient:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return type("R", (), {"status_code": self.statuses.pop(0)})()

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def delete(self, url, **kw):
        return self._answer("DELETE", url, **kw)


def test_the_after_delete_probe_uses_a_valid_question_and_a_real_job_id():
    """A 3-character question fails validation (400) before the workspace check it means to test, and a made-up job id
    proves nothing about the job route: the probe must reach the workspace check on every route."""
    client = _RecordingClient([404] * 7)
    ok, statuses = smoke._all_workspace_routes_404(client, "http://x", "a" * 32, "t", "0123456789ab",
                                                   "doc:0123456789ab:v1:0000", "job123")
    assert ok and set(statuses.values()) == {404} and len(statuses) == 7
    ask = [kw for method, url, kw in client.calls if url.endswith("/api/ask")][0]
    assert len(ask["json"]["question"]) >= 8
    assert any(url.endswith("/jobs/job123") for _, url, _ in client.calls)


def test_the_after_delete_probe_names_the_route_that_still_answered():
    client = _RecordingClient([404, 404, 404, 404, 404, 400, 404])
    ok, statuses = smoke._all_workspace_routes_404(client, "http://x", "a" * 32, "t", "0123456789ab",
                                                   "doc:0123456789ab:v1:0000", "job123")
    assert not ok and statuses["POST /api/ask"] == 400


# ---------------------------------------------------------------- round-2 review finding C6: a step raising must
# still delete the workspace and produce a report — never abort run()/main() with no artifact written at all.


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"simulated HTTP {self.status_code}")

    def json(self):
        return self._json


def test_ask_returns_an_error_marker_instead_of_raising_on_a_non_2xx_response():
    """C6: the third (merely observational) ask of a second G1 run within 10 minutes gets a 429 from the real
    per-address ask rate limit. Before the fix, `_ask` called `raise_for_status()` and this escaped `run()`."""
    class _Client:
        def post(self, url, **kw):
            return _FakeResponse(429)

    result = smoke._ask(_Client(), "http://x", "a" * 32, "t", "What does my document say about the market outlook?")
    assert result == {"error": True, "status_code": 429}


def test_ask_still_parses_a_successful_response_exactly_as_before():
    class _Client:
        def post(self, url, **kw):
            return _FakeResponse(200, text='data: {"event": "done", "answer": "hi", "citations": []}\n\n')

    result = smoke._ask(_Client(), "http://x", "a" * 32, "t", "What does my document say?")
    assert result == {"event": "done", "answer": "hi", "citations": []}


class _FailingUploadClient:
    """POST /api/workspace succeeds; the first upload then raises — modelling a transient error escaping the route
    before the normal flow ever reaches its own DELETE at the end of `_run_as_of_and_cleanup`."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        if url.endswith("/api/workspace"):
            return _FakeResponse(201, {"workspace_id": "a" * 32, "token": "tok", "expires_at": "later"})
        if url.endswith("/documents"):
            raise RuntimeError("simulated transient upload failure")
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kw):
        raise AssertionError(f"unexpected GET {url}")

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return _FakeResponse(204)


def test_redact_exception_text_scrubs_the_raw_workspace_id_and_token_from_an_error_message():
    """A real httpx.HTTPStatusError embeds the request URL (raw workspace id) in its message; the token never
    appears in a URL, but is scrubbed too as defense in depth. Neither may reach the committed artifact."""
    ws, token = "a" * 32, "super-secret-token"
    exc = RuntimeError(f"Client error '429' for url 'http://x/api/workspace/{ws}/documents' token={token}")
    text = smoke._redact_exception_text(exc, ws, token)
    assert ws not in text and token not in text
    assert text.startswith("RuntimeError: ") and f"<ws:{smoke._ws_hash(ws)}>" in text and "<secret>" in text


def test_run_g1_deletes_the_workspace_and_records_an_error_when_a_step_raises_partway():
    client = _FailingUploadClient()
    report = smoke._run_g1(client, "http://x", 0.5)
    assert "error" in report and "simulated transient upload failure" in report["error"]
    deletes = [c for c in client.calls if c[0] == "DELETE"]
    assert deletes == [("DELETE", f"http://x/api/workspace/{'a' * 32}")]
    # every G1 check is reported as FAILED (none of the steps that would set them ever ran) — never an exception,
    # and evaluate_g1/main() can still print a verdict and write an artifact from this.
    assert smoke.evaluate_g1(report["results"]) == smoke.evaluate_g1({})


def test_run_g1_never_raises_when_the_workspace_itself_cannot_even_be_created():
    class _AlwaysFailsClient:
        def post(self, url, **kw):
            raise RuntimeError("simulated network error creating the workspace")

        def delete(self, url, **kw):
            raise AssertionError("nothing to delete: no workspace was ever created")

    report = smoke._run_g1(_AlwaysFailsClient(), "http://x", 0.5)   # must not raise
    assert "error" in report and report["results"] == {}
    assert report["steps"]["created"] is None


def test_run_g1_still_deletes_when_the_delete_call_itself_also_fails():
    """The `finally` cleanup is itself best-effort: an unreachable service must not turn a reported step failure
    into an unhandled exception from _run_g1 — the workspace is simply left to its own TTL."""
    class _Client:
        def post(self, url, **kw):
            if url.endswith("/api/workspace"):
                return _FakeResponse(201, {"workspace_id": "a" * 32, "token": "tok", "expires_at": "later"})
            raise RuntimeError("boom")

        def delete(self, url, **kw):
            raise RuntimeError("the service is unreachable")

    report = smoke._run_g1(_Client(), "http://x", 0.5)   # must not raise, despite delete() also raising
    assert "error" in report and "boom" in report["error"]


class _AsOfCleanupClient:
    """Everything `_run_as_of_and_cleanup` touches. The observational as_of=yesterday ask answers 429 — the exact
    scenario finding C6 named — and every route answers 404 once DELETE has actually been called."""

    def __init__(self, document_id: str):
        self.document_id = document_id
        self.deleted = False
        self.ask_calls = 0

    def post(self, url, **kw):
        if url.endswith("/api/ask"):
            self.ask_calls += 1
            return _FakeResponse(404 if self.deleted else 429)
        if url.endswith("/documents"):
            return _FakeResponse(404 if self.deleted else 202, {})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kw):
        if self.deleted:
            return _FakeResponse(404)
        if url.endswith("/changes"):
            return _FakeResponse(200, {"items_compared": True,
                                       "changed": [{"headline": "Market Outlook"}],
                                       "added": [{"headline": "Cybersecurity Practices"}],
                                       "removed": [{"headline": "Legal Proceedings"}], "unchanged_count": 1})
        if "/evidence/" in url:
            return _FakeResponse(200, {"document_id": self.document_id, "is_current": False,
                                       "status": "superseded", "superseded_by_version": 2})
        return _FakeResponse(404)

    def delete(self, url, **kw):
        was_deleted = self.deleted
        self.deleted = True
        return _FakeResponse(404 if was_deleted else 204)


def test_run_as_of_and_cleanup_treats_a_failed_observational_ask_as_one_failed_check_not_a_crash():
    """C6: `as_of_date_before_creation_empty` (fed by the failed ask) is reported False, but the change report,
    evidence and delete-then-404 checks — none of which depend on that ask — still run and still pass. The
    observational ask never aborts the checks after it."""
    client = _AsOfCleanupClient(document_id="doc1")
    result = smoke._run_as_of_and_cleanup(client, "http://x", "a" * 32, "t", "doc1", "doc:doc1:v1:0000", "job1")
    assert result["results"]["as_of_date_before_creation_empty"] is False
    assert result["results"]["changes_match_edit_set"] is True
    assert result["results"]["evidence_ok"] is True
    assert result["results"]["deleted_then_404"] is True
    assert client.ask_calls == 2   # the observational ask itself, then its own post-delete 404 probe
