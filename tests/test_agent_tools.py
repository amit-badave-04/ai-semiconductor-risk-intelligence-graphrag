"""The seven read-only tools: schemas, validation, the queries they run, what they merge and what the planner is told.

Every tool is read-only, parameterised Cypher through the retriever's own constants (NO text-to-Cypher: the fake driver refuses a
query it does not know), its arguments are validated pydantic models, a failure is a ``{"error": ...}`` result instead of an
exception, and what the planner is shown is counts / ids / years / metric values / universe company names only.
"""

import copy
import json

import pytest
from agent_fakes import (
    AMD,
    CANARY,
    INJECTION,
    NVDA,
    NVDA_ACC,
    NVDA_REVENUE,
    TSMC,
    FakeDriver,
    FakeEmbedder,
    poisoned_world,
)

from semigraph.agent import tools as T
from semigraph.eval.agent_eval import AGENT_TOOL_UNIVERSE
from semigraph.retrieval import retriever as R
from semigraph.retrieval.answerer import build_blocks
from semigraph.retrieval.retriever import hybrid_retrieve

QUESTION = "How did Nvidia's revenue and risk disclosures change?"


def setup(question=QUESTION, driver=None):
    driver = driver or FakeDriver.world()
    embedder = FakeEmbedder()
    r = hybrid_retrieve(question, driver, embedder)
    driver.calls.clear()
    embedder.queries.clear()
    return T.Toolbox(driver, embedder, question), r, driver, embedder


def run(box, r, name, args):
    before = copy.deepcopy(r)
    out = box.execute(name, args if isinstance(args, str) else json.dumps(args), r)
    assert r == before, f"{name} mutated the retrieval dict"
    return out


# --- the declared tools ------------------------------------------------------------------------------------------------------

def test_the_tools_are_exactly_the_universe_the_eval_harness_scores():
    assert set(T.TOOL_NAMES) == AGENT_TOOL_UNIVERSE and len(T.TOOL_NAMES) == len(set(T.TOOL_NAMES)) == 7


def test_the_specs_are_openai_function_schemas_that_forbid_extra_arguments():
    specs = T.tool_specs()
    assert [s["function"]["name"] for s in specs] == list(T.TOOL_NAMES)
    for spec in specs:
        params = spec["function"]["parameters"]
        assert spec["type"] == "function" and params["type"] == "object" and params["additionalProperties"] is False
        assert spec["function"]["description"] and '"title"' not in json.dumps(params)
    assert len(json.dumps(specs)) < 9000                               # a cost guard: the schemas ride on every planner call


def test_no_tool_takes_a_query_language_argument():
    """There is no text-to-Cypher on the public path: no tool schema has a property that could carry a query."""
    for spec in T.tool_specs():
        properties = spec["function"]["parameters"]["properties"]
        assert not {"cypher", "sql", "statement", "command"} & set(properties)


# --- refusals: nothing is executed --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,args", [
    ("run_cypher", {"query": "MATCH (n) DETACH DELETE n"}),
    ("delete_everything", {}),
    ("financial_metrics ", {"companies": ["Nvidia"]}),                 # a name that is nearly a tool is not one
    ("", {}),
    ("x" * 300, {}),
])
def test_a_tool_outside_the_declared_set_is_refused_and_nothing_runs(name, args):
    box, r, driver, embedder = setup()
    out = run(box, r, name, args)
    assert out.ok is False and "error" in out.result and out.r == r
    assert driver.calls == [] and embedder.queries == []
    assert len(out.tool) <= 40 and set(out.tool) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def test_a_tool_name_that_is_not_a_string_is_only_a_refusal():
    box, r, driver, _ = setup()
    out = box.execute(["financial_metrics"], "{}", r)
    assert out.ok is False and out.result["error"] == "unknown tool" and driver.calls == []


