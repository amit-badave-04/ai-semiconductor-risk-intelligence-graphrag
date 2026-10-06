"""The agent's company fan-out cap (M5a J2): one agent ask never covers more companies than ``retriever.MAX_ANCHORS``.

The plain retrieval keeps the first ``MAX_ANCHORS`` companies a question names. The agent's tools used to add companies
beyond that (4 per call, 4 calls, no cap in ``agent/merge``), which is what made its spend estimate $1.57. Now a tool
that would take the answer past the cap is REFUSED before it runs a query: ``ok=False``, a plain reason the planner can
read, the retrieval dict unchanged. Pinned here: the count is by company (the default Nvidia counts when no company was
detected), the cap is the retriever's own value read at call time, a refused call is a normal ``ok=false`` step event,
the merged context stays under the rendered worst case at the cap, and ``merge.add_anchors`` (the backstop) both
enforces the cap and un-drops a company the retrieval had not covered.
"""

import copy
import json

import pytest
from agent_fakes import (
    AMD,
    NVDA,
    FakeDriver,
    FakeEmbedder,
    FakeWriter,
    ScriptedPlanner,
    make_settings,
    metric_rows,
    passage_row,
    temporal_rows,
    turn,
)

from semigraph.agent import graph as G
from semigraph.agent import merge as M
from semigraph.agent import sanitize as S
from semigraph.agent import stream as agent_stream
from semigraph.agent.state import Limits
from semigraph.agent.tools import Toolbox
from semigraph.retrieval import answerer
from semigraph.retrieval import retriever as R
from semigraph.retrieval.retriever import hybrid_retrieve

CAP = R.MAX_ANCHORS
assert CAP == 4, "the scenarios below are written for a cap of four companies"

Q_ONE = "How much revenue did Nvidia report?"
Q_THREE = "Compare the revenue of Nvidia, AMD and Intel."
Q_FOUR = "Compare the revenue of Nvidia, AMD, Intel and Broadcom."
Q_FIVE = "Compare the revenue of Nvidia, AMD, Intel, Broadcom and Qualcomm."
Q_NONE = "What risks matter most for the AI supply chain this year?"
SERIES = {f"{2025 - y}-01-26": 9e11 - y * 7e10 for y in range(19)}
WIDE = Limits(max_tool_calls=8, max_model_calls=9, time_budget_s=25.0)


def id_of(name: str) -> int:
    return S.entity_id_of(name)


def every_company_world(**override) -> FakeDriver:
    """The stock world, but the metrics layer answers for ANY company (the stock one knows three)."""
    def metrics(params):
        return [row for cik in params["ids"] for row in metric_rows(cik, S.company_for_id(cik), series=SERIES)]

    return FakeDriver.world(**{"metrics": metrics, **override})


def setup(question: str, driver: FakeDriver | None = None):
    driver = driver or every_company_world()
    embedder = FakeEmbedder()
    r = hybrid_retrieve(question, driver, embedder)
    driver.calls.clear()
    embedder.queries.clear()
    return Toolbox(driver, embedder, question), r, driver, embedder


def call(box: Toolbox, r: dict, tool: str, args: dict):
    before = copy.deepcopy(r)
    out = box.execute(tool, json.dumps(args), r)
    assert r == before, f"{tool} mutated the retrieval dict"
    return out


def drain(gen):
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def run(question: str, planner, *, driver: FakeDriver | None = None, limits: Limits = WIDE):
    driver = driver or every_company_world()
    gen = G.run_agent(question, driver, FakeEmbedder(), planner=planner, planner_model="openai/gpt-6-luna",
                      limits=limits)
    events, result = drain(gen)
    assert result.fallback_reason is None
    return events, result, driver


def metrics_of(*companies: str):
    return [("financial_metrics", {"companies": [name]}) for name in companies]


def covered_ids(r: dict) -> set[int]:
    return set(M.covered_companies(r))


def blocks_by_company(r: dict) -> set[int]:
    """Every company that has a metrics row, a temporal pair, an item or a passage in the merged context."""
    rows = [*r["metrics"], *r["temporal_pairs"], *r["temporal"], *r["temporal_passages"]]
    return {row["cik"] for row in rows}


ADDING_CALLS = {
    "search_filings": {"query": "export controls", "companies": ["TSMC"]},
    "financial_metrics": {"companies": ["TSMC"]},
    "risk_changes": {"companies": ["TSMC"]},
    "relationships": {"companies": ["TSMC"]},
    "active_risks": {"companies": ["TSMC"]},
}


# --- the planner adds companies one call at a time -------------------------------------------------------------------

