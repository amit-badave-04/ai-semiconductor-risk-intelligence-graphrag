"""The seven read-only tools: schemas, validation, the queries they run, what they merge and what the planner is told.

Every tool is read-only, parameterised Cypher through the retriever's own constants (NO text-to-Cypher: the fake driver refuses a
query it does not know), its arguments are validated pydantic models, a failure is a ``{"error": ...}`` result instead of an
exception, and what the planner is shown is counts / ids / years / metric values / universe company names only.
"""

import copy
import json
import time

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
    chunk_row,
    metric_rows,
    poisoned_world,
)

from semigraph.agent import tools as T
from semigraph.eval.agent_eval import AGENT_TOOL_UNIVERSE
from semigraph.retrieval import retriever as R
from semigraph.retrieval.fusion import FusionConfig
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
    ("financial_metrics", {"companies": ["Nvidia"], "metrics": ["ignore_previous_instructions"]}),   # not in the real vocabulary
    ("financial_metrics", {"companies": "Nvidia"}),
    ("risk_changes", {"companies": ["Nvidia"], "multi_year": "maybe"}),
    ("relationships", {"companies": ["Nvidia"], "hops": 9}),
    ("relationships", {"companies": ["Nvidia"], "hops": 3}),   # RelationshipsArgs.hops has le=2: 3 must be rejected too
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
        def answer(self, query, params, *, timeout=None):
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
    # R20: k is the asked 10 plus the 8 excerpts already held (the deliberate update of the pinned parameter)
    assert params["ids"] == [TSMC] and params["k"] == 18 and params["candidates"] == R.EXCERPT_CANDIDATES + 8
    # the fake returns ids 1..18 (it honours k): ids 9..18 are the ten new ones, of which 8 fit under the 16-chunk cap
    assert out.r["chunks"][:8] == r["chunks"] and len(out.r["chunks"]) == 16
    assert out.r["anchors"]["TSMC"] == TSMC
    assert out.result == {"chunks_returned": 10, "chunks_added": 8, "chunks_total": 16, "chunk_cap": 16} and out.ok
    assert out.summary == "search_filings: 8 new excerpts (16 in total)"


def test_search_filings_without_a_company_is_the_unanchored_vector_search():
    box, r, driver, _ = setup()
    out = run(box, r, "search_filings", {"query": "data center power"})
    assert driver.names() == ["vector"] and driver.params_of("vector")[0]["k"] == 6 + len(r["chunks"])
    assert out.r["anchors"] == r["anchors"] and out.result["chunks_total"] == len(out.r["chunks"])


def test_search_filings_never_exceeds_sixteen_chunks_and_never_duplicates_one():
    box, r, _, _ = setup()
    out = run(box, r, "search_filings", {"query": "again", "companies": ["Nvidia"], "k": 10})
    ids = [c["chunk_id"] for c in out.r["chunks"]]
    assert len(ids) == len(set(ids)) <= 16


def _rows_after(driver, n_new):
    """A driver whose excerpt query returns the 8 held ids first and then ``n_new`` fresh ones, cut at ``k`` like the real query."""
    held = [chunk_row(n) for n in range(1, 9)]
    fresh = [chunk_row(n) for n in range(100, 100 + n_new)]
    driver.layers["excerpts"] = lambda params: (held + fresh)[: params["k"]]
    driver.layers["vector"] = lambda params: (held + fresh)[: params["k"]]


def test_search_filings_excludes_held_ids():
    box, r, driver, _ = setup()
    held = {c["chunk_id"] for c in r["chunks"]}
    _rows_after(driver, 5)
    out = run(box, r, "search_filings", {"query": "x", "companies": ["Nvidia"], "k": 3})
    new = [c["chunk_id"] for c in out.r["chunks"][len(r["chunks"]):]]
    assert len(new) == 3 and not held & set(new)
    assert out.result["chunks_added"] == 3 and out.result["chunks_returned"] == 3


