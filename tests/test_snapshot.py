"""Snapshot identity: a stable hash of everything that defines the served corpus."""

import json
from datetime import date

import pytest

from semigraph.config import Settings
from semigraph.snapshot import compute_snapshot_id, snapshot_inputs


@pytest.fixture
def lake(tmp_path):
    """A minimal data lake: manifest, one extraction jsonl, FR cache, one metrics parquet stand-in."""
    s = Settings(data_dir=tmp_path / "data", _env_file=None)
    (s.raw_dir / "edgar").mkdir(parents=True)
    (s.raw_dir / "edgar" / "manifest_universe.json").write_text(json.dumps({"NVDA": [{"accession_no": "a-1"}]}))
    s.extractions_dir.mkdir(parents=True)
    (s.extractions_dir / "nvda_extractions.jsonl").write_text('{"chunk_id": "c1"}\n')
    (s.raw_dir / "federal_register_bis_rules.json").write_text(json.dumps({"results": [{"document_number": "2026-1"}]}))
    (s.processed_dir / "xbrl").mkdir(parents=True)
    (s.processed_dir / "xbrl" / "NVDA_key_metrics.parquet").write_bytes(b"parquet-bytes")
    return s


def test_same_lake_same_id(lake):
    assert compute_snapshot_id(lake, date(2026, 9, 25)) == compute_snapshot_id(lake, date(2026, 9, 25))


def test_id_embeds_the_as_of_date(lake):
    assert compute_snapshot_id(lake, date(2026, 9, 25)).startswith("snap-20260925-")


def test_as_of_changes_the_id(lake):
    assert compute_snapshot_id(lake, date(2026, 9, 25)) != compute_snapshot_id(lake, date(2026, 9, 26))


@pytest.mark.parametrize("mutate", [
    lambda s: (s.raw_dir / "edgar" / "manifest_universe.json").write_text("{}"),
    lambda s: (s.extractions_dir / "nvda_extractions.jsonl").write_text('{"chunk_id": "c2"}\n'),
    lambda s: (s.raw_dir / "federal_register_bis_rules.json").write_text('{"results": []}'),
    lambda s: (s.processed_dir / "xbrl" / "NVDA_key_metrics.parquet").write_bytes(b"other"),
])
def test_any_input_change_changes_the_id(lake, mutate):
    before = compute_snapshot_id(lake, date(2026, 9, 25))
    mutate(lake)
    assert compute_snapshot_id(lake, date(2026, 9, 25)) != before


def test_code_version_changes_the_id(lake):
    a = compute_snapshot_id(lake, date(2026, 9, 25), code_version="0.1.0")
    b = compute_snapshot_id(lake, date(2026, 9, 25), code_version="0.2.0")
    assert a != b


def test_missing_inputs_are_recorded_not_fatal(tmp_path):
    empty = Settings(data_dir=tmp_path / "nothing", _env_file=None)
    inputs = snapshot_inputs(empty)
    assert inputs["manifest"] is None and inputs["extractions"] == {} and inputs["federal_register"] is None
    assert compute_snapshot_id(empty, "2026-09-25").startswith("snap-20260925-")


def test_as_of_accepts_iso_string_and_none(lake):
    assert compute_snapshot_id(lake, "2026-09-25") == compute_snapshot_id(lake, date(2026, 9, 25))
    assert compute_snapshot_id(lake, None).startswith("snap-")


# ------------------------------------------------ newest date in the lake

from semigraph.snapshot import newest_lake_date  # noqa: E402


def test_newest_lake_date_is_the_latest_of_filings_and_rules(lake):
    (lake.raw_dir / "edgar" / "manifest_universe.json").write_text(json.dumps({
        "NVDA": [{"accession_no": "a", "filing_date": "2026-08-26"}, {"accession_no": "b", "filing_date": "2026-05-20"}],
        "AVGO": [{"accession_no": "c", "filing_date": "2026-09-10"}]}))
    (lake.raw_dir / "federal_register_bis_rules.json").write_text(json.dumps({
        "results": [{"publication_date": "2026-09-24"}, {"publication_date": "2025-01-01"}]}))

    assert newest_lake_date(lake) == date(2026, 9, 24)


def test_newest_lake_date_handles_missing_and_legacy_files(tmp_path):
    assert newest_lake_date(Settings(data_dir=tmp_path / "nothing", _env_file=None)) is None
