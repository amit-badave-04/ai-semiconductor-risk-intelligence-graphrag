"""Which filing pair(s) of a company the temporal layer reads (M1b, found by the 2026-09-27 deployed eval).

The graph holds the text comparison of EVERY consecutive annual-filing pair (NVDA: FY2023->24, FY2024->25, FY2025->26) but the
retriever only ever read the pair whose newer filing is current, so "between its FY2024 and FY2025 annual reports" was answered
with the FY2025->26 comparison or with "no comparison available". Selection is by the fiscal years the question names (the
year of a filing's period end, the benchmark's convention) or, for a multi-year question, the newest pairs. A question that
names no pair keeps today's behaviour exactly (see ``test_a_question_that_names_no_pair_keeps_the_current_pair_query``).
"""

import json
from pathlib import Path

import pytest

from semigraph.retrieval import retriever as R
from semigraph.retrieval.retriever import (
    ANNUAL_PAIRS_QUERY,
    PASSAGE_CAPS,
    PASSAGES_QUERY,
    TEMPORAL_CAPS,
    TEMPORAL_QUERY,
    TEMPORAL_SELECTED_QUERY,
    hybrid_retrieve,
    mentioned_periods,
    pair_selection_mode,
    select_pairs,
    select_passages,
    select_temporal,
)

NVDA, MU, INTC = 1045810, 723125, 50863
ACC = {"n23": "0001045810-23-000017", "n24": "0001045810-24-000029", "n25": "0001045810-25-000023",
       "n26": "0001045810-26-000021"}
BENCHMARK = json.loads((Path(__file__).resolve().parents[1] / "artifacts" / "benchmark.json").read_text(encoding="utf-8"))


def annual(older, newer, older_fy, newer_fy, *, company="Nvidia", cik=NVDA, is_current=False, compared=True,
           reason=None, older_has_items=True, newer_has_items=True):
    return {"company": company, "cik": cik, "older_accession": older, "older_form": "10-K", "older_date": f"{older_fy}-02-20",
            "newer_accession": newer, "newer_form": "10-K", "newer_date": f"{newer_fy}-02-20", "compared": compared,
            "not_compared_reason": reason, "is_current": is_current, "older_period_end": f"{older_fy}-01-28",
            "older_fy": older_fy, "newer_period_end": f"{newer_fy}-01-28", "newer_fy": newer_fy,
            "newer_has_items": newer_has_items, "older_has_items": older_has_items}


def nvda_history():
    """Newest first, as ANNUAL_PAIRS_QUERY returns them."""
    return [annual(ACC["n25"], ACC["n26"], 2025, 2026, is_current=True), annual(ACC["n24"], ACC["n25"], 2024, 2025),
            annual(ACC["n23"], ACC["n24"], 2023, 2024)]


def choose(question, rows):
    return select_pairs(rows, question, mentioned_periods(question))


def newer_accessions(pairs):
    return [p["newer_accession"] for p in pairs]


# ---------------------------------------------------------------- which questions ask for a pair selection

@pytest.mark.parametrize("question,mode", [
    ("Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", "named"),
    ("Does NVIDIA's FY2026 10-K still say that it transitioned some operations out of China, or was that statement removed?", "named"),
    ("Did Nvidia stop disclosing any risk factors in its latest 10-K that appeared in earlier 10-Ks?", "multi"),
    ("How has Nvidia's disclosed risk profile evolved across its recent annual reports?", "multi"),
    ("What new supply-chain or geopolitical risks appeared in Meta's most recent annual report?", None),
    ("How does Nvidia depend on TSMC?", None),
    ("By what percentage did Nvidia's revenue change from the fiscal year ended January 26, 2025 to the fiscal year ended "
     "January 25, 2026?", None),                              # names years, but asks about a metric, not a risk disclosure
    ("What was Nvidia's revenue in fiscal 2022?", None),
    ("Compare Nvidia's revenue year over year for the last three years", None),
])
def test_only_questions_about_a_risk_disclosure_change_ask_for_a_pair_selection(question, mode):
    assert pair_selection_mode(question, mentioned_periods(question)) == mode


