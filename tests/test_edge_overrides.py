"""M2a R10: curated edge overrides (reverse every row of an edge key; absent edge fails; Historical pass targets one edge)."""

import pytest

from semigraph.artifacts import load_canonical_entities
from semigraph.graph import overrides as ov

CANON = load_canonical_entities()
APPLE, GOOG, QCOM, HUAWEI = (CANON[n]["entity_id"] for n in ("Apple", "Alphabet", "Qualcomm", "Huawei"))
OVERRIDES = ov.load_edge_overrides()


def row(src, tgt, chunk, type_="CUSTOMER_OF"):
    return {"src": src, "tgt": tgt, "type": type_, "chunk_id": chunk, "quote": "q", "date": "2026-01-01"}


def test_packaged_overrides_are_the_cited_entries():
    assert [(e["source"], e["type"], e["action"], e["chunk_id"]) for e in OVERRIDES] == [
        ("Apple", "CUSTOMER_OF", "reverse", "0000320193-26-000020:II.1A:0002"),
        ("Huawei", "CUSTOMER_OF", "inactive", "0000804328-26-000061:II.1A:0015"),
        ("Qualcomm", "SUPPLIES_TO", "inactive", "0000804328-26-000061:II.1A:0015")]
    assert all(e["reason"] for e in OVERRIDES)


def test_rows_from_two_filers_both_land_on_the_reversed_edge():
    rows = [row(APPLE, GOOG, "apple:1"), row(APPLE, GOOG, "goog:1"), row(GOOG, APPLE, "other:1"),
            row(APPLE, GOOG, "x:1", type_="COMPETES_WITH")]
    out, hits = ov.apply_reverse_overrides(rows, OVERRIDES, CANON)
    assert [(r["src"], r["tgt"]) for r in out] == [(GOOG, APPLE), (GOOG, APPLE), (GOOG, APPLE), (APPLE, GOOG)]
    assert hits == {0: 2}
    assert rows[0]["src"] == APPLE   # input untouched


def test_an_override_with_no_matching_row_fails_the_build():
    _, hits = ov.apply_reverse_overrides([row(GOOG, APPLE, "c")], OVERRIDES, CANON)
    with pytest.raises(ov.EdgeOverrideError, match="Apple CUSTOMER_OF Alphabet"):
        ov.require_reverse_hits(OVERRIDES, hits)


class FakeDriver:
    def __init__(self, n):
        self.n, self.calls = n, []

    def session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        self.calls.append((query, params))
        return type("R", (), {"single": lambda _s: {"n": self.n}})()


def test_the_historical_pass_marks_both_twin_huawei_qualcomm_edges():
    d = FakeDriver(1)
    ov.apply_inactive_overrides(d, OVERRIDES, CANON)
    (q1, p1), (q2, p2) = d.calls
    assert "[r:CUSTOMER_OF]" in q1 and p1 == {"src": HUAWEI, "tgt": QCOM, "status": "Historical"}
    assert "[r:SUPPLIES_TO]" in q2 and p2 == {"src": QCOM, "tgt": HUAWEI, "status": "Historical"}


def test_the_historical_pass_fails_when_the_edge_is_absent():
    with pytest.raises(ov.EdgeOverrideError, match="Huawei CUSTOMER_OF Qualcomm"):
        ov.apply_inactive_overrides(FakeDriver(0), OVERRIDES, CANON)


def test_the_override_file_is_part_of_the_snapshot_inputs(tmp_path):
    from semigraph.config import Settings
    from semigraph.snapshot import snapshot_inputs
    assert snapshot_inputs(Settings(data_dir=tmp_path))["edge_overrides"]
