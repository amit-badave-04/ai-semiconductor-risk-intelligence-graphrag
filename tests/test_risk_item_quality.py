"""The per-filing quality sidecar: a filing whose items cannot be trusted is never compared with its neighbour."""

import json

from semigraph.parsing import risk_item_quality as q


def filing(acc, **over):
    return {"accession_no": acc, "form": "10-K", "filing_date": "2026-02-25", "section_id": "I.1A", "n_items": 24,
            "coverage": 0.96, "method": "bold", "low_coverage": False, "section_suspect": False, "section_chars": 90000,
            "notes": [], **over}


def test_quality_round_trips_and_keeps_only_the_gate_fields(tmp_path):
    path = tmp_path / "NVDA_risk_items_quality.json"
    q.write_quality(path, "NVDA", [filing("a-1"), filing("a-2", low_coverage=True, coverage=0.71, notes=["cut off"])])
    loaded = q.load_quality(tmp_path)
    assert set(loaded) == {"a-1", "a-2"}
    assert loaded["a-2"]["low_coverage"] is True and loaded["a-2"]["coverage"] == 0.71 and loaded["a-2"]["ticker"] == "NVDA"
    assert set(json.loads(path.read_text(encoding="utf-8"))["filings"][0]) >= {"accession_no", "coverage", "low_coverage",
                                                                                "section_suspect", "method", "n_items"}


def test_two_clean_filings_are_comparable():
    quality = {"a": filing("a"), "b": filing("b")}
    assert q.comparability("a", "b", quality) == (True, None)


def test_a_low_coverage_side_makes_the_pair_not_compared_and_names_the_side_and_the_reason():
    quality = {"a": {**filing("a"), "low_coverage": True, "coverage": 0.758, "ticker": "INTC"}, "b": filing("b")}
    ok, reason = q.comparability("a", "b", quality)
    assert ok is False and "older" in reason and "75.8%" in reason


def test_a_suspect_section_side_makes_the_pair_not_compared():
    quality = {"a": filing("a"), "b": {**filing("b"), "section_suspect": True}}
    ok, reason = q.comparability("a", "b", quality)
    assert ok is False and "newer" in reason and "section text" in reason


def test_a_filing_with_no_quality_record_is_not_compared_never_assumed_fine():
    ok, reason = q.comparability("a", "zzz", {"a": filing("a")})
    assert ok is False and "no quality record" in reason


def test_rewriting_a_ticker_replaces_its_file_atomically(tmp_path):
    path = tmp_path / "NVDA_risk_items_quality.json"
    q.write_quality(path, "NVDA", [filing("a-1")])
    q.write_quality(path, "NVDA", [filing("a-9")])
    assert set(q.load_quality(tmp_path)) == {"a-9"}


def test_building_a_ticker_writes_its_sidecar_next_to_the_item_parquet(tmp_path):
    from types import SimpleNamespace

    from semigraph.parsing import risk_items

    settings = SimpleNamespace(interim_dir=tmp_path)
    row = {c: None for c in risk_items.ITEM_COLUMNS} | {"item_id": "a-1:I.1A:i000", "accession_no": "a-1", "chunk_ids": ["c1"]}
    summary = risk_items._ticker_summary(settings, "NVDA", [row], [filing("a-1", low_coverage=True, coverage=0.7)], [], [], True)
    assert summary["written"] is True
    assert q.load_quality(tmp_path / "risk_items")["a-1"]["low_coverage"] is True


def test_a_coverage_only_run_writes_no_sidecar(tmp_path):
    from types import SimpleNamespace

    from semigraph.parsing import risk_items

    risk_items._ticker_summary(SimpleNamespace(interim_dir=tmp_path), "NVDA", [], [filing("a-1")], [], [], False)
    assert not list(tmp_path.rglob("*quality*"))