@pytest.mark.parametrize("name,args", [
    ("financial_metrics", {}),                                                       # companies is required
    ("financial_metrics", {"companies": []}),
    ("financial_metrics", {"companies": ["A", "B", "C", "D", "E"]}),                 # more than four
    ("financial_metrics", {"companies": ["Nvidia"], "cypher": "MATCH (n) RETURN n"}),   # an argument the tool does not have
    ("financial_metrics", {"companies": ["Nvidia"], "fiscal_years": [1066]}),
    ("financial_metrics", {"companies": ["Nvidia"], "fiscal_years": ["last year"]}),
    ("financial_metrics", {"companies": ["Nvidia"], "period_ends": ["2026-13-45"]}),
    ("financial_metrics", {"companies": ["Nvidia"], "metrics": ["Revenue; DROP"]}),
    ("financial_metrics", {"companies": "Nvidia"}),
    ("risk_changes", {"companies": ["Nvidia"], "multi_year": "maybe"}),
    ("relationships", {"companies": ["Nvidia"], "hops": 9}),
    ("search_filings", {"query": ""}),
    ("search_filings", {"query": "x" * 400}),
    ("search_filings", {"query": "tsmc", "k": 500}),
    ("compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "yesterday", "to_period_end": "2026-01-25"}),
    ("lookup_company", {"name": ""}),
    ("lookup_company", {"name": "Nvidia", "extra": 1}),
])
def test_invalid_arguments_are_refused_before_anything_runs(name, args):
    box, r, driver, embedder = setup()
    out = run(box, r, name, args)
    assert out.ok is False and out.tool == name and out.r == r
    assert set(out.result) == {"error"} and driver.calls == [] and embedder.queries == []


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null", "42", '{"companies": ["Nvidia"]', ""])
def test_arguments_that_are_not_a_json_object_are_refused(raw):
    box, r, driver, _ = setup()
    out = run(box, r, "financial_metrics", raw)
    assert out.ok is False and out.r == r and driver.calls == []


def test_a_refusal_never_echoes_what_the_model_sent():
    box, r, _, _ = setup()
    out = run(box, r, "financial_metrics", {"companies": [INJECTION], "cypher": INJECTION, "fiscal_years": [INJECTION]})
    assert CANARY not in json.dumps(out.result) and CANARY not in out.summary and "Ignore" not in out.summary
    assert all(len(json.dumps(v)) <= 1000 for v in out.args.values())            # what is recorded of the call is bounded


def test_an_unknown_company_is_an_error_that_lists_the_known_ones():
    box, r, driver, _ = setup()
    out = run(box, r, "financial_metrics", {"companies": ["Acme Robotics"]})
    assert out.ok is False and "Nvidia" in out.result["known_companies"] and driver.calls == []
    assert "Acme" not in json.dumps(out.result)


def test_a_database_failure_is_an_error_result_with_no_message_in_it():
    class Broken(FakeDriver):
        def answer(self, query, params):
            raise RuntimeError(f"neo4j said {INJECTION} with {params}")

    box, r, _, _ = setup()
    box = T.Toolbox(Broken(), FakeEmbedder(), QUESTION)
    out = run(box, r, "financial_metrics", {"companies": ["Nvidia"]})
    assert out.ok is False and out.result == {"error": "the tool failed", "type": "RuntimeError"} and out.r == r
    assert CANARY not in json.dumps(out.result) and CANARY not in out.summary


# --- lookup_company ---------------------------------------------------------------------------------------------------------

def test_lookup_company_for_a_filer_reports_the_annual_filing_years_and_changes_nothing():
    box, r, driver, _ = setup()
    out = run(box, r, "lookup_company", {"name": "NVDA"})
    assert out.ok and out.r == r
    assert out.result == {"company": "Nvidia", "sec_filer": True, "annual_filing_fiscal_years": [2023, 2024, 2025, 2026]}
    assert driver.names() == ["annual_pairs"] and driver.params_of("annual_pairs") == [{"ids": [NVDA]}]


def test_lookup_company_for_a_name_without_filings_needs_no_query():
    box, r, driver, _ = setup()
    out = run(box, r, "lookup_company", {"name": "sk hynix"})
    assert out.ok and out.result == {"company": "SK Hynix", "sec_filer": False, "annual_filing_fiscal_years": []}
    assert driver.calls == []


# --- search_filings ---------------------------------------------------------------------------------------------------------

def test_search_filings_embeds_the_planners_query_and_merges_new_chunks_after_the_prefetch():
    box, r, driver, embedder = setup()
    assert len(r["chunks"]) == 8
    out = run(box, r, "search_filings", {"query": "CoWoS packaging capacity", "companies": ["TSMC"], "k": 10})
    assert embedder.queries == ["CoWoS packaging capacity"]
    assert driver.names() == ["excerpts"]
    params = driver.params_of("excerpts")[0]
    assert params["ids"] == [TSMC] and params["k"] == 10 and params["candidates"] == R.EXCERPT_CANDIDATES
    assert out.r["chunks"][:8] == r["chunks"] and len(out.r["chunks"]) == 10        # the fake returns ids 1..10: two are new
    assert out.r["anchors"]["TSMC"] == TSMC
    assert out.result == {"chunks_returned": 10, "chunks_added": 2, "chunks_total": 10, "chunk_cap": 16} and out.ok
    assert out.summary == "search_filings: 2 new excerpts (10 in total)"


def test_search_filings_without_a_company_is_the_unanchored_vector_search():
    box, r, driver, _ = setup()
    out = run(box, r, "search_filings", {"query": "data center power"})
    assert driver.names() == ["vector"] and driver.params_of("vector")[0]["k"] == 6
    assert out.r["anchors"] == r["anchors"] and out.result["chunks_total"] == len(out.r["chunks"])


def test_search_filings_never_exceeds_sixteen_chunks_and_never_duplicates_one():
    box, r, _, _ = setup()
    out = run(box, r, "search_filings", {"query": "again", "companies": ["Nvidia"], "k": 10})
    ids = [c["chunk_id"] for c in out.r["chunks"]]
    assert len(ids) == len(set(ids)) <= 16


# --- financial_metrics ------------------------------------------------------------------------------------------------------

def test_financial_metrics_runs_the_retrievers_metrics_query_and_merges_by_union():
    box, r, driver, _ = setup("How is TSMC doing?")
    out = run(box, r, "financial_metrics", {"companies": ["AMD", "TSMC"], "fiscal_years": [2024], "period_ends": ["2024-12-28"]})
    params = driver.params_of("metrics")[0]
    assert params == {"ids": [AMD, TSMC], "periods": R.METRIC_PERIODS_FETCHED, "years": [2024], "dates": ["2024-12-28"]}
    assert {m["cik"] for m in out.r["metrics"]} >= {AMD, TSMC}
    assert out.r["metric_periods"]["years"] == [2024] and out.r["metric_periods"]["dates"] == ["2024-12-28"]
    assert out.r["anchors"]["AMD"] == AMD and out.r["anchors"]["TSMC"] == TSMC
    names = {c["company"] for c in out.result["companies"]}
    assert names == {"AMD", "TSMC"} and out.ok


def test_financial_metrics_can_filter_to_named_metrics_and_reports_the_ones_it_has():
    box, r, _, _ = setup()
    out = run(box, r, "financial_metrics", {"companies": ["Nvidia"], "metrics": ["net_income"]})
    shown = out.result["companies"][0]["metrics"]
    assert list(shown) == ["net_income"] and out.result["available_metrics"] == ["net_income", "revenue"]
    assert {m["metric"] for m in out.r["metrics"]} == {"net_income", "revenue"}                     # the merge keeps the whole prefetch


def test_facts_fetched_by_year_are_shown_in_the_metrics_block_and_so_citable():
    box, r, _, _ = setup()
    assert "xbrl:1045810:revenue:2023-01-29" not in build_blocks(r)[2]
    out = run(box, r, "financial_metrics", {"companies": ["Nvidia"], "fiscal_years": [2023]})
    assert "xbrl:1045810:revenue:2023-01-29" in build_blocks(out.r)[2]


# --- risk_changes -----------------------------------------------------------------------------------------------------------

def test_risk_changes_without_years_reads_the_current_pair_and_its_passages():
    box, r, driver, _ = setup(question="What is Nvidia's market cap?")
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"]})
    assert driver.names() == ["temporal", "passages"]
    assert driver.params_of("passages")[0]["pairs"] == [{"cik": NVDA, "older": NVDA_ACC["n25"], "newer": NVDA_ACC["n26"]}]
    pair = out.result["comparisons"][0]
    assert pair["company"] == "Nvidia" and pair["compared"] and pair["risk_items"]["removed"] == 1 and pair["risk_items"]["new"] == 1
    assert pair["passages"]["removed"] == 1
    assert out.r["temporal"] and out.r["temporal_passages"] and out.ok


def test_risk_changes_for_named_years_reads_the_pair_between_them():
    box, r, driver, _ = setup()
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"], "fiscal_years": [2024, 2025]})
    assert driver.names() == ["annual_pairs", "temporal_selected", "passages"]
    assert driver.params_of("temporal_selected")[0]["newer_accessions"] == [NVDA_ACC["n25"]]
    pair = out.result["comparisons"][0]
    assert (pair["older_fiscal_year"], pair["newer_fiscal_year"]) == (2024, 2025)
    assert out.r["temporal_pairs"][0]["selection"] == "named" and out.r["temporal_pairs"][0]["newer_accession"] == NVDA_ACC["n25"]


def test_risk_changes_across_recent_reports_reads_the_newest_pairs():
    box, r, driver, _ = setup()
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"], "multi_year": True})
    assert driver.names() == ["annual_pairs", "temporal_selected", "passages"]
    assert len(out.result["comparisons"]) == 2 and out.r["temporal_pairs"][0]["selection"] == "multi"


def test_risk_changes_replaces_the_company_it_returns_and_keeps_the_others():
    box, r, _, _ = setup("Compare the risk changes of Nvidia and AMD.")
    assert {p["cik"] for p in r["temporal_pairs"]} == {NVDA, AMD}
    amd_before = [i for i in r["temporal"] if i["cik"] == AMD]
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"], "fiscal_years": [2024, 2025]})
    assert [i for i in out.r["temporal"] if i["cik"] == AMD] == amd_before
    assert {p["newer_accession"] for p in out.r["temporal_pairs"] if p["cik"] == NVDA} == {NVDA_ACC["n25"]}


# --- relationships / active_risks -------------------------------------------------------------------------------------------

def test_relationships_reads_company_edges_and_rules_and_reports_counts_and_names_only():
    box, r, driver, _ = setup("What is the weather?")
    out = run(box, r, "relationships", {"companies": ["Nvidia"], "hops": 1})
    assert driver.names() == ["company_edges", "rule_edges"]
    assert driver.params_of("rule_edges")[0] == {"ids": [NVDA], "include_neighbours": False, "per_company": R.RULES_PER_COMPANY}
    assert out.result["by_relation"] == {"DEPENDS_ON": 1, "COMPETES_WITH": 1, "AFFECTED_BY": 1}
    assert out.result["companies"] == ["Nvidia", "Samsung", "TSMC"] and out.result["rule_ids"] == ["fr:2026-19537"]
    assert out.result["edges_added"] == 0                     # the prefetch had them (the default anchor is Nvidia)


def test_relationships_for_a_company_the_prefetch_did_not_read_adds_its_edges():
    box, r, _, _ = setup()
    out = run(box, {**r, "edges": [], "anchors": {}}, "relationships", {"companies": ["Nvidia"]})
    assert out.result["edges_added"] == 3 and len(out.r["edges"]) == 3 and out.r["anchors"]["Nvidia"] == NVDA


def test_active_risks_runs_one_search_per_company_with_the_topic_as_the_query():
    box, r, driver, embedder = setup()
    out = run(box, r, "active_risks", {"companies": ["Nvidia", "AMD"], "topic": "export controls"})
    assert embedder.queries == ["export controls"]
    assert [p["cik"] for p in driver.params_of("active_risks")] == [NVDA, AMD]
    assert driver.params_of("active_risks")[0]["candidates"] == R.ACTIVE_RISKS_PER_ANCHOR
    assert out.result["risks_total"] == len(out.r["risks"]) and out.ok
    assert "summary" not in json.dumps(out.result)


def test_active_risks_without_a_topic_uses_the_question():
    box, r, _, embedder = setup()
    run(box, r, "active_risks", {"companies": ["Nvidia"]})
    assert embedder.queries == [QUESTION]


# --- compute_change ---------------------------------------------------------------------------------------------------------

def test_compute_change_through_the_tool_writes_the_line_and_names_the_ids():
    box, r, driver, _ = setup()
    out = run(box, r, "compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2023-01-29",
                                         "to_period_end": "2026-01-25"})
    old, new = NVDA_REVENUE["2023-01-29"], NVDA_REVENUE["2026-01-25"]
    assert out.ok and driver.calls == []                                # pure: no query at all
    assert out.result["change_percent"] == round((new - old) / old * 100, 1)
    assert out.result["line"] == out.r["computed"][0] and out.result["line"].startswith("computed: +700.5%")
    assert out.result["ids"] == ["xbrl:1045810:revenue:2023-01-29", "xbrl:1045810:revenue:2026-01-25"]
    _, _, valid = build_blocks(out.r)
    assert set(out.result["ids"]) <= valid