def test_the_multi_pair_intent_matches_exactly_t1_and_t3_of_the_benchmark_and_named_never_touches_a_metric_question():
    modes = {b["id"]: pair_selection_mode(b["q"], mentioned_periods(b["q"])) for b in BENCHMARK}
    assert {i for i, m in modes.items() if m == "multi"} == {"T1", "T3"}
    named = {i for i, m in modes.items() if m == "named"}
    assert named and all(i.startswith("T") for i in named), named           # only the temporal questions
    assert {b["id"] for b in BENCHMARK if b["type"] == "numeric"}.isdisjoint(named)


# ---------------------------------------------------------------- select_pairs: named years

def test_two_named_fiscal_years_pick_exactly_the_pair_between_them():
    pairs, notices = choose("Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n25"]] and pairs[0]["older_accession"] == ACC["n24"] and notices == []
    assert pairs[0]["selection"] == "named" and (pairs[0]["older_fy"], pairs[0]["newer_fy"]) == (2024, 2025)


def test_the_latest_pair_is_picked_when_the_question_names_its_two_years():
    pairs, notices = choose("Did Nvidia remove any risk factors between its FY2025 and FY2026 annual reports?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n26"]] and notices == [] and pairs[0]["selection"] == "named"


def test_a_year_named_only_for_context_falls_back_to_the_pair_whose_newer_filing_is_that_year():
    """T14: "the 2022 export controls" is not a filing year; the FY2026 10-K names the pair."""
    q = "Does NVIDIA's FY2026 10-K still say it transitioned operations after the 2022 export controls, or was that removed?"
    pairs, notices = choose(q, nvda_history())
    assert newer_accessions(pairs) == [ACC["n26"]] and notices == []


def test_a_single_named_year_picks_the_pair_that_ends_in_that_year():
    pairs, _ = choose("Did Nvidia remove any risk factors in its fiscal 2024 10-K?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n24"]]


def test_a_period_end_date_names_its_fiscal_year():
    q = "Did Nvidia remove any risk factors from the fiscal year ended January 28, 2024 to the fiscal year ended January 26, 2025?"
    pairs, _ = choose(q, nvda_history())
    assert newer_accessions(pairs) == [ACC["n25"]]


def test_years_with_no_pair_in_the_graph_show_the_latest_pair_and_say_so():
    pairs, notices = choose("Did Nvidia remove any risk factors between FY2020 and FY2021?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n26"]] and pairs[0]["selection"] == "latest"
    (notice,) = notices
    assert notice["cik"] == NVDA and notice["company"] == "Nvidia"
    for part in ("no annual-filing comparison covering the fiscal years ending in 2020 and 2021", "Nvidia", "the fiscal years ending in 2023, 2024, 2025, 2026",
                 "latest comparison is shown instead"):
        assert part in notice["text"], part


def test_the_notice_does_not_promise_a_latest_comparison_when_the_current_pair_cannot_be_read():
    """Found by the Neo4j integration review: a Micron-shaped filer whose current pair has no risk items on either side."""
    rows = [annual(ACC["n25"], ACC["n26"], 2025, 2026, is_current=True, older_has_items=False, newer_has_items=False)]
    pairs, notices = choose("Did Nvidia remove any risk factors between FY2020 and FY2021?", rows)
    assert pairs == []
    (notice,) = notices
    assert "no annual-filing comparison covering the fiscal years ending in 2020 and 2021" in notice["text"]
    assert "is shown instead" not in notice["text"] and "no readable comparison of the latest annual filings" in notice["text"]


def test_named_pairs_are_capped_and_returned_oldest_first():
    q = "Which risk factors were removed between 2023 and 2026?"
    pairs, _ = choose(q, nvda_history())
    assert len(pairs) == R.MAX_PAIRS_PER_COMPANY == 2
    assert [p["newer_fy"] for p in pairs] == [2025, 2026]                                   # the two newest, oldest first


def test_a_pair_whose_side_has_no_risk_items_is_returned_as_not_compared_with_the_reason_and_is_not_queried():
    rows = [annual(ACC["n25"], ACC["n26"], 2025, 2026, is_current=True), annual(ACC["n24"], ACC["n25"], 2024, 2025, older_has_items=False)]
    pairs, notices = choose("Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", rows)
    (pair,) = pairs
    assert pair["compared"] is False and pair["queryable"] is False and notices == []
    assert "no risk items were loaded" in pair["not_compared_reason"] and ACC["n24"] in pair["not_compared_reason"]