def test_search_filings_overfetches_by_held_count():
    box, r, driver, _ = setup()
    _rows_after(driver, 5)
    run(box, r, "search_filings", {"query": "x", "companies": ["Nvidia"], "k": 3})
    assert driver.params_of("excerpts")[0]["k"] == 3 + len(r["chunks"])
    # R20: raising $k alone cannot help, the companies branch draws only EXCERPT_CANDIDATES ANN candidates before the filter
    assert driver.params_of("excerpts")[0]["candidates"] == R.EXCERPT_CANDIDATES + len(r["chunks"]) == 60 + len(r["chunks"])
    run(box, r, "search_filings", {"query": "x", "k": 2})
    assert driver.params_of("vector")[0]["k"] == 2 + len(r["chunks"])


def test_search_filings_with_every_hit_already_held_adds_nothing_and_says_so():
    box, r, driver, _ = setup()
    _rows_after(driver, 0)
    out = run(box, r, "search_filings", {"query": "x", "companies": ["Nvidia"], "k": 4})
    assert out.ok and out.r["chunks"] == r["chunks"] and out.result["chunks_added"] == 0


# --- search_filings with RETRIEVAL_FUSION (the agent uses the fused legs) -----------------------------------------------------

class _Runtime:
    """What ``FusionRuntime`` carries (the leg config and the keyword index); the legs themselves are faked below."""
    def __init__(self):
        self.config = FusionConfig(filer=True, period=True, keyword=True, keyword_context=True, rerank="minilm")
        self.index = object()


def test_search_filings_with_fusion_runs_the_fused_legs_on_the_query_and_excludes_held_ids(monkeypatch):
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    runtime = _Runtime()
    r = hybrid_retrieve(QUESTION, driver, embedder)
    held = [c["chunk_id"] for c in r["chunks"]]
    seen = {}

    def fake_fused(drv, question, vec, anchor_ids, **kw):
        seen.update(question=question, vec=vec, anchor_ids=anchor_ids, **kw)
        rows = [chunk_row(n) for n in range(1, 9)] + [chunk_row(n) for n in range(100, 105)]
        return rows[: kw["k_chunks"]], {}, [], None

    monkeypatch.setattr(R, "_fused_excerpts", fake_fused)
    driver.calls.clear()
    box = T.Toolbox(driver, embedder, QUESTION, fusion=runtime)
    out = box.execute("search_filings", json.dumps({"query": "CoWoS capacity", "companies": ["TSMC"], "k": 3}), r)
    assert seen["question"] == "CoWoS capacity" and seen["anchor_ids"] == [TSMC] and seen["cfg"] is runtime.config
    assert seen["index"] is runtime.index and seen["k_chunks"] == 3 + len(held) and seen["defaulted"] is False
    assert driver.names() == []                       # the plain excerpt query did not run
    new = [c["chunk_id"] for c in out.r["chunks"][len(held):]]
    assert len(new) == 3 and not set(held) & set(new) and out.result["chunks_added"] == 3 and len(out.r["chunks"]) <= 16


def test_fused_search_filings_is_refused_when_the_time_budget_is_spent_or_below_the_floor(monkeypatch):
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    r = hybrid_retrieve(QUESTION, driver, embedder)
    monkeypatch.setattr(R, "_fused_excerpts", lambda *a, **k: pytest.fail("no time remained: the fused legs must not run"))
    box = T.Toolbox(driver, embedder, QUESTION, fusion=_Runtime())
    for timeout in (0.0, T.FUSED_MIN_SECONDS / 2):
        out = box.execute("search_filings", json.dumps({"query": "x", "companies": ["TSMC"]}), r, timeout=timeout)
        assert not out.ok and out.r is r and "time" in out.summary


def test_fused_search_filings_runs_when_there_is_no_deadline_or_enough_time(monkeypatch):
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    r = hybrid_retrieve(QUESTION, driver, embedder)
    monkeypatch.setattr(R, "_fused_excerpts", lambda *a, **k: ([chunk_row(100)], {}, [], None))
    box = T.Toolbox(driver, embedder, QUESTION, fusion=_Runtime())
    for timeout in (None, T.FUSED_MIN_SECONDS * 10):
        out = box.execute("search_filings", json.dumps({"query": "x", "companies": ["TSMC"]}), r, timeout=timeout)
        assert out.ok and out.result["chunks_added"] == 1