def test_a_planner_adding_one_company_per_call_ends_with_exactly_the_cap_and_each_refusal_is_an_ok_false_step():
    names = ["AMD", "TSMC", "Intel", "Micron", "Broadcom", "Qualcomm", "ASML"]
    planner = ScriptedPlanner(*[turn(c) for c in metrics_of(*names)], turn())
    events, result, driver = run(Q_ONE, planner)
    assert [e["ok"] for e in events] == [True, True, True, False, False, False, False]
    assert list(result.r["anchors"]) == ["Nvidia", "AMD", "TSMC", "Intel"] and len(result.r["anchors"]) == CAP
    assert [e["args"] for e in events[3:]] == [{"companies": [n]} for n in names[3:]]
    assert all("already covers 4 companies" in e["summary"] and e["summary"].startswith("financial_metrics: refused")
               for e in events[3:])
    assert len(driver.params_of("metrics")) == 1 + 3, "a refused call must not run its query"
    assert blocks_by_company(result.r) == {id_of(n) for n in ("Nvidia", "AMD", "TSMC", "Intel")}
    assert [t["ok"] for t in result.tool_calls] == [e["ok"] for e in events]


def test_a_refused_step_has_the_grammar_of_an_accepted_one_and_the_result_is_not_an_exception():
    planner = ScriptedPlanner(turn(*metrics_of("AMD", "TSMC", "Intel", "Micron")), turn())
    events, result, _ = run(Q_ONE, planner)
    assert [e["ok"] for e in events] == [True, True, True, False]
    assert {frozenset(e) for e in events} == {frozenset({"event", "n", "tool", "args", "summary", "ok"})}
    assert [e["n"] for e in events] == [1, 2, 3, 4] and result.stop_reason == "planner_done"
    assert set(result.tool_calls[3]) == {"tool", "args", "ok"}


def test_the_planner_reads_the_refusal_and_the_answer_goes_on_with_what_was_gathered():
    planner = ScriptedPlanner(turn(*metrics_of("AMD", "TSMC", "Intel")), turn(*metrics_of("Micron")), turn())
    events, result, _ = run(Q_ONE, planner)
    assert [e["ok"] for e in events] == [True, True, True, False] and result.stop_reason == "planner_done"
    told = json.loads(planner.calls[2]["messages"][-1]["content"])
    assert told == {"error": "the answer already covers 4 companies and cannot cover more",
                    "covered_companies": ["AMD", "Intel", "Nvidia", "TSMC"], "company_limit": 4, "room": 0}
    assert not any(m["company"] == "Micron" for m in result.r["metrics"])


def test_a_prefetch_that_already_holds_the_cap_refuses_every_addition_whatever_the_tool():
    for tool, args in ADDING_CALLS.items():
        box, r, driver, embedder = setup(Q_FOUR)
        out = call(box, r, tool, args)
        assert out.ok is False and out.r is r, tool
        assert out.summary == f"{tool}: refused, the answer already covers 4 companies", tool
        assert driver.calls == [] and embedder.queries == [], f"{tool} did work before it refused"
        assert out.result["room"] == 0 and out.result["company_limit"] == CAP


def test_a_call_naming_only_companies_the_answer_already_covers_is_not_an_addition():
    box, r, driver, _ = setup(Q_FOUR)
    for tool, args in ADDING_CALLS.items():
        out = call(box, r, tool, {**args, "companies": ["Nvidia", "intel"]})
        assert out.ok is True, tool
        assert list(out.r["anchors"]) == ["Nvidia", "AMD", "Intel", "Broadcom"], tool
    assert driver.calls, "the accepted calls ran their queries"


def test_the_two_tools_that_add_no_company_are_never_refused_for_the_cap():
    box, r, _, _ = setup(Q_FOUR)
    assert call(box, r, "lookup_company", {"name": "TSMC"}).ok is True
    fetched = call(box, r, "financial_metrics", {"companies": ["Nvidia"]})
    change = call(box, fetched.r, "compute_change", {"company": "Nvidia", "metric": "revenue",
                                                    "from_period_end": "2024-01-26", "to_period_end": "2025-01-26"})
    assert fetched.ok is True and change.ok is True and change.r["computed"]


