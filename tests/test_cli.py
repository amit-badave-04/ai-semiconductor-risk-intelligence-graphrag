"""CLI wiring: options reach the right ingestion functions; no network, no LLM."""

import json

import pytest
from typer.testing import CliRunner

from semigraph.cli import app

runner = CliRunner()


@pytest.fixture
def calls(monkeypatch):
    """Replace every heavy stage with a recorder; return the recorded calls."""
    from semigraph.ingestion import edgar, federal_register, freshness, xbrl
    from semigraph.parsing import chunker, segmentation

    rec: dict[str, dict] = {}

    def recorder(name, result):
        def fake(*args, **kwargs):
            rec[name] = {"args": args, "kwargs": kwargs}
            return result
        return fake

    monkeypatch.setattr(edgar, "download_filings", recorder("edgar", {"tickers": {}, "total_filings": 0}))
    monkeypatch.setattr(xbrl, "extract_metrics", recorder("xbrl", {}))
    monkeypatch.setattr(federal_register, "download_bis_rules", recorder("fr", [{"document_number": "1"}]))
    monkeypatch.setattr(segmentation, "segment_filings", recorder("segment", {}))
    monkeypatch.setattr(chunker, "chunk_filings", recorder("chunk", {}))
    monkeypatch.setattr(freshness, "pending_filings", recorder("pending", [
        {"ticker": "NVDA", "form": "10-Q", "filing_date": "2026-08-26", "accession_no": "0001045810-26-000075",
         "cik": 1045810, "period_of_report": "2026-07-26"}]))
    monkeypatch.setattr(freshness, "federal_register_pending", recorder("frp", {
        "stored_count": 13, "stored_latest_date": "2025-09-16", "live_count": 166, "new_since_stored": 153}))
    return rec


def test_ingest_passes_as_of_and_refresh_flags(calls):
    result = runner.invoke(app, ["ingest", "--as-of", "2026-09-25", "--refresh-xbrl", "--refresh-fr"])

    assert result.exit_code == 0, result.output
    assert str(calls["edgar"]["kwargs"]["as_of"]) == "2026-09-25"
    assert calls["xbrl"]["kwargs"]["refresh"] is True
    assert calls["fr"]["kwargs"]["refresh"] is True and str(calls["fr"]["kwargs"]["as_of"]) == "2026-09-25"


def test_ingest_defaults_do_not_refresh_or_bound(calls):
    result = runner.invoke(app, ["ingest"])

    assert result.exit_code == 0, result.output
    assert calls["edgar"]["kwargs"].get("as_of") is None
    assert calls["xbrl"]["kwargs"]["refresh"] is False
    assert calls["fr"]["kwargs"]["refresh"] is False


def test_ingest_skip_download_only_parses(calls):
    result = runner.invoke(app, ["ingest", "--skip-download"])

    assert result.exit_code == 0, result.output
    assert "edgar" not in calls and "xbrl" not in calls and "fr" not in calls
    assert "segment" in calls and "chunk" in calls


def test_ingest_rejects_a_malformed_as_of(calls):
    result = runner.invoke(app, ["ingest", "--as-of", "25/09/2026"])

    assert result.exit_code != 0
    assert "edgar" not in calls


def test_freshness_reports_pending_filings_and_rules(calls):
    result = runner.invoke(app, ["freshness", "--as-of", "2026-09-25"])

    assert result.exit_code == 0, result.output
    assert "0001045810-26-000075" in result.output
    assert "153" in result.output          # new Federal Register rules since the stored cache
    assert str(calls["pending"]["kwargs"]["as_of"]) == "2026-09-25"


def test_freshness_json_output_is_machine_readable(calls):
    result = runner.invoke(app, ["freshness", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pending_filings"][0]["ticker"] == "NVDA"
    assert payload["federal_register"]["new_since_stored"] == 153


def test_snapshot_prints_the_id_for_the_lake(tmp_path, monkeypatch):
    from semigraph import cli
    from semigraph.config import Settings

    monkeypatch.setattr(cli, "_settings", lambda: Settings(data_dir=tmp_path / "data", _env_file=None))

    result = runner.invoke(app, ["snapshot", "--as-of", "2026-09-25"])

    assert result.exit_code == 0, result.output
    assert "snap-20260925-" in result.output