def test_a_pair_the_loader_marked_not_compared_keeps_its_own_reason():
    rows = [annual("i24", "i25", 2024, 2025, company="Intel", cik=INTC, is_current=True, compared=False,
                   reason="older filing's risk items cover only 75.7% of its risk section text")]
    pairs, _ = choose("Did Intel remove any risk factors between FY2024 and FY2025?", rows)
    assert pairs[0]["compared"] is False and "75.7%" in pairs[0]["not_compared_reason"]


def test_two_companies_are_selected_independently():
    rows = nvda_history() + [annual("m24", "m25", 2024, 2025, company="Micron", cik=MU, is_current=True)]
    pairs, notices = choose("Did Nvidia and Micron remove any risk factors between FY2024 and FY2025?", rows)
    assert sorted((p["cik"], p["newer_accession"]) for p in pairs) == sorted([(NVDA, ACC["n25"]), (MU, "m25")]) and notices == []


def test_a_year_that_is_missing_for_one_company_only_gives_that_company_the_notice():
    rows = nvda_history() + [annual("m21", "m22", 2021, 2022, company="Micron", cik=MU, is_current=True)]
    pairs, notices = choose("Did Nvidia and Micron remove any risk factors between FY2024 and FY2025?", rows)
    assert [n["cik"] for n in notices] == [MU]
    assert sorted(p["cik"] for p in pairs) == sorted([NVDA, MU]) and {p["cik"]: p["selection"] for p in pairs} == {NVDA: "named", MU: "latest"}


def test_a_filing_whose_fiscal_year_is_unknown_is_never_matched_by_year():
    rows = [{**annual(ACC["n24"], ACC["n25"], 2024, 2025), "older_fy": None, "newer_fy": None}, *nvda_history()[:1]]
    pairs, _ = choose("Did Nvidia remove any risk factors between FY2024 and FY2025?", rows)
    assert newer_accessions(pairs) == [ACC["n26"]]                                            # the latest, with a notice


# ---------------------------------------------------------------- select_pairs: several annual reports

def test_a_multi_year_question_gets_the_two_newest_pairs_oldest_first():
    q = "How has Nvidia's disclosed risk profile evolved across its recent annual reports?"
    pairs, notices = choose(q, nvda_history())
    assert newer_accessions(pairs) == [ACC["n25"], ACC["n26"]] and {p["selection"] for p in pairs} == {"multi"}
    (notice,) = notices
    assert notice["cik"] == NVDA and "showing the 2 most recent of 3 annual-filing comparisons for Nvidia" in notice["text"]


def test_a_multi_year_question_with_only_two_pairs_has_no_notice():
    q = "Did Nvidia stop disclosing any risk factors in its latest 10-K that appeared in earlier 10-Ks?"
    pairs, notices = choose(q, nvda_history()[:2])
    assert len(pairs) == 2 and notices == []


def test_multi_year_selection_skips_pairs_that_have_no_items_instead_of_showing_not_compared_ones():
    rows = [annual(ACC["n25"], ACC["n26"], 2025, 2026, is_current=True), annual(ACC["n24"], ACC["n25"], 2024, 2025, older_has_items=False),
            annual(ACC["n23"], ACC["n24"], 2023, 2024)]
    pairs, _ = choose("How has Nvidia's risk profile evolved across its annual reports?", rows)
    assert newer_accessions(pairs) == [ACC["n24"], ACC["n26"]]


# ---------------------------------------------------------------- the queries

