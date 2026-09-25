"""The retrieval fingerprint/diff used by the M1 before/after gate (pure functions)."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_retrieval.py"
spec = importlib.util.spec_from_file_location("compare_retrieval", SCRIPT)
cr = importlib.util.module_from_spec(spec)
sys.modules["compare_retrieval"] = cr
spec.loader.exec_module(cr)


def retrieval(**over):
    base = {
        "anchors": {"Nvidia": 1045810},
        "edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC"}],
        "metrics": [{"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25", "value": 1.0, "unit": "USD"}],
        "risks": [{"chunk_id": "c1"}], "temporal": [{"company": "Nvidia", "lineage": "1045810:3"}],
        "chunks": [{"chunk_id": "c2"}, {"chunk_id": "c3"}],
    }
    return {**base, **over}


def test_summary_is_order_independent_and_json_safe():
    a = cr.summarize(retrieval(chunks=[{"chunk_id": "c2"}, {"chunk_id": "c3"}]))
    b = cr.summarize(retrieval(chunks=[{"chunk_id": "c3"}, {"chunk_id": "c2"}]))
    assert a == b


def test_metrics_without_a_unit_are_treated_as_usd():
    m = {"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25", "value": 1.0}
    assert cr.summarize(retrieval(metrics=[m]))["metrics"] == cr.summarize(retrieval())["metrics"]


def test_identical_retrievals_have_an_empty_diff():
    s = cr.summarize(retrieval())
    d = cr.diff_summaries(s, s)
    assert all(not d[layer]["lost"] and not d[layer]["gained"] for layer in cr.LAYERS)
    assert d["anchors_changed"] is False


def test_diff_reports_lost_and_gained_items_per_layer():
    before = cr.summarize(retrieval())
    after = cr.summarize(retrieval(chunks=[{"chunk_id": "c3"}, {"chunk_id": "c9"}]))
    d = cr.diff_summaries(before, after)
    assert d["chunks"] == {"before": 2, "after": 2, "lost": ["c2"], "gained": ["c9"]}
    assert not d["edges"]["lost"]


def test_unit_change_is_visible_as_a_metric_difference():
    usd = cr.summarize(retrieval())
    twd = cr.summarize(retrieval(metrics=[{"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25",
                                           "value": 1.0, "unit": "TWD"}]))
    d = cr.diff_summaries(usd, twd)
    assert len(d["metrics"]["lost"]) == 1 and len(d["metrics"]["gained"]) == 1


def test_diff_snapshots_only_compares_questions_present_in_both():
    s = cr.summarize(retrieval())
    out = cr.diff_snapshots({"N1": s, "old-only": s}, {"N1": s, "new-only": s})
    assert list(out) == ["N1"]


def test_render_marks_identical_and_changed_questions():
    s = cr.summarize(retrieval())
    changed = cr.summarize(retrieval(chunks=[{"chunk_id": "cX"}]))
    text = cr.render_diff(cr.diff_snapshots({"N1": s, "N2": s}, {"N1": s, "N2": changed}))
    assert "N1: identical" in text and "N2: CHANGED chunks" in text and "lost: c2" in text and "gained: cX" in text


@pytest.mark.parametrize("n", [1, 3])
def test_render_truncates_long_lists(n):
    s = cr.summarize(retrieval(chunks=[]))
    many = cr.summarize(retrieval(chunks=[{"chunk_id": f"c{i}"} for i in range(10)]))
    text = cr.render_diff(cr.diff_snapshots({"Q": s}, {"Q": many}), limit=n)
    assert f"and {10 - n} more" in text
