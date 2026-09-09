"""Error-analysis (failure taxonomy) tests — judge mocked, no LLM spend."""

import json

from semigraph.eval.error_analysis import (
    LABELS,
    FailureLabel,
    analyze_failures,
    classify_failures,
    is_failure,
    mechanical_label,
)

BENCH = [
    {"id": "N1", "type": "numeric", "q": "revenue?", "expect": {"value": 60_922_000_000}},
    {"id": "T1", "type": "temporal", "q": "dropped risks?", "judge_notes": "should affirm"},
    {"id": "D1", "type": "dependency", "q": "deps?", "expect": {"any_of": ["TSMC"]}},
    {"id": "U1", "type": "refusal", "q": "samsung revenue?"},
]


def score(id_, system="hybrid", **kw):
    base = {"id": id_, "system": system, "correct": True, "faithfulness": 1.0, "citation_ok": True}
    return {**base, **kw}


def run(id_, answer="some answer", system="hybrid"):
    return {"id": id_, "system": system, "answer": answer, "context": "ctx"}


class ScriptedTaxonomyJudge:
    def __init__(self, label="UNGROUNDED_CLAIM"):
        self.label, self.calls = label, []

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append((prompt, kw))
        assert model_cls is FailureLabel
        return FailureLabel(label=self.label, evidence="the answer says X")


def test_is_failure_thresholds():
    assert not is_failure(score("N1"))
    assert is_failure(score("N1", correct=False))
    assert is_failure(score("N1", faithfulness=0.5))
    assert is_failure(score("N1", citation_ok=False))


def test_mechanical_labels_cover_the_unambiguous_cases():
    assert mechanical_label(score("U1", correct=False), run("U1", "Samsung revenue was $1B"), BENCH[3]) == "REFUSAL_MISSED"
    assert mechanical_label(score("D1", correct=False), run("D1", "The context does not contain this."), BENCH[2]) == "REFUSAL_OVERTRIGGER"
    assert mechanical_label(score("D1", citation_ok=False), run("D1"), BENCH[2]) == "FORMAT_OR_CITATION"
    assert mechanical_label(score("T1", correct=False, context_recall=0.0), run("T1"), BENCH[1]) == "RETRIEVAL_MISS"
    assert mechanical_label(score("N1", correct=False), run("N1", "$5 billion"), BENCH[0]) == "NUMERIC_MISMATCH"
    assert mechanical_label(score("T1", correct=False, context_recall=1.0), run("T1"), BENCH[1]) is None


def test_classify_uses_judge_only_for_ambiguous_failures_and_validates_label():
    judge = ScriptedTaxonomyJudge("STALE_RISK_LEAK")
    scored = [score("N1"), score("T1", correct=False, context_recall=1.0),
              score("U1", correct=False), score("D1", faithfulness=0.3, context_recall=1.0)]
    rows = classify_failures(scored, [run("N1"), run("T1"), run("U1", "It was $1B"), run("D1")], BENCH,
                             judge=judge, model="haiku")
    assert [r["id"] for r in rows] == ["T1", "U1", "D1"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["U1"]["label"] == "REFUSAL_MISSED" and by_id["U1"]["mechanical"] is True
    assert by_id["T1"]["label"] == "STALE_RISK_LEAK" and by_id["T1"]["mechanical"] is False
    assert len(judge.calls) == 2 and judge.calls[0][1]["model"] == "haiku"
    assert "should affirm" in judge.calls[0][0] and "GRADING NOTES" in judge.calls[0][0]
    bad = ScriptedTaxonomyJudge("NOT_A_LABEL")
    [row] = classify_failures([score("T1", correct=False, context_recall=1.0)], [run("T1")], BENCH, judge=bad)
    assert row["label"] == "OTHER" and set(LABELS) >= {"OTHER", "RETRIEVAL_MISS"}


def test_analyze_failures_writes_report_with_counts(tmp_path):
    scored = [score("N1", correct=False), score("U1", correct=False), score("N1", system="vector")]
    report = analyze_failures(scored, [run("N1", "$5B"), run("U1", "It was $1B")], BENCH,
                              artifacts_dir=tmp_path, judge=ScriptedTaxonomyJudge())
    assert report["n_failures"] == 2 and report["mechanical_share"] == 1.0
    assert report["counts"] == {"hybrid": {"NUMERIC_MISMATCH": 1, "REFUSAL_MISSED": 1}}
    on_disk = json.loads((tmp_path / "error_analysis.json").read_text(encoding="utf-8"))
    assert on_disk["rows"][0]["label"] == "NUMERIC_MISMATCH" and on_disk["labels"] == list(LABELS)