def test_the_selected_pair_query_is_the_current_pair_query_with_only_the_pair_match_changed():
    assert "(cur:Filing {is_current: true})-[sup:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)" in TEMPORAL_QUERY
    assert "(cur:Filing {is_current: true})" not in TEMPORAL_SELECTED_QUERY
    assert TEMPORAL_SELECTED_QUERY.count("cur.accession_no IN $newer_accessions") == TEMPORAL_QUERY.count("UNION ALL") + 1
    assert TEMPORAL_QUERY.replace("(cur:Filing {is_current: true})", "(cur:Filing)").count("UNION ALL") == \
        TEMPORAL_SELECTED_QUERY.count("UNION ALL")
    stripped = TEMPORAL_SELECTED_QUERY.replace(" AND cur.accession_no IN $newer_accessions", "")
    assert stripped == TEMPORAL_QUERY.replace("(cur:Filing {is_current: true})", "(cur:Filing)")


def test_the_annual_pairs_query_derives_the_fiscal_year_from_the_filings_own_annual_metric():
    q = ANNUAL_PAIRS_QUERY
    assert "REPORTS_METRIC {accession_no: cur.accession_no}" in q and "REPORTS_METRIC {accession_no: prev.accession_no}" in q
    assert "duration.inDays(m.period_start, m.period_end).days >= 300" in q          # an annual, not a quarterly, period
    assert "cur.is_current AS is_current" in q and "c.cik IN $ids" in q
    assert "AS older_fy" in q and "AS newer_fy" in q and "AS newer_has_items" in q and "AS older_has_items" in q
    assert "is_current: true" not in q                                                # every pair, not only the current one


def test_the_passages_query_returns_the_accessions_of_the_pair_each_passage_belongs_to():
    assert "pr.older AS older_accession" in PASSAGES_QUERY and "pr.newer AS newer_accession" in PASSAGES_QUERY


# ---------------------------------------------------------------- select_temporal / select_passages with two pairs of one company

def pair_row(older, newer, older_fy, newer_fy):
    return {**annual(older, newer, older_fy, newer_fy), "change": "pair"}


def item_row(change, n, newer, older, *, headline="h"):
    return {"company": "Nvidia", "cik": NVDA, "change": change, "item_id": f"{newer}:i{n:03d}", "headline": f"{headline} {n}",
            "older_headline": None, "unit_kind": "headline", "section_id": "I.1A", "seq": n, "length": 1000 + n,
            "older_chunk_ids": [f"{older}:I.1A:{n:04d}"] if change in ("removed", "reworded") else [],
            "newer_chunk_ids": [f"{newer}:I.1A:{n:04d}"] if change in ("new", "reworded") else [],
            "decided_by": None, "sim_embed": None, "sim_lex": None, "lineage": None, "lead_text": None,
            "older_accession": older, "newer_accession": newer, "older_form": "10-K", "older_date": "d", "newer_form": "10-K",
            "newer_date": "d", "compared": True, "not_compared_reason": None}


def two_pair_rows():
    a, b, c = ACC["n24"], ACC["n25"], ACC["n26"]
    rows = [pair_row(a, b, 2024, 2025), pair_row(b, c, 2025, 2026)]
    rows += [item_row("removed", n, b, a) for n in range(1, 7)] + [item_row("removed", n, c, b) for n in range(1, 3)]
    rows += [item_row("new", 1, c, b)]
    return rows


def chosen_two():
    pairs, _ = choose("How has Nvidia's risk profile evolved across its annual reports?", nvda_history())
    return pairs


def test_select_temporal_keeps_each_pairs_items_and_totals_apart_and_halves_the_caps():
    items, pairs = select_temporal(two_pair_rows(), "q", pairs=chosen_two())
    by_newer = {p["newer_accession"]: p for p in pairs}
    assert set(by_newer) == {ACC["n25"], ACC["n26"]}
    assert by_newer[ACC["n25"]]["totals"]["removed"] == 6 and by_newer[ACC["n26"]]["totals"]["removed"] == 2
    assert by_newer[ACC["n26"]]["totals"]["new"] == 1 and by_newer[ACC["n25"]]["totals"]["new"] == 0
    shown = {(i["newer_accession"], i["change"]): 0 for i in items}
    for i in items:
        shown[(i["newer_accession"], i["change"])] += 1
    assert shown[(ACC["n25"], "removed")] == TEMPORAL_CAPS["removed"] // 2 == 4               # 6 found, 4 shown
    assert shown[(ACC["n26"], "removed")] == 2
    assert all(i["older_accession"] and i["newer_accession"] for i in items)