def test_a_call_that_would_pass_the_cap_is_refused_whole_and_names_the_room_left():
    box, r, driver, _ = setup(Q_THREE)
    out = call(box, r, "financial_metrics", {"companies": ["TSMC", "Micron"]})
    assert out.ok is False and out.r is r and driver.calls == []
    assert out.summary == "financial_metrics: refused, it would take the answer past 4 companies"
    assert out.result == {"error": "this call would take the answer past 4 companies; it has room for 1 more",
                          "covered_companies": ["AMD", "Intel", "Nvidia"], "company_limit": 4, "room": 1}
    one = call(box, r, "financial_metrics", {"companies": ["Nvidia", "TSMC"]})      # one of the two is already covered
    assert one.ok is True and list(one.r["anchors"]) == ["Nvidia", "AMD", "Intel", "TSMC"]


def test_a_refusal_names_only_companies_of_the_universe():
    box, r, _, _ = setup(Q_FOUR)
    odd = {**r, "anchors": {**r["anchors"], "Injected <b>name</b>": 99}}
    out = call(box, odd, "financial_metrics", {"companies": ["TSMC"]})
    assert out.ok is False and set(out.result["covered_companies"]) <= set(S.KNOWN_COMPANIES)


# --- what counts as a company ----------------------------------------------------------------------------------------

def test_the_default_company_counts_when_no_company_was_detected():
    box, r, _, _ = setup(Q_NONE)
    assert r["anchors"] == {} and r["anchor_defaulted"] is True and covered_ids(r) == {NVDA}
    planner = ScriptedPlanner(*[turn(c) for c in metrics_of("AMD", "TSMC", "Intel", "Micron")], turn())
    events, result, _ = run(Q_NONE, planner)
    assert [e["ok"] for e in events] == [True, True, True, False]
    assert covered_ids(result.r) == {NVDA, AMD, id_of("TSMC"), id_of("Intel")} and len(covered_ids(result.r)) == CAP
    assert result.r["anchor_defaulted"] is True


def test_the_default_company_is_not_counted_twice_when_the_planner_names_it():
    box, r, _, _ = setup(Q_NONE)
    first = call(box, r, "financial_metrics", {"companies": ["Nvidia"]})
    assert first.ok is True and covered_ids(first.r) == {NVDA}
    more = call(box, first.r, "financial_metrics", {"companies": ["AMD", "TSMC", "Intel"]})
    assert more.ok is True and len(covered_ids(more.r)) == CAP
    assert call(box, more.r, "financial_metrics", {"companies": ["Micron"]}).ok is False


def test_the_cap_is_the_retrievers_value_read_at_call_time(monkeypatch):
    box, r, driver, _ = setup(Q_ONE)
    monkeypatch.setattr(R, "MAX_ANCHORS", 2)
    assert M.company_cap() == 2
    added = call(box, r, "financial_metrics", {"companies": ["AMD"]})
    assert added.ok is True
    refused = call(box, added.r, "financial_metrics", {"companies": ["TSMC"]})
    assert refused.ok is False and refused.result["company_limit"] == 2
    assert "already covers 2 companies" in refused.summary
    monkeypatch.setattr(R, "MAX_ANCHORS", 5)
    assert call(box, added.r, "financial_metrics", {"companies": ["TSMC"]}).ok is True


def test_a_prefetch_over_a_lowered_cap_still_answers_and_only_refuses_additions(monkeypatch):
    box, r, _, _ = setup(Q_FOUR)
    monkeypatch.setattr(R, "MAX_ANCHORS", 2)
    assert call(box, r, "financial_metrics", {"companies": ["Nvidia"]}).ok is True
    assert call(box, r, "financial_metrics", {"companies": ["TSMC"]}).ok is False


# --- the companies the retrieval dropped (gap B) ---------------------------------------------------------------------

def test_a_company_the_retrieval_dropped_cannot_be_added_by_a_tool_so_the_note_stays_true():
    """Dropped companies exist only when the prefetch already holds exactly the cap, and a tool call that adds any
    company is then refused: so through the Toolbox the note ("not covered: Qualcomm") never contradicts the context."""
    box, r, driver, _ = setup(Q_FIVE)
    assert r["anchors_dropped"] == ["Qualcomm"] and len(r["anchors"]) == CAP
    for tool, args in ADDING_CALLS.items():
        out = call(box, r, tool, {**args, "companies": ["Qualcomm"]})
        assert out.ok is False and out.r is r, tool
    assert driver.calls == []
    kept = call(box, r, "financial_metrics", {"companies": ["AMD"]})
    assert kept.ok is True
    assert kept.r["anchors_dropped"] == ["Qualcomm"] and kept.r["temporal_notices"] == r["temporal_notices"]
    assert "not covered: Qualcomm" in kept.r["temporal_notices"][-1]["text"]


