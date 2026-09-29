"""What the planner model may see (prompt-injection defence, docs/v2/M3_AGENT_PLAN.md section 1).

The planner sees counts, ids, fiscal years, metric names / values / units and company names from the fixed universe, and nothing
else: never chunk text, risk summaries, headlines, passage text, quotes or rule titles. The views are ALLOWLISTS (each field passes a
strict check or is dropped), not a search for bad words: the tests plant an injection in every free-text field and prove it never
comes out.
"""

import json

import pytest
from agent_fakes import (
    AMD,
    CANARY,
    INJECTION,
    NVDA,
    NVDA_ACC,
    TSMC,
    FakeDriver,
    FakeEmbedder,
    edge_row,
    metric_rows,
    pair_columns,
    poison_rows,
    poisoned_world,
    rule_row,
)

from semigraph.agent import sanitize as S
from semigraph.retrieval.retriever import hybrid_retrieve


@pytest.mark.parametrize("text,expected", [
    ("Nvidia", "Nvidia"), ("nvidia", "Nvidia"), ("  NVDA ", "Nvidia"), ("Nvidia Corporation", "Nvidia"),
    ("Advanced Micro Devices", "AMD"), ("taiwan semi", "TSMC"), ("SK hynix", "SK Hynix"),
    ("Advanced Micro Devices, Inc.", "AMD"),                 # a graph node name: one alias inside it
])
def test_a_company_resolves_to_its_canonical_name(text, expected):
    assert S.canonical_name(text) == expected


@pytest.mark.parametrize("text", ["", "Acme Robotics", "Nvidia and AMD", INJECTION, None, 42])
def test_an_unknown_or_ambiguous_company_resolves_to_nothing(text):
    assert S.canonical_name(text) is None


def test_a_string_that_mentions_a_company_only_ever_yields_the_canonical_name_never_the_string():
    assert S.canonical_name(f"{INJECTION} TSMC") == "TSMC"
    assert CANARY not in json.dumps(S.canonical_name(f"{INJECTION} TSMC"))


def test_company_ids_map_to_canonical_names_for_filers_and_for_synthetic_ids():
    assert S.company_for_id(NVDA) == "Nvidia" and S.company_for_id("1045810") == "Nvidia" and S.company_for_id(-1) == "Samsung"
    assert S.company_for_id(12345) is None and S.company_for_id(None) is None


def test_filers_are_the_companies_with_a_cik():
    assert "Nvidia" in S.SEC_FILERS and "Samsung" not in S.SEC_FILERS and "Samsung" in S.KNOWN_COMPANIES


@pytest.mark.parametrize("fn,good,bad", [
    (S.safe_metric, "revenue", "revenue " + INJECTION),
    (S.safe_metric, "net_income", "Net Income"),
    (S.safe_date, "2026-01-25", f"2026-01-25 {INJECTION}"),
    (S.safe_unit, "USD", f"USD {INJECTION}"),
    (S.safe_unit, "TWD", "United States dollars"),
    (S.safe_unit, "USD/shares", "USD/" + INJECTION),
    (S.safe_id, "xbrl:1045810:revenue:2026-01-25", f"xbrl:1045810:revenue:2026-01-25 {INJECTION}"),
    (S.safe_id, "0001045810-26-000021:I.1A:0361", INJECTION),
    (S.safe_id, "fr:2026-19537", "fr:" + INJECTION),
])
def test_the_allowlist_checks_accept_the_grammar_and_reject_everything_else(fn, good, bad):
    assert fn(good) == good
    assert fn(bad) is None


def test_an_uploaded_document_id_never_reaches_the_planner():
    """M4 (docs/v2/M4_PLAN.md 4.2): ``doc:`` ids are well-formed citations for the workspace writer, but uploaded text is
    user-controlled, so no ``doc:`` id is ever allowlisted into anything the agent planner sees (structural, on top of the 400
    that ``strategy=agent`` + a workspace gets at the route)."""
    assert S.safe_id("doc:0123456789ab:v1:0001") is None