def test_select_temporal_with_one_chosen_pair_is_identical_to_the_default_caps():
    one = [chosen_two()[1]]
    items, pairs = select_temporal(two_pair_rows(), "q", pairs=one)
    assert {i["newer_accession"] for i in items} == {ACC["n26"]} and pairs[0]["totals"]["removed"] == 2
    default_items, default_pairs = select_temporal([pair_row(ACC["n25"], ACC["n26"], 2025, 2026),
                                                    *[r for r in two_pair_rows() if r["newer_accession"] == ACC["n26"]]], "q")
    assert [i["item_id"] for i in items] == [i["item_id"] for i in default_items]


def test_a_chosen_pair_that_cannot_be_compared_keeps_zero_totals_and_no_items_whatever_rows_arrive():
    chosen = [{**chosen_two()[0], "compared": False, "queryable": False, "not_compared_reason": "no risk items were loaded"}]
    items, pairs = select_temporal(two_pair_rows(), "q", pairs=chosen)
    assert items == [] and pairs[0]["compared"] is False and set(pairs[0]["totals"].values()) == {0}


def passage_row(kind, n, older, newer):
    return {"cik": NVDA, "passage_id": f"{newer}:{kind}:{n}", "kind": kind, "item_id": f"i{n}", "item_headline": "h",
            "item_unit_kind": "headline", "section_id": "I.1A", "lead_text": None, "text": f"sentence {n} of {kind}",
            "counterpart_text": None, "similarity": None, "chunk_ids": [f"{older}:I.1A:{n:04d}"], "counterpart_chunk_ids": [],
            "older_accession": older, "newer_accession": newer}


