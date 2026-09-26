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
    from semigraph.graph import client, item_loader, items, loaders, schema, temporal

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
    monkeypatch.setattr(temporal, "apply_current_status", rec("apply_current_status", {}))
    monkeypatch.setattr(item_loader, "load_risk_items", rec("load_risk_items", {"items": 0}))
    monkeypatch.setattr(items, "discover_tickers", lambda settings, requested=None: list(requested or ["NVDA"]))
    monkeypatch.setattr(items, "require_alignment", rec("require_alignment"))
    monkeypatch.setattr("semigraph.embeddings.Embedder", lambda *a, **k: object())
    from semigraph.extraction import extractor
    monkeypatch.setattr(extractor, "build_extraction_plan", lambda settings, tickers=None: ({}, {}))
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
    assert order.index("load_knowledge") < order.index("load_risk_items")         # OF_ITEM needs the RiskFactors, SPANS the spans
    assert order.index("load_risk_items") < order.index("load_export_controls")
    assert order.index("normalize_categories") < order.index("load_export_controls")
    assert order.index("load_export_controls") < order.index("apply_current_status") < order.index("stamp_snapshot")
    assert "apply_closure" not in order
    assert order[-1] == "close"
    sid = dict(graph_calls)["load_companies"]["snapshot_id"]
    assert sid == "snap-20260924-test"          # default as-of = the newest date in the lake
    assert all(kw.get("snapshot_id") == sid for n, kw in graph_calls
               if n in ("load_companies", "load_filings_and_sections", "load_metrics", "load_evidence_spans",
                        "load_knowledge", "load_risk_items", "load_export_controls"))


def test_build_graph_rebuild_resets_before_applying_the_schema(graph_calls):
    result = runner.invoke(app, ["build-graph", "--rebuild", "--yes"])

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


# --------------------------------------------- build-graph guardrails (verifier findings 4 and 5)

@pytest.fixture
def plan(monkeypatch):
    """Control what `build-graph` believes is left to extract."""
    from semigraph.extraction import extractor

    state = {"todo": {}}
    monkeypatch.setattr(extractor, "build_extraction_plan", lambda settings, tickers=None: ({}, state["todo"]))
    return state


class _Rows(list):
    """Minimal stand-in for a DataFrame in the completeness check (only len() is used)."""


def test_build_graph_refuses_while_current_filings_are_partly_unextracted(graph_calls, plan):
    plan["todo"] = {"MSFT": _Rows(range(41)), "AMD": _Rows(range(17))}

    result = runner.invoke(app, ["build-graph"])

    assert result.exit_code == 4
    assert "58" in result.output and "MSFT" in result.output and "--allow-partial" in result.output
    assert "apply_schema" not in names(graph_calls)            # nothing was written


def test_build_graph_allow_partial_proceeds_with_a_warning(graph_calls, plan):
    plan["todo"] = {"MSFT": _Rows(range(3))}

    result = runner.invoke(app, ["build-graph", "--allow-partial"])

    assert result.exit_code == 0, result.output
    assert "partial" in result.output.lower() and "apply_schema" in names(graph_calls)


def test_build_graph_rebuild_rejects_a_ticker_subset(graph_calls, plan):
    result = runner.invoke(app, ["build-graph", "--rebuild", "--yes", "-t", "NVDA"])

    assert result.exit_code != 0 and "reset_graph" not in names(graph_calls)
    assert "whole graph" in result.output


def test_build_graph_rebuild_asks_for_confirmation_showing_the_target(graph_calls, plan):
    result = runner.invoke(app, ["build-graph", "--rebuild"], input="n\n")

    assert result.exit_code != 0 and "reset_graph" not in names(graph_calls)
    assert "bolt://" in result.output          # the operator sees WHICH server is about to be wiped


def test_build_graph_rebuild_with_yes_skips_the_prompt(graph_calls, plan):
    result = runner.invoke(app, ["build-graph", "--rebuild", "--yes"])

    assert result.exit_code == 0, result.output
    assert names(graph_calls).index("reset_graph") < names(graph_calls).index("apply_schema")


def test_build_graph_has_no_paid_extraction_path_any_more(graph_calls, plan):
    result = runner.invoke(app, ["build-graph", "--extract"])

    assert result.exit_code != 0            # extraction lives in `semigraph extract` with its --max-usd cap


# --------------------------------------------- the item layer in build-graph (M1b step 4)

def test_build_graph_stops_before_touching_the_graph_when_the_alignment_is_missing(graph_calls, monkeypatch):
    """A --rebuild that wiped the graph and only then found the item layer missing would leave it empty."""
    from semigraph.graph import items

    def missing(settings, tickers):
        raise items.AlignItemsError("no risk-item alignment for ['NVDA']: run `semigraph align-items` first")

    monkeypatch.setattr(items, "require_alignment", missing)
    result = runner.invoke(app, ["build-graph", "--rebuild", "--yes"])

    assert result.exit_code == 2 and "run `semigraph align-items` first" in result.output
    assert not {"reset_graph", "apply_schema", "load_companies", "load_risk_items"} & set(names(graph_calls))