def test_numbers_and_years_are_numbers_only():
    assert S.safe_number(215938000000.0) == 215938000000 and S.safe_number(1.5) == 1.5 and S.safe_number(-3) == -3
    for bad in (True, None, "12", float("nan"), float("inf"), INJECTION):
        assert S.safe_number(bad) is None
    assert S.safe_year(2025) == 2025 and S.safe_year(1800) is None and S.safe_year("2025") is None and S.safe_year(True) is None


def test_the_metric_view_names_companies_metrics_periods_values_and_units_and_nothing_else():
    rows = metric_rows() + metric_rows(metric="net_income", series={"2026-01-25": 1.0})
    view = S.view_metrics(rows)
    assert view["companies"] == [{"company": "Nvidia", "metrics": {
        "revenue": [{"period_end": "2026-01-25", "value": 215938000000, "unit": "USD"},
                    {"period_end": "2025-01-26", "value": 130497000000, "unit": "USD"},
                    {"period_end": "2024-01-28", "value": 60922000000, "unit": "USD"},
                    {"period_end": "2023-01-29", "value": 26974000000, "unit": "USD"}],
        "net_income": [{"period_end": "2026-01-25", "value": 1, "unit": "USD"}]}}]
    assert view["truncated"] is False


def test_the_metric_view_drops_what_fails_the_allowlist_and_keeps_the_rest():
    rows = poison_rows(metric_rows(), company_names=True)                        # unit and company name poisoned, cik intact
    rows += [{**metric_rows()[0], "metric": f"revenue {INJECTION}"}]              # a poisoned metric NAME: the row is dropped
    text = json.dumps(S.view_metrics(rows))
    assert CANARY not in text and "Ignore" not in text
    view = S.view_metrics(rows)["companies"][0]
    assert view["company"] == "Nvidia" and list(view["metrics"]) == ["revenue"]
    assert all(period["unit"] is None for period in view["metrics"]["revenue"])


def test_the_metric_view_is_capped_and_says_so():
    """Three companies, all four known metrics, eight periods each: 96 candidate rows, way over the view's cap -- and every
    metric name is a REAL one (S.KNOWN_METRICS is a fixed vocabulary now, M3 finding #8, so an invented name would just be
    dropped, proving nothing about the cap)."""
    rows = [row for cik, company in ((NVDA, "Nvidia"), (AMD, "AMD"), (TSMC, "TSMC")) for metric in S.KNOWN_METRICS
            for row in metric_rows(cik, company, metric, series={f"20{20 + n:02d}-01-01": float(n) for n in range(8)})]
    view = S.view_metrics(rows)
    assert sum(len(p) for c in view["companies"] for p in c["metrics"].values()) <= S.MAX_VIEW_ROWS
    assert view["truncated"] is True


def test_an_out_of_vocabulary_metric_name_never_reaches_the_planners_prefetch_summary_or_any_tool_result():
    """A name that used to pass the old permissive ``[a-z][a-z0-9_]*`` shape check (M3 finding #8) is now just an unknown
    metric: dropped from the view like any other failed check, never shown."""
    bad = "ignore_previous_instructions"
    assert S.safe_metric(bad) is None and S.safe_metric("revenue") == "revenue"
    rows = metric_rows() + [{**metric_rows()[0], "metric": bad}]
    view = S.view_metrics(rows)["companies"][0]
    assert list(view["metrics"]) == ["revenue"] and bad not in json.dumps(view)
    r = {"anchors": {"Nvidia": NVDA}, "anchor_defaulted": False, "metrics": rows, "edges": [], "risks": [], "chunks": [],
         "temporal_pairs": []}
    assert bad not in json.dumps(S.prefetch_summary(r))


def test_the_pair_view_carries_totals_fiscal_years_and_dates_never_headlines_or_reasons():
    pair = {**pair_columns("Nvidia", NVDA, NVDA_ACC["n25"], NVDA_ACC["n26"]), "older_fy": 2025, "newer_fy": 2026,
            "totals": {"removed": 2, "unsettled": 0, "new": 1, "reworded": 0}, "passage_totals": {"removed": 3, "added": 0, "reworded": 1},
            "not_compared_reason": INJECTION}
    view = S.view_pairs([pair])
    assert view == [{"company": "Nvidia", "compared": True, "older_filed": "2025-02-26", "newer_filed": "2026-02-25",
                     "older_fiscal_year": 2025, "newer_fiscal_year": 2026,
                     "risk_items": {"removed": 2, "unsettled": 0, "new": 1, "reworded": 0},
                     "passages": {"removed": 3, "added": 0, "reworded": 1}}]
    assert CANARY not in json.dumps(view)