def dropped_r(**kw) -> dict:
    """A synthetic retrieval dict with room under the cap and two dropped companies (what a diverging cap gives)."""
    base = {"anchors": {"Nvidia": NVDA, "AMD": AMD}, "anchor_defaulted": False, "metrics": [], "temporal_pairs": [],
            "temporal_notices": [{"cik": AMD, "company": "AMD", "text": "latest comparison shown"},
                                 R._dropped_notice(["Intel", "Micron"])], "anchors_dropped": ["Intel", "Micron"]}
    return {**base, **kw}


def test_adding_a_dropped_company_removes_it_from_the_list_and_rewrites_the_note():
    r = dropped_r()
    before = copy.deepcopy(r)
    out = M.add_anchors(r, {"Intel": id_of("Intel")}, cap=4)
    assert r == before
    assert out["anchors"] == {"Nvidia": NVDA, "AMD": AMD, "Intel": id_of("Intel")}
    assert out["anchors_dropped"] == ["Micron"]
    assert out["temporal_notices"] == [r["temporal_notices"][0], R._dropped_notice(["Micron"])]
    note = out["temporal_notices"][1]["text"]
    assert "Intel" not in note and "not covered: Micron" in note


def test_adding_every_dropped_company_deletes_the_key_and_the_note():
    out = M.add_anchors(dropped_r(), {"Micron": id_of("Micron"), "Intel": id_of("Intel")}, cap=4)
    assert "anchors_dropped" not in out
    assert out["temporal_notices"] == [{"cik": AMD, "company": "AMD", "text": "latest comparison shown"}]


def test_adding_a_company_that_was_not_dropped_leaves_the_list_and_the_note_alone():
    r = dropped_r()
    out = M.add_anchors(r, {"TSMC": id_of("TSMC")}, cap=4)
    assert out["anchors_dropped"] == r["anchors_dropped"] and out["temporal_notices"] == r["temporal_notices"]


def test_the_rewrite_only_touches_the_dropped_note_never_a_notice_a_tool_wrote():
    lookalike = {"cik": None, "company": "this question", "text": "not covered: Intel (a tool wrote this)"}
    r = dropped_r(temporal_notices=[lookalike, R._dropped_notice(["Intel", "Micron"])])
    out = M.add_anchors(r, {"Intel": id_of("Intel")}, cap=4)
    assert out["temporal_notices"] == [lookalike, R._dropped_notice(["Micron"])]


# --- the backstop in merge.add_anchors -------------------------------------------------------------------------------

def test_add_anchors_raises_past_the_cap_and_changes_nothing():
    r = {"anchors": {"Nvidia": NVDA, "AMD": AMD}, "anchor_defaulted": False}
    before = copy.deepcopy(r)
    with pytest.raises(M.CompanyCapError) as caught:
        M.add_anchors(r, {n: id_of(n) for n in ("Intel", "TSMC", "Micron")})
    assert r == before
    assert (caught.value.covered, caught.value.adding, caught.value.cap, caught.value.room) == (
        ["AMD", "Nvidia"], ["Intel", "Micron", "TSMC"], CAP, 2)
    assert len(M.add_anchors(r, {n: id_of(n) for n in ("Intel", "TSMC")})["anchors"]) == CAP


def test_add_anchors_counts_by_company_so_a_company_already_held_is_free():
    r = {"anchors": {n: id_of(n) for n in ("Nvidia", "AMD", "Intel", "Broadcom")}, "anchor_defaulted": False}
    assert M.add_anchors(r, {"AMD": AMD})["anchors"] == r["anchors"]
    assert M.add_anchors({"anchors": {}, "anchor_defaulted": True}, {"Nvidia": NVDA})["anchors"] == {"Nvidia": NVDA}


def test_a_cap_given_by_the_caller_wins_over_the_retrievers():
    r = {"anchors": {"Nvidia": NVDA}, "anchor_defaulted": False}
    with pytest.raises(M.CompanyCapError):
        M.add_anchors(r, {"AMD": AMD}, cap=1)
    assert len(M.add_anchors(r, {n: id_of(n) for n in ("AMD", "Intel", "TSMC", "Micron")}, cap=5)["anchors"]) == 5


# --- the size of what the agent can now build ------------------------------------------------------------------------