def test_build_graph_checks_the_alignment_of_the_requested_tickers_only(graph_calls, monkeypatch):
    from semigraph.graph import items

    seen = {}
    monkeypatch.setattr(items, "require_alignment", lambda settings, tickers: seen.setdefault("tickers", tickers))
    runner.invoke(app, ["build-graph", "-t", "AMD"])

    assert seen["tickers"] == ["AMD"]


# --------------------------------------------- align-items

@pytest.fixture
def align_run(monkeypatch, tmp_path):
    """Replace the pipeline with a recorder; the CLI's own wiring, printing and exit codes are what is under test."""
    from semigraph import cli
    from semigraph.config import Settings
    from semigraph.graph import adjudicate as adj
    from semigraph.graph import items

    state = {"calls": [], "raises": None, "estimate": None, "written": [tmp_path / "NVDA_pairs.parquet"], "dry": False}

    def fake(settings, tickers=None, **kwargs):
        state["calls"].append({"tickers": tickers, **kwargs})
        if state["raises"]:
            raise state["raises"]
        summary = [{"pair_id": "NVDA-a-b", "ticker": "NVDA", "older_date": "d", "newer_date": "e", "compared": True,
                    "not_compared_reason": None, "older_items": 2, "newer_items": 2, "older_unchanged": 1, "older_reworded": 1,
                    "older_merged": 0, "older_removed": 0, "older_uncertain": 0, "newer_carried": 2, "newer_new": 0,
                    "newer_uncertain": 0, "passages_removed": 1, "passages_reworded": 0, "passages_added": 0, "adjudicated": 0},
                   {"pair_id": "INTC-a-b", "ticker": "INTC", "older_date": "d", "newer_date": "e", "compared": False,
                    "not_compared_reason": "older filing's risk items cover only 75.8% of its risk section text"}]
        return items.AlignRun(summary, state["estimate"], [] if kwargs.get("dry_run") else state["written"], bool(kwargs.get("dry_run")))

    monkeypatch.setattr(cli, "_settings", lambda: Settings(data_dir=tmp_path / "data", _env_file=None))
    monkeypatch.setattr(items, "run_align_items", fake)
    state["adj"] = adj
    return state


def test_align_items_defaults_to_no_adjudication_a_half_dollar_cap_and_every_ticker(align_run):
    result = runner.invoke(app, ["align-items"])

    assert result.exit_code == 0, result.output
    assert align_run["calls"] == [{"tickers": None, "adjudicate": False, "max_usd": 0.5, "dry_run": False}]


def test_align_items_prints_the_per_pair_table_the_not_compared_reason_and_the_files_written(align_run):
    result = runner.invoke(app, ["align-items", "-t", "NVDA", "-t", "INTC"])

    assert align_run["calls"][0]["tickers"] == ["NVDA", "INTC"]
    assert "NVDA-a-b" in result.output and "REMOVED" in result.output
    assert "NOT COMPARED" in result.output and "75.8%" in result.output
    assert "NVDA_pairs.parquet" in result.output


def test_align_items_dry_run_writes_nothing_and_says_so(align_run):
    result = runner.invoke(app, ["align-items", "--dry-run"])

    assert align_run["calls"][0]["dry_run"] is True and "nothing was written and no model was called" in result.output
    assert "NVDA_pairs.parquet" not in result.output


def test_align_items_adjudicate_prints_the_estimate_and_warns_when_the_cap_would_refuse(align_run):
    from semigraph.graph import adjudicate as adj

    align_run["estimate"] = adj.Estimate("openai/gpt-6-luna", 12, 2, 10, 24000, 10000, 0.6, 0.004, True)
    result = runner.invoke(app, ["align-items", "--adjudicate", "--dry-run", "--max-usd", "0.5"])

    assert align_run["calls"][0]["adjudicate"] is True and align_run["calls"][0]["max_usd"] == 0.5
    assert "12 item(s) to settle (2 already answered)" in result.output and "10 call(s)" in result.output
    assert "worst case $0.6000" in result.output and "would be refused before any call" in result.output


def test_align_items_exits_3_when_the_worst_case_is_over_the_cap(align_run):
    from semigraph.graph import adjudicate as adj

    align_run["raises"] = adj.BudgetExceeded("worst case $0.6000 for 10 call(s) exceeds --max-usd $0.50: nothing was spent")
    result = runner.invoke(app, ["align-items", "--adjudicate"])

    assert result.exit_code == 3 and "exceeds --max-usd" in result.output and "nothing was spent" in result.output


def test_align_items_exits_2_with_the_message_when_an_input_is_missing(align_run):
    from semigraph.graph import items

    align_run["raises"] = items.AlignItemsError("no risk items in data/interim/risk_items: run `semigraph risk-items` first")
    result = runner.invoke(app, ["align-items"])

    assert result.exit_code == 2 and "run `semigraph risk-items` first" in result.output