def test_compute_change_of_a_fact_that_was_never_fetched_is_an_error():
    box, r, _, _ = setup()
    out = run(box, r, "compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2010-01-31",
                                         "to_period_end": "2026-01-25"})
    assert out.ok is False and "financial_metrics" in out.result["error"] and out.r == r and "computed" not in out.r


# --- the whole toolbox against a poisoned graph -----------------------------------------------------------------------------

CALLS = [("lookup_company", {"name": "Nvidia"}), ("search_filings", {"query": "supply", "companies": ["Nvidia"]}),
         ("search_filings", {"query": "supply"}), ("financial_metrics", {"companies": ["Nvidia", "AMD"], "fiscal_years": [2023]}),
         ("risk_changes", {"companies": ["Nvidia"]}), ("risk_changes", {"companies": ["Nvidia"], "fiscal_years": [2024, 2025]}),
         ("relationships", {"companies": ["Nvidia"]}), ("active_risks", {"companies": ["Nvidia"]}),
         ("compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2025-01-26", "to_period_end": "2026-01-25"})]


@pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)])
def test_no_tool_result_or_summary_carries_text_from_the_graph(name, args):
    box, r, driver, _ = setup(driver=poisoned_world())
    out = run(box, r, name, args)
    text = json.dumps([out.result, out.summary, out.tool])
    assert CANARY not in text and "Ignore" not in text and "run_cypher" not in text
    # a fact whose unit is not a currency code is refused (a wrong currency in a computed line would be a false statement)
    assert out.ok is (name != "compute_change"), out.result


def test_the_summary_of_a_step_is_a_short_line_of_counts():
    box, r, _, _ = setup()
    out = run(box, r, "financial_metrics", {"companies": ["Nvidia"]})
    assert out.summary.startswith("financial_metrics") and len(out.summary) <= 200 and "\n" not in out.summary


def test_arguments_may_arrive_already_parsed():
    box, r, _, _ = setup()
    out = box.execute("lookup_company", {"name": "AMD"}, r)
    assert out.ok and out.result["company"] == "AMD"