def test_select_passages_groups_by_pair_so_two_pairs_never_share_a_cap_or_a_total():
    _, pairs = select_temporal(two_pair_rows(), "q", pairs=chosen_two())
    a, b, c = ACC["n24"], ACC["n25"], ACC["n26"]
    rows = [passage_row("removed", n, a, b) for n in range(1, 10)] + [passage_row("removed", n, b, c) for n in range(1, 3)]
    passages, with_totals = select_passages(rows, pairs, "q")
    totals = {p["newer_accession"]: p["passage_totals"]["removed"] for p in with_totals}
    assert totals == {b: 9, c: 2}
    per_pair = {}
    for p in passages:
        per_pair[p["newer_accession"]] = per_pair.get(p["newer_accession"], 0) + 1
    assert per_pair == {b: PASSAGE_CAPS["removed"] // 2, c: 2}


def test_a_passage_row_without_pair_accessions_still_belongs_to_the_companys_only_pair():
    _, pairs = select_temporal([pair_row(ACC["n25"], ACC["n26"], 2025, 2026)], "q")
    legacy = {k: v for k, v in passage_row("removed", 1, ACC["n25"], ACC["n26"]).items() if k not in ("older_accession", "newer_accession")}
    passages, _ = select_passages([legacy], pairs, "q")
    assert len(passages) == 1


# ---------------------------------------------------------------- hybrid_retrieve wiring

class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.driver.calls.append((query, params))
        return self.driver.answers.get(query, [])


class Driver:
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def session(self, **kw):
        return _Session(self)

    def params_of(self, query):
        return [p for q, p in self.calls if q == query]


class Embedder:
    def encode_query(self, question):
        return [0.1, 0.2]


def pair_answers():
    return {ANNUAL_PAIRS_QUERY: nvda_history(),
            TEMPORAL_SELECTED_QUERY: [pair_row(ACC["n24"], ACC["n25"], 2024, 2025),
                                      *[r for r in two_pair_rows() if r["newer_accession"] == ACC["n25"]]]}


def test_a_named_pair_question_enumerates_the_pairs_and_queries_only_the_chosen_accession():
    d = Driver(pair_answers())
    r = hybrid_retrieve("Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", d, Embedder())
    assert d.params_of(TEMPORAL_QUERY) == []                                              # the current-pair query is not used
    assert d.params_of(TEMPORAL_SELECTED_QUERY) == [{"ids": [NVDA], "newer_accessions": [ACC["n25"]]}]
    assert [p["newer_accession"] for p in r["temporal_pairs"]] == [ACC["n25"]] and r["temporal_notices"] == []
    assert {i["change"] for i in r["temporal"]} == {"removed"} and r["temporal_pairs"][0]["totals"]["removed"] == 6


def test_a_question_that_names_no_pair_keeps_the_current_pair_query():
    d = Driver({TEMPORAL_QUERY: [pair_row(ACC["n25"], ACC["n26"], 2025, 2026)]})
    r = hybrid_retrieve("How does Nvidia depend on TSMC?", d, Embedder())
    assert d.params_of(ANNUAL_PAIRS_QUERY) == [] and d.params_of(TEMPORAL_SELECTED_QUERY) == []
    assert d.params_of(TEMPORAL_QUERY) == [{"ids": [NVDA, 1046179]}] and r["temporal_notices"] == []
    assert [p["newer_accession"] for p in r["temporal_pairs"]] == [ACC["n26"]]


def test_the_notice_of_an_unmatched_year_reaches_the_result_and_the_latest_pair_is_queried():
    d = Driver({ANNUAL_PAIRS_QUERY: nvda_history(), TEMPORAL_SELECTED_QUERY: [pair_row(ACC["n25"], ACC["n26"], 2025, 2026)]})
    r = hybrid_retrieve("Did Nvidia remove any risk factors between FY2020 and FY2021?", d, Embedder())
    assert d.params_of(TEMPORAL_SELECTED_QUERY) == [{"ids": [NVDA], "newer_accessions": [ACC["n26"]]}]
    assert len(r["temporal_notices"]) == 1 and "no annual-filing comparison covering the fiscal years ending in 2020 and 2021" in r["temporal_notices"][0]["text"]


def test_a_named_pair_that_cannot_be_compared_runs_no_temporal_query_at_all():
    rows = [annual(ACC["n25"], ACC["n26"], 2025, 2026, is_current=True), annual(ACC["n24"], ACC["n25"], 2024, 2025, older_has_items=False)]
    d = Driver({ANNUAL_PAIRS_QUERY: rows})
    r = hybrid_retrieve("Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", d, Embedder())
    assert d.params_of(TEMPORAL_SELECTED_QUERY) == [] and r["temporal"] == []
    assert r["temporal_pairs"][0]["compared"] is False and "no risk items were loaded" in r["temporal_pairs"][0]["not_compared_reason"]


def test_a_multi_year_question_queries_both_pairs_and_the_passages_of_both():
    d = Driver({ANNUAL_PAIRS_QUERY: nvda_history(), TEMPORAL_SELECTED_QUERY: two_pair_rows()})
    hybrid_retrieve("How has Nvidia's disclosed risk profile evolved across its recent annual reports?", d, Embedder())
    assert d.params_of(TEMPORAL_SELECTED_QUERY) == [{"ids": [NVDA], "newer_accessions": [ACC["n25"], ACC["n26"]]}]
    (passage_call,) = d.params_of(PASSAGES_QUERY)
    assert passage_call == {"pairs": [{"cik": NVDA, "older": ACC["n24"], "newer": ACC["n25"]},
                                      {"cik": NVDA, "older": ACC["n25"], "newer": ACC["n26"]}]}


# --- closing review M4: ranges, "since", "last N years", metric questions that only mention a filing, and a notice with no bare fiscal year ---

def longer_history():
    """FY2022 -> FY2026: four pairs, newest first."""
    return [*nvda_history(), annual("0001045810-22-000036", ACC["n23"], 2022, 2023)]


def test_two_named_years_more_than_one_apart_select_every_pair_between_them_up_to_the_cap():
    pairs, notices = choose("Which risk factors did Nvidia remove between FY2023 and FY2025?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n24"], ACC["n25"]] and notices == [] and {p["selection"] for p in pairs} == {"named"}


def test_a_named_range_with_more_pairs_than_the_cap_says_how_many_are_shown():
    pairs, notices = choose("Which risk factors did Nvidia remove between FY2022 and FY2026?", longer_history())
    assert newer_accessions(pairs) == [ACC["n25"], ACC["n26"]]
    (notice,) = notices
    assert "showing the 2 most recent of 4 annual-filing comparisons for Nvidia" in notice["text"]