def test_search_filings_with_fusion_but_no_company_keeps_the_unanchored_vector_search(monkeypatch):
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    r = hybrid_retrieve(QUESTION, driver, embedder)
    monkeypatch.setattr(R, "_fused_excerpts", lambda *a, **k: pytest.fail("fusion needs anchors"))
    driver.calls.clear()
    out = T.Toolbox(driver, embedder, QUESTION, fusion=_Runtime()).execute("search_filings", json.dumps({"query": "power"}), r)
    assert out.ok and driver.names() == ["vector"]


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
    named = next(p for p in out.r["temporal_pairs"] if p["newer_accession"] == NVDA_ACC["n25"])
    assert named["selection"] == "named"


def test_risk_changes_across_recent_reports_reads_the_newest_pairs():
    box, r, driver, _ = setup()
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"], "multi_year": True})
    assert driver.names() == ["annual_pairs", "temporal_selected", "passages"]
    assert len(out.result["comparisons"]) == 2 and out.r["temporal_pairs"][0]["selection"] == "multi"


def test_risk_changes_for_an_older_named_pair_adds_alongside_the_current_pair_and_keeps_other_companies():
    """M3 finding #4: a company whose prefetch already has its CURRENT pair (Nvidia: n25 -> n26), asked about an OLDER
    named fiscal year (n24 -> n25), keeps the current pair's items and passages alongside the newly named one -- it does
    not replace it -- while AMD (a company the call never named) is untouched."""
    box, r, _, _ = setup("Compare the risk changes of Nvidia and AMD.")
    assert {p["cik"] for p in r["temporal_pairs"]} == {NVDA, AMD}
    current_before = [p for p in r["temporal_pairs"] if p["cik"] == NVDA][0]
    assert current_before["newer_accession"] == NVDA_ACC["n26"]
    amd_before = [i for i in r["temporal"] if i["cik"] == AMD]
    out = run(box, r, "risk_changes", {"companies": ["Nvidia"], "fiscal_years": [2024, 2025]})
    assert [i for i in out.r["temporal"] if i["cik"] == AMD] == amd_before                         # AMD untouched
    nvda_pairs = {p["newer_accession"] for p in out.r["temporal_pairs"] if p["cik"] == NVDA}
    assert nvda_pairs == {NVDA_ACC["n26"], NVDA_ACC["n25"]}                                        # UNION, not replace
    current_after = next(p for p in out.r["temporal_pairs"] if p["newer_accession"] == NVDA_ACC["n26"])
    assert current_after["totals"] == current_before["totals"]                                     # true totals untouched
    current_items = [i for i in out.r["temporal"] if i["cik"] == NVDA and i["newer_accession"] == NVDA_ACC["n26"]]
    current_passages = [p for p in out.r["temporal_passages"] if p["cik"] == NVDA and p["newer_accession"] == NVDA_ACC["n26"]]
    assert current_items and current_passages                                                      # left intact, not emptied
    named_items = [i for i in out.r["temporal"] if i["cik"] == NVDA and i["newer_accession"] == NVDA_ACC["n25"]]
    assert named_items                                                                             # the newly named pair is there too
    # the writer can actually use the merged context: both pairs' chunk ids are valid (citable) ids, not just present
    _, _, valid = build_blocks(out.r)
    current_chunk_ids = {cid for i in current_items for cid in [*i["older_chunk_ids"], *i["newer_chunk_ids"]]}
    named_chunk_ids = {cid for i in named_items for cid in [*i["older_chunk_ids"], *i["newer_chunk_ids"]]}
    assert current_chunk_ids and current_chunk_ids <= valid
    assert named_chunk_ids and named_chunk_ids <= valid


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
    assert out.result["line"] == out.r["computed"][-1] and out.result["line"].startswith("computed: +700.5%")
    assert out.r["computed"][0] == "Computed changes:"                  # the one-time header (M3 finding #9)
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


# --- the per-call time budget (M3 finding #3) ---------------------------------------------------------------------------

