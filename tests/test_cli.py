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


def test_ingest_exits_nonzero_but_still_parses_when_a_filing_failed(calls, monkeypatch):
    from semigraph.ingestion import edgar

    monkeypatch.setattr(edgar, "download_filings", lambda *a, **k: {
        "tickers": {"INTC": {"filings": 5, "new": [], "cached": True, "failed": [
            {"accession_no": "0000050863-26-000157", "form": "10-Q", "filing_date": "2026-07-24", "error": "boom"}]}},
        "total_filings": 5, "failed_total": 1})

    result = runner.invoke(app, ["ingest"])

    assert result.exit_code == 2
    assert "0000050863-26-000157" in result.output and "NOT ingested" in result.output
    assert "segment" in calls and "chunk" in calls      # what did land is still parsed


# ------------------------------------------------------------------ extract (paid)

@pytest.fixture
def extraction(monkeypatch):
    """Fake the paid stage; record whether it ran."""
    from semigraph.extraction import extractor, resolution

    ran: dict = {}
    est = {"n_chunks": 660, "chunk_tokens": 230933, "likely_usd": 3.67, "worst_case_usd": 5.51, "per_ticker": {"NVDA": 40}}
    monkeypatch.setattr(extractor, "build_extraction_plan", lambda settings, tickers=None: ({}, {"NVDA": None}))
    monkeypatch.setattr(extractor, "estimate_extraction_cost", lambda todo, settings=None: est)
    monkeypatch.setattr(extractor, "run_extraction", lambda settings, tickers=None: ran.setdefault("extract", {"NVDA": 40}))
    monkeypatch.setattr(resolution, "resolve_extractions", lambda settings, tickers=None: ran.setdefault("resolve", True))
    return ran


def test_extract_prints_the_estimate_and_runs_within_the_cap(extraction):
    result = runner.invoke(app, ["extract", "--yes", "--max-usd", "6"])

    assert result.exit_code == 0, result.output
    assert "3.67" in result.output and "5.51" in result.output
    assert extraction == {"extract": {"NVDA": 40}, "resolve": True}


def test_extract_refuses_to_start_when_the_worst_case_exceeds_the_cap(extraction):
    result = runner.invoke(app, ["extract", "--yes", "--max-usd", "5"])

    assert result.exit_code == 3
    assert "exceeds" in result.output and extraction == {}          # nothing was spent


def test_extract_asks_before_spending_and_aborts_on_no(extraction):
    result = runner.invoke(app, ["extract", "--max-usd", "6"], input="n\n")

    assert result.exit_code != 0 and extraction == {}


def test_extract_dry_run_never_spends(extraction):
    result = runner.invoke(app, ["extract", "--dry-run"])

    assert result.exit_code == 0 and "5.51" in result.output and extraction == {}


def test_extract_requires_a_cap_for_a_paid_run(extraction):
    result = runner.invoke(app, ["extract", "--yes"])

    assert result.exit_code != 0 and extraction == {}


# ------------------------------------------------------------ build-graph orchestration

@pytest.fixture
def graph_calls(monkeypatch, tmp_path):
    """Record the order and arguments of every stage `build-graph` drives."""
    from datetime import date

    from semigraph import cli, snapshot
    from semigraph.graph import client, loaders, schema, temporal

    log: list[tuple[str, dict]] = []

    def rec(name, result=None):
        def fake(*args, **kwargs):
            log.append((name, kwargs))
            return result
        return fake

    class FakeDriver:
        def close(self):
            log.append(("close", {}))

    monkeypatch.setattr(cli, "_settings", lambda: __import__("semigraph.config", fromlist=["Settings"]).Settings(
        data_dir=tmp_path / "data", _env_file=None))
    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(client, "run_cypher", lambda driver, q, **p: [{"label": "Company", "n": 26}])
    monkeypatch.setattr(schema, "reset_graph", rec("reset_graph", {"deleted_nodes": 5}))
    monkeypatch.setattr(schema, "apply_schema", rec("apply_schema", 12))
    for name in ("load_companies", "load_filings_and_sections", "load_metrics", "load_evidence_spans",
                 "load_knowledge", "load_export_controls"):
        monkeypatch.setattr(loaders, name, rec(name, {}))
    monkeypatch.setattr(loaders, "stamp_snapshot", rec("stamp_snapshot", "x"))
    monkeypatch.setattr(temporal, "normalize_categories", rec("normalize_categories", 0))
    monkeypatch.setattr(temporal, "apply_closure", rec("apply_closure", {}))
    monkeypatch.setattr("semigraph.embeddings.Embedder", lambda *a, **k: object())
    monkeypatch.setattr(snapshot, "newest_lake_date", lambda settings: date(2026, 9, 24))
    monkeypatch.setattr(snapshot, "compute_snapshot_id", lambda settings, as_of=None, **k: f"snap-{as_of:%Y%m%d}-test")
    return log


def names(log):
    return [n for n, _ in log]


def test_build_graph_loads_in_dependency_order_and_stamps_the_snapshot(graph_calls):
    result = runner.invoke(app, ["build-graph"])

    assert result.exit_code == 0, result.output
    order = names(graph_calls)
    assert order.index("apply_schema") < order.index("load_companies") < order.index("load_filings_and_sections")
    assert order.index("load_evidence_spans") < order.index("load_knowledge")     # relation post-pass needs spans
    assert order.index("normalize_categories") < order.index("load_export_controls")
    assert order.index("load_export_controls") < order.index("apply_closure") < order.index("stamp_snapshot")
    assert order[-1] == "close"
    sid = dict(graph_calls)["load_companies"]["snapshot_id"]
    assert sid == "snap-20260924-test"          # default as-of = the newest date in the lake
    assert all(kw.get("snapshot_id") == sid for n, kw in graph_calls
               if n in ("load_companies", "load_filings_and_sections", "load_metrics", "load_evidence_spans",
                        "load_knowledge", "load_export_controls"))


def test_build_graph_rebuild_resets_before_applying_the_schema(graph_calls):
    result = runner.invoke(app, ["build-graph", "--rebuild"])

    assert result.exit_code == 0, result.output
    assert names(graph_calls).index("reset_graph") < names(graph_calls).index("apply_schema")


def test_build_graph_without_rebuild_never_resets(graph_calls):
    runner.invoke(app, ["build-graph"])

    assert "reset_graph" not in names(graph_calls)


def test_build_graph_refuses_an_as_of_older_than_the_lake(graph_calls):
    result = runner.invoke(app, ["build-graph", "--as-of", "2026-06-30"])

    assert result.exit_code != 0
    assert "newer than --as-of" in result.output and "apply_schema" not in names(graph_calls)


def test_build_graph_accepts_an_as_of_on_or_after_the_newest_data(graph_calls):
    result = runner.invoke(app, ["build-graph", "--as-of", "2026-09-25"])

    assert result.exit_code == 0, result.output
    assert dict(graph_calls)["load_companies"]["snapshot_id"] == "snap-20260925-test"