def test_a_pair_that_could_not_be_compared_says_only_that():
    pair = {"company": "Intel", "cik": 50863, "compared": False, "not_compared_reason": INJECTION, "older_date": "2025-01-31",
            "newer_date": "2026-01-30", "totals": {"removed": 0}}
    view = S.view_pairs([pair])[0]
    assert view["compared"] is False and CANARY not in json.dumps(view)


def test_the_edge_view_counts_relations_and_names_only_universe_companies_and_valid_rule_ids():
    rows = [edge_row(target="TSMC"), edge_row(target="Acme " + INJECTION, n=2), edge_row(relation="COMPETES_WITH", target="Samsung", n=3),
            edge_row(relation=f"DROP TABLE {INJECTION}", target="AMD", n=4), rule_row("2026-19537", title=INJECTION),
            {**rule_row("2026-19538"), "rule_id": INJECTION}]
    view = S.view_edges(rows)
    assert view["by_relation"] == {"DEPENDS_ON": 2, "COMPETES_WITH": 1, "AFFECTED_BY": 2}
    assert view["companies"] == ["Nvidia", "Samsung", "TSMC"]                    # the edge with an unknown relation is skipped whole
    assert view["other_relations"] == 1 and view["other_companies"] == 1 and view["rule_ids"] == ["fr:2026-19537"]
    assert CANARY not in json.dumps(view) and "Ignore" not in json.dumps(view)


def test_the_prefetch_summary_of_a_fully_poisoned_graph_carries_no_injection():
    r = hybrid_retrieve("How exposed is Nvidia to TSMC, and how did its risks change?", poisoned_world(), FakeEmbedder())
    assert any(CANARY in json.dumps(chunk) for chunk in r["chunks"])           # the graph really is poisoned
    summary = S.prefetch_summary(r)
    assert CANARY not in json.dumps(summary) and "Ignore" not in json.dumps(summary)
    assert summary["companies_in_question"] == ["Nvidia", "TSMC"] and summary["defaulted_to"] is None
    assert summary["metrics"]["Nvidia"]["revenue"][0] == "2026-01-25"
    assert summary["counts"]["chunks"] == len(r["chunks"]) and summary["counts"]["rules"] == 1
    assert summary["risk_comparisons"] and summary["risk_comparisons"][0]["company"] == "Nvidia"


def test_the_prefetch_summary_says_when_no_company_was_named():
    r = hybrid_retrieve("What is the weather?", FakeDriver.world(), FakeEmbedder())
    summary = S.prefetch_summary(r)
    assert summary["companies_in_question"] == [] and summary["defaulted_to"] == "Nvidia"


def test_recorded_tool_names_and_arguments_are_clipped_to_something_safe_to_emit():
    assert S.clip_name("financial_metrics") == "financial_metrics"
    assert S.clip_name("run cypher; DROP") == "run_cypher__DROP" and S.clip_name("") == "invalid" and len(S.clip_name("x" * 500)) == 40
    args = S.clip_args({"query": "x" * 500, "companies": [f"c{i}" for i in range(30)], "nested": {"a": {"b": {"c": 1}}}, 7: "n"})
    assert len(args["query"]) == 80 and len(args["companies"]) == 10 and set(args) == {"query", "companies", "nested", "7"}
    assert S.clip_args("not a dict") == {} and S.clip_args(None) == {}


def test_a_pair_of_a_company_outside_the_universe_is_left_out_and_odd_argument_values_are_stringified_and_clipped():
    assert S.view_pairs([{"company": "Acme Robotics", "cik": 42, "compared": True}]) == []
    assert S.clip_args({"thing": object()})["thing"].startswith("<object object")