def test_a_tool_calls_timeout_bounds_every_query_it_makes_together_not_per_query():
    """``active_risks`` over three companies runs one query per company; without a shared budget each one could take its
    own full 12 s, so a hung query holds the whole time budget (and the answer slot) past it. A ``timeout`` given to
    ``execute`` is ONE deadline for the whole call: the total elapsed time stays bounded near it, not the SUM of three
    slow queries, because the second and third queries get whatever time is left of the first's, not a fresh budget."""
    driver = FakeDriver.world(slow={"active_risks": 0.12})
    box = T.Toolbox(driver, FakeEmbedder(), QUESTION)
    r = hybrid_retrieve(QUESTION, FakeDriver.world(), FakeEmbedder())
    args = json.dumps({"companies": ["Nvidia", "AMD", "TSMC"]})

    # unbounded (no timeout, the default -- exactly what a direct call outside the agent loop gets): every query runs its
    # full "slow" duration, so the total grows with the company count.
    started = time.perf_counter()
    unbounded = box.execute("active_risks", args, r)
    unbounded_elapsed = time.perf_counter() - started
    assert unbounded.ok is True and unbounded_elapsed >= 0.3                  # close to 3 x 0.12s = 0.36s

    # bounded: ONE shared deadline for the whole call -- elapsed stays near the 0.2s cap, well under the unbounded run.
    started = time.perf_counter()
    bounded = box.execute("active_risks", args, r, timeout=0.2)
    bounded_elapsed = time.perf_counter() - started
    assert bounded.ok is False and bounded.result["type"] == "TimeoutError"
    assert bounded_elapsed < 0.3


def test_a_non_positive_timeout_refuses_the_call_instead_of_running_with_no_bound():
    """``neo4j.Query(timeout=0)`` means run with NO timeout at all, so a budget that has already reached zero must refuse
    the call outright rather than pass 0 straight through (which would do the opposite of what is intended)."""
    driver = FakeDriver.world(slow={"active_risks": 5.0})
    box = T.Toolbox(driver, FakeEmbedder(), QUESTION)
    r = hybrid_retrieve(QUESTION, FakeDriver.world(), FakeEmbedder())
    started = time.perf_counter()
    out = box.execute("active_risks", json.dumps({"companies": ["Nvidia"]}), r, timeout=0.0)
    elapsed = time.perf_counter() - started
    assert out.ok is False and out.result["type"] == "TimeoutError" and elapsed < 0.05   # refused immediately, never ran


def test_arguments_may_arrive_already_parsed():
    box, r, _, _ = setup()
    out = box.execute("lookup_company", {"name": "AMD"}, r)
    assert out.ok and out.result["company"] == "AMD"


# --- M3 I3: compute_change is one company's own two period ends; refusals are not retried (spec R5, S3) --------------------------

def _with_amd_fact(r):
    return {**r, "metrics": [*r["metrics"], *metric_rows(cik=AMD, company="AMD", series={"2024-12-28": 25785000000.0})]}


def test_compute_change_refuses_two_companies():
    box, r, driver, _ = setup()
    out = run(box, r, "compute_change", {"company": "Nvidia and AMD", "metric": "revenue", "from_period_end": "2025-01-26",
                                         "to_period_end": "2026-01-25"})
    assert out.ok is False and driver.calls == [] and out.r == r
    assert "one company" in out.result["error"] and "own two period ends" in out.result["error"]


@pytest.mark.parametrize("company", ["Nvidia and AMD", "NVDA and AMD", "Nvidia, AMD", "Nvidia vs AMD", "Nvidia/AMD"])
def test_compute_change_refuses_every_two_company_form(company):
    box, r, driver, _ = setup()
    out = run(box, r, "compute_change", {"company": company, "metric": "revenue", "from_period_end": "2025-01-26",
                                         "to_period_end": "2026-01-25"})
    assert out.ok is False and driver.calls == [] and "one company" in out.result["error"]


def test_compute_change_for_a_period_end_the_company_lacks_says_to_fetch_it_never_that_it_is_another_companys():
    # Intel and AMD share a fiscal year end: the right recovery is financial_metrics for this company, not giving up.
    box, r, _, _ = setup()
    r = _with_amd_fact(r)
    out = run(box, r, "compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2024-12-28",
                                         "to_period_end": "2026-01-25"})
    assert out.ok is False and "financial_metrics" in out.result["error"] and "another company" not in out.result["error"]


