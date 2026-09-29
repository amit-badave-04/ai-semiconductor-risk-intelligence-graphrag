"""Pure parts of ``scripts/workspace_smoke.py`` (M4 gate G1, docs/v2/M4_PLAN.md 9): redaction, budget arithmetic and
the G1 pass/fail evaluator. ``run``/``main`` (the parts that touch a real server) are NEVER called here or anywhere
in this worker's tests — the main session runs the smoke script itself, against a real local service."""

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