def heavy_world() -> FakeDriver:
    """Every company answers with the largest blocks the fakes can build: 19 annual rows of 4 metrics, a temporal layer
    with 8 removed and 8 new 240-character headlines, and all 16 passages."""
    def accession(cik: int, year: int) -> str:
        return f"{cik:010d}-{year}-000001"

    def metrics(params):
        return [row for cik in params["ids"] for metric in ("revenue", "net_income", "rnd", "capex")
                for row in metric_rows(cik, S.company_for_id(cik), metric, SERIES)]

    def temporal(params):
        headlines = tuple(f"{i} " + "H" * 238 for i in range(8))
        return [row for cik in params["ids"] for row in temporal_rows(
            S.company_for_id(cik), cik, accession(cik, 25), accession(cik, 26), removed=headlines, new=headlines)]

    def passages(params):
        kinds = ["removed"] * 8 + ["added"] * 4 + ["reworded"] * 4
        return [passage_row(p["cik"], p["older"], p["newer"], kind, n, text="T" * 600, counterpart_text="C" * 600)
                for p in params["pairs"] for n, kind in enumerate(kinds)]

    return every_company_world(metrics=metrics, temporal=temporal, passages=passages)


def heavy_script() -> ScriptedPlanner:
    years = {"fiscal_years": [2017, 2019, 2021, 2023]}
    return ScriptedPlanner(
        turn(("financial_metrics", {"companies": ["AMD", "Intel", "Broadcom"], **years})),
        turn(("risk_changes", {"companies": ["Nvidia", "AMD", "Intel", "Broadcom"]})),
        turn(("financial_metrics", {"companies": ["Qualcomm", "TSMC", "ASML", "Micron"], **years})),
        turn(("risk_changes", {"companies": ["TSMC", "ASML", "Micron", "Qualcomm"]})),
        turn(("relationships", {"companies": ["Micron"]})), turn())


def merged_chars(question: str) -> tuple[int, dict]:
    _, result, _ = run(question, heavy_script(), driver=heavy_world())
    return len(answerer.render_prompt(question, answerer.build_blocks(result.r)[0])), result.r


def test_the_merged_context_has_at_most_the_cap_companies_and_fits_the_rendered_worst_case_at_the_cap(monkeypatch):
    from test_serve_estimate import rendered_worst_case_chars
    chars, merged = merged_chars(Q_ONE)
    assert len(covered_ids(merged)) == len(blocks_by_company(merged)) == CAP
    assert {c["cik"] for c in merged["metrics"]} == blocks_by_company(merged)
    assert chars <= rendered_worst_case_chars("agent")
    monkeypatch.setattr(R, "MAX_ANCHORS", 13)
    uncapped, wide = merged_chars(Q_ONE)
    assert len(blocks_by_company(wide)) == 8 and uncapped > chars, "the script must exceed the cap without it"


def test_the_whole_agent_stream_carries_the_refused_step_the_dropped_list_and_the_documented_agent_object():
    """End to end through ``agent_answer_stream``: a five-company question and a tool call for a sixth company."""
    planner = ScriptedPlanner(turn(*metrics_of("TSMC")), turn())
    events = list(agent_stream.agent_answer_stream(
        Q_FIVE, every_company_world(), FakeEmbedder(), planner=planner, settings=make_settings(),
        llm_stream=FakeWriter("Nvidia revenue is shown in the context.")))
    assert [e["event"] for e in events] == ["step", "retrieval", "delta", "done"]
    step, retrieval, _, done = events
    assert step["ok"] is False
    assert step["summary"] == "financial_metrics: refused, the answer already covers 4 companies"
    assert list(retrieval) == ["event", "anchors", "counts", "anchor_defaulted", "anchors_dropped"]
    assert list(retrieval["anchors"]) == ["Nvidia", "AMD", "Intel", "Broadcom"]
    assert retrieval["anchors_dropped"] == ["Qualcomm"]
    assert done["agent"]["tool_calls"] == [{"tool": "financial_metrics", "args": {"companies": ["TSMC"]}, "ok": False}]
    assert done["agent"]["fallback_reason"] is None and done["agent"]["stop_reason"] == "planner_done"
    assert set(done["agent"]) == {"tool_calls", "model_calls", "elapsed_s", "fallback_reason", "planner_model",
                                  "planner_usage", "planner_cost_usd", "planner_prompt_version", "stop_reason"}


def test_the_agent_object_and_events_of_a_question_under_the_cap_are_what_they_were():
    """A question that stays under the cap (the full pin is the pre-M5 replay fixtures, test_events_pre_m5_fixtures)."""
    planner = ScriptedPlanner(turn(*metrics_of("AMD", "TSMC")), turn())
    events, result, _ = run(Q_ONE, planner)
    assert [e["ok"] for e in events] == [True, True] and result.stop_reason == "planner_done"
    assert set(result.r) == set(hybrid_retrieve(Q_ONE, every_company_world(), FakeEmbedder()))
    assert "anchors_dropped" not in result.r and list(result.r["anchors"]) == ["Nvidia", "AMD", "TSMC"]