def test_compute_change_still_refuses_a_never_fetched_fact_with_the_fetch_message():
    box, r, _, _ = setup()
    out = run(box, r, "compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2010-01-31",
                                         "to_period_end": "2026-01-25"})
    assert "financial_metrics" in out.result["error"]


def test_a_refused_call_is_refused_at_once_when_sent_again_with_the_same_arguments():
    box, r, driver, _ = setup()
    args = {"company": "Nvidia and AMD", "metric": "revenue", "from_period_end": "2025-01-26", "to_period_end": "2026-01-25"}
    first = run(box, r, "compute_change", args)
    second = run(box, r, "compute_change", dict(reversed(list(args.items()))))      # same arguments, other key order
    assert first.ok is False and second.ok is False and driver.calls == []
    assert "already refused" in second.result["error"] and "change one argument" in second.result["error"]
    assert "do not resend" in first.result["error"]
    changed = run(box, r, "compute_change", {**args, "company": "Nvidia"})           # one argument changed: judged on its own
    assert "already refused" not in changed.result.get("error", "")


def test_an_invalid_call_sent_twice_is_refused_the_second_time_without_a_query():
    box, r, driver, _ = setup()
    args = {"query": "x" * 301}
    first = run(box, r, "search_filings", args)
    second = run(box, r, "search_filings", args)
    assert first.ok is False and "already refused" not in first.result["error"]
    assert second.ok is False and "already refused" in second.result["error"] and driver.calls == []


def test_a_refusal_that_depends_on_what_was_fetched_is_not_remembered():
    box, r, _, _ = setup()
    args = {"company": "Nvidia", "metric": "revenue", "from_period_end": "2025-01-26", "to_period_end": "2026-01-25"}
    first = run(box, r, "compute_change", {**args, "from_period_end": "2010-01-31"})
    again = run(box, r, "compute_change", {**args, "from_period_end": "2010-01-31"})
    assert first.ok is False and again.ok is False and "already refused" not in again.result["error"]


def test_tool_descriptions_state_the_v2_limits_once_and_stay_small():
    texts = {s["function"]["name"]: " ".join(s["function"]["description"].split()) for s in T.tool_specs()}
    assert "one company" in texts["compute_change"].lower() and "own two period ends" in texts["compute_change"]
    assert "keywords" in texts["search_filings"] and "300" in texts["search_filings"]
    for name in ("financial_metrics", "risk_changes", "relationships", "active_risks"):
        assert "4 companies in all" not in texts[name]          # stated once, in the system prompt


def test_the_argument_schemas_are_unchanged_by_the_description_edits():
    schemas = {s["function"]["name"]: s["function"]["parameters"] for s in T.tool_specs()}
    assert schemas["search_filings"]["properties"]["query"]["maxLength"] == 300
    assert schemas["financial_metrics"]["properties"]["companies"]["maxItems"] == 4
    assert schemas["compute_change"]["additionalProperties"] is False


# --- M3 pre-push security review lows (L2, L3) -------------------------------------------------------------------------------

def test_compute_change_accepts_a_legal_entity_form_that_resolves_to_one_company():
    """L3: "Advanced Micro Devices, Inc." is one company (canonical_name resolves it); the comma must not refuse it."""
    box, r, _, _ = setup()
    r = _with_amd_fact(r)
    out = run(box, r, "compute_change", {"company": "Advanced Micro Devices, Inc.", "metric": "revenue",
                                         "from_period_end": "2024-12-28", "to_period_end": "2025-12-27"})
    assert "one company" not in (out.result.get("error") or "")


def test_execute_never_raises_when_the_repeat_key_cannot_be_built(monkeypatch):
    """L2: the repeat key is built outside the handler's try; a failure there is a refusal, never an exception."""
    box, r, _, _ = setup()
    import semigraph.agent.tools as tools_mod

    def boom(*args, **kwargs):
        raise TypeError("not serialisable")

    monkeypatch.setattr(tools_mod.json, "dumps", boom)
    out = box.execute("lookup_company", {"name": "AMD"}, r)
    assert out.ok is False and out.r == r
