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
           "changes_match_edit_set": True, "evidence_ok": True, "deleted_then_404": True}


def test_evaluate_g1_passes_when_every_check_is_true():
    assert smoke.evaluate_g1(ALL_PASS) == []


def test_evaluate_g1_reports_every_missing_or_false_check():
    assert smoke.evaluate_g1({}) == [
        "the v1 answer did not cite a doc: id from v1",
        "an identical re-upload was not reported unchanged",
        "v2 did not flip the v1 chunk(s) to is_current=false / status=superseded",
        "the change report did not match the known edit set",
        "the workspace evidence route did not return the expected chunk",
        "a workspace route still answered after DELETE",
    ]


def test_evaluate_g1_reports_exactly_the_failed_checks():
    partial = {**ALL_PASS, "evidence_ok": False, "deleted_then_404": False}
    failures = smoke.evaluate_g1(partial)
    assert failures == ["the workspace evidence route did not return the expected chunk",
                        "a workspace route still answered after DELETE"]


# ---------------------------------------------------------------- _budget_notes (observational, never a G1 failure)


def test_budget_notes_flag_the_stale_citation_and_as_of_observations_without_failing_g1():
    results = {**ALL_PASS, "stale_citation_named": False, "as_of_before_v2_returns_v1": False}
    notes = smoke._budget_notes(results)
    assert len(notes) == 2
    assert smoke.evaluate_g1(results) == []      # neither observation is a G1 failure


def test_budget_notes_are_empty_when_both_observations_held():
    results = {**ALL_PASS, "stale_citation_named": True, "as_of_before_v2_returns_v1": True}
    assert smoke._budget_notes(results) == []


# ---------------------------------------------------------------- the fixture's own known edit set is self-consistent


def test_the_md_fixture_pair_actually_differs_the_way_the_known_edit_set_claims():
    for headline in smoke.KNOWN_EDIT_SET["removed_headlines"]:
        assert f"# {headline}" in smoke.MD_V1 and f"# {headline}" not in smoke.MD_V2
    for headline in smoke.KNOWN_EDIT_SET["added_headlines"]:
        assert f"# {headline}" not in smoke.MD_V1 and f"# {headline}" in smoke.MD_V2
    for headline in smoke.KNOWN_EDIT_SET["changed_headlines"]:
        assert f"# {headline}" in smoke.MD_V1 and f"# {headline}" in smoke.MD_V2