def test_since_a_year_selects_the_pairs_from_that_year_on_and_says_when_it_shows_fewer():
    pairs, notices = choose("What risks has Nvidia added since 2023?", nvda_history())
    assert newer_accessions(pairs) == [ACC["n25"], ACC["n26"]]
    assert notices and "2 most recent of 3" in notices[0]["text"]


def test_a_year_named_as_a_range_end_that_the_graph_lacks_still_uses_the_years_it_has():
    """T14: "the 2022 export controls" is not a loaded fiscal year, so it does not widen the selection to a range."""
    q = "Does NVIDIA's FY2026 10-K still say it transitioned operations after the 2022 export controls, or was that statement removed?"
    pairs, notices = choose(q, nvda_history())
    assert newer_accessions(pairs) == [ACC["n26"]] and notices == []


@pytest.mark.parametrize("question", [
    "What changed in Nvidia's risk factors in the last 3 years?",
    "Which risk factors did Nvidia add over the past few years?",
])
def test_a_last_n_years_question_about_risk_disclosures_asks_for_the_newest_pairs(question):
    assert pair_selection_mode(question, mentioned_periods(question)) == "multi"
    pairs, _ = choose(question, nvda_history())
    assert len(pairs) == 2 and {p["selection"] for p in pairs} == {"multi"}


@pytest.mark.parametrize("question", [
    "How did Nvidia's revenue change between fiscal 2024 and fiscal 2025 according to its 10-K?",
    "What was Nvidia's net income in fiscal 2024 per its financial statements?",
    "How did gross margin differ between FY2024 and FY2025 filings?",
    "What was Nvidia's revenue over the last 3 years?",
    "How did Nvidia's capital expenditure change in the past two years?",
])
def test_a_metric_question_that_merely_names_a_filing_keeps_the_current_pair(question):
    assert pair_selection_mode(question, mentioned_periods(question)) is None


def test_the_notice_names_no_bare_fiscal_year_because_the_answer_prompt_forbids_that_label():
    _, notices = choose("Did Nvidia remove any risk factors between FY2020 and FY2021?", nvda_history())
    text = notices[0]["text"]
    assert "the fiscal years ending in 2020 and 2021" in text and "annual filings loaded for the fiscal years ending in 2023, 2024, 2025, 2026" in text
    assert "covering fiscal" not in text and "loaded: fiscal" not in text


def test_a_pair_that_cannot_be_compared_does_not_halve_the_caps_of_the_pair_that_can():
    chosen = [{**chosen_two()[0], "compared": False, "queryable": False, "not_compared_reason": "no risk items were loaded"}, chosen_two()[1]]
    items, pairs = select_temporal(two_pair_rows(), "q", pairs=chosen)
    shown = [i for i in items if i["newer_accession"] == ACC["n26"] and i["change"] == "removed"]
    assert len(shown) == 2 and pairs[1]["totals"]["removed"] == 2
    many = [item_row("removed", n, ACC["n26"], ACC["n25"]) for n in range(1, 10)]
    items, _ = select_temporal(many, "q", pairs=chosen)
    assert len([i for i in items if i["change"] == "removed"]) == TEMPORAL_CAPS["removed"]           # 8, not 4


# --- second closing review: the multi-year wording needs a risk-change word AND a disclosure noun, like the named mode ---

@pytest.mark.parametrize("question", [
    "How has TSMC's revenue evolved over the past three years?",
    "How has Nvidia's data center revenue evolved?",
    "What was the change in Intel's net income in fiscal 2024 per its financial statements?",
    "How did operating expenses evolve across Nvidia's annual reports?",
])
def test_a_metric_question_is_never_a_pair_question_even_with_multi_year_or_evolution_wording(question):
    assert pair_selection_mode(question, mentioned_periods(question)) is None


def test_a_disclosure_question_with_evolution_wording_is_still_a_multi_year_question():
    for q in ("How has Nvidia's disclosed risk profile evolved across its recent annual reports?",
              "How have Nvidia's risk factors evolved?"):
        assert pair_selection_mode(q, mentioned_periods(q)) == "multi", q
