"""Review of the M1b retriever: not-compared pairs, passages (graph contract L.7), similarity-first ranking, the
first-sentence lead of paragraph units, and period-aware metrics. Pure: the Cypher is pinned as text and exercised through
a recording driver; tests/integration/test_m1b_retrieval_neo4j.py runs the same queries against a real server."""

import re

import pytest

from semigraph.retrieval import retriever as R
from semigraph.retrieval.retriever import (
    METRIC_PERIODS_FETCHED,
    METRICS_QUERY,
    PASSAGE_CAPS,
    PASSAGES_QUERY,
    TEMPORAL_QUERY,
    hybrid_retrieve,
    mentioned_periods,
    select_passages,
    select_temporal,
)

NVDA, INTC = 1045810, 50863
OLD, NEW = "0001045810-25-000023", "0001045810-26-000021"


class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.driver.calls.append((query, params))
        return self.driver.responses.get(self.driver.classify(query), [])


class Driver:
    def __init__(self, responses=None):
        self.calls, self.responses = [], responses or {}

    def session(self, **kw):
        return _Session(self)

    @staticmethod
    def classify(query):
        return {METRICS_QUERY: "metrics", TEMPORAL_QUERY: "temporal", PASSAGES_QUERY: "passages"}.get(query, "other")

    def of(self, kind):
        return [p for q, p in self.calls if self.classify(q) == kind]


class Embedder:
    def encode_query(self, question):
        return [0.1, 0.2]


def pair_row(company="Nvidia", cik=NVDA, older=OLD, newer=NEW, compared=True, reason=None):
    return {"company": company, "cik": cik, "change": "pair", "item_id": None, "headline": None, "older_headline": None,
            "unit_kind": None, "section_id": None, "seq": None, "length": None, "older_chunk_ids": [],
            "newer_chunk_ids": [], "decided_by": None, "sim_embed": None, "sim_lex": None, "lineage": None,
            "lead_text": None, "older_accession": older, "older_form": "10-K", "older_date": "2025-02-26",
            "newer_accession": newer, "newer_form": "10-K", "newer_date": "2026-02-25", "compared": compared,
            "not_compared_reason": reason}


def item_row(change, n, *, headline="h", kind="headline", length=1000, cik=NVDA, company="Nvidia", lead=None):
    return {**pair_row(company, cik), "change": change, "item_id": f"i{n}",
            "headline": f"headline {n}" if headline == "h" else headline, "unit_kind": kind, "section_id": "I.1A",
            "seq": n, "length": length, "lead_text": lead,
            "older_chunk_ids": [f"{OLD}:I.1A:{n:04d}"] if change in ("removed", "reworded") else [],
            "newer_chunk_ids": [f"{NEW}:I.1A:{n:04d}"] if change in ("new", "reworded") else []}


def passage_row(kind, n, *, text=None, cik=NVDA, item_headline="Export controls", length=None, unit_kind="headline"):
    text = text if text is not None else f"passage text {n} " + "x" * (length or 50)
    return {"cik": cik, "passage_id": f"i1:{kind[0]}{n:03d}", "kind": kind, "item_id": "i1", "item_headline": item_headline,
            "item_unit_kind": unit_kind, "section_id": "I.1A", "lead_text": None, "text": text,
            "counterpart_text": f"newer wording {n}" if kind == "reworded" else None, "similarity": 0.7,
            "chunk_ids": [f"{NEW if kind == 'added' else OLD}:I.1A:{n:04d}"], "counterpart_chunk_ids": []}


# ============================================================ H2: a pair the loader could not compare

def return_columns(member: str) -> list[str]:
    return re.findall(r"\bAS\s+(\w+)", member[member.rindex("RETURN"):])


def test_the_pair_query_binds_the_supersedes_edge_and_returns_its_comparison_flags():
    q = TEMPORAL_QUERY
    assert "-[sup:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)" in q
    assert "coalesce(sup.items_compared, true) AS compared" in q
    assert "sup.not_compared_reason AS not_compared_reason" in q
    columns = [return_columns(m) for m in q.split("UNION ALL")]
    assert all(c == columns[0] for c in columns) and {"compared", "not_compared_reason", "lead_text"} <= set(columns[0])


def test_a_pair_marked_not_compared_survives_the_has_items_guards():
    """A side with no RiskItems (a filing whose section could not be trusted) must not make the pair vanish: it would
    render "(none)" instead of "comparison not available"."""
    q = TEMPORAL_QUERY
    assert q.count("sup.items_compared = false OR (") == q.count("UNION ALL") + 1
    for member in q.split("UNION ALL")[1:]:      # the item members of a not-compared pair contribute no item rows
        assert "coalesce(sup.items_compared, true)" in member.split("RETURN")[0]


def test_select_temporal_keeps_the_comparison_flag_and_reason_and_drops_items_of_a_not_compared_pair():
    rows = [pair_row("Intel", INTC, compared=False, reason="the older filing's section is suspect (coverage 0.62)"),
            item_row("removed", 1, cik=INTC, company="Intel"), pair_row(), item_row("new", 2)]
    items, pairs = select_temporal(rows, "q")
    intel, nvidia = pairs
    assert intel["compared"] is False and intel["not_compared_reason"].startswith("the older filing's section is suspect")
    assert intel["totals"] == {"removed": 0, "new": 0, "reworded": 0}
    assert nvidia["compared"] is True and nvidia["not_compared_reason"] is None and nvidia["totals"]["new"] == 1
    assert [i["item_id"] for i in items] == ["i2"]


def test_a_row_that_carries_no_comparison_flag_means_compared():
    """Rows from an older graph (before the loader stamped the edge) have no flag."""
    row = {k: v for k, v in pair_row().items() if k not in ("compared", "not_compared_reason")}
    _, pairs = select_temporal([row], "q")
    assert pairs[0]["compared"] is True and pairs[0]["not_compared_reason"] is None


# ============================================================ M7: similarity ranks before the length band

def test_when_the_question_overlaps_a_headline_similarity_ranks_before_the_length_band():
    rows = [pair_row(),
            item_row("removed", 1, headline="Short but on topic China", length=200),
            item_row("removed", 2, headline="Medium generic", length=700),
            item_row("removed", 3, headline="Long generic", length=2000),
            item_row("removed", 4, headline="Long on topic China licensing", length=1600)]
    items, _ = select_temporal(rows, "Which China licensing risk was removed?")
    assert [i["item_id"] for i in items] == ["i4", "i1", "i3", "i2"]


def test_with_no_overlap_the_length_band_still_decides():
    rows = [pair_row(), item_row("removed", 1, headline="Short", length=200), item_row("removed", 2, headline="Long", length=2000)]
    assert [i["item_id"] for i in select_temporal(rows, "Anything about Mars?")[0]] == ["i2", "i1"]


# ============================================================ M3: a paragraph unit is labelled by its first sentence

def test_temporal_items_carry_the_lead_text_column():
    items, _ = select_temporal([pair_row(), item_row("new", 1, headline="", kind="paragraph", lead="We depend on TSMC. More.")], "q")
    assert items[0]["lead_text"] == "We depend on TSMC. More."


def test_the_lead_text_is_read_from_the_first_chunk_from_the_items_own_offset():
    q = TEMPORAL_QUERY
    assert "EvidenceSpan {chunk_id: head(i.chunk_ids)}" in q
    assert "i.char_start - lead.char_start" in q                 # section offsets on both: slice from where the unit starts
    assert "i.unit_kind = 'paragraph'" in q                      # headline items do not pay for a text they never print


# ============================================================ passages (graph contract L.7)

def test_the_passages_query_reads_risk_passages_by_filer_and_both_accessions_through_their_item():
    q = PASSAGES_QUERY
    assert "UNWIND $pairs AS pr" in q
    assert "(i:RiskItem)-[:HAS_PASSAGE]->(p:RiskPassage {filer_cik: pr.cik, older_accession: pr.older, newer_accession: pr.newer})" in q
    for column in ("cik", "passage_id", "kind", "item_id", "item_headline", "item_unit_kind", "section_id", "lead_text",
                   "text", "counterpart_text", "similarity", "chunk_ids", "counterpart_chunk_ids"):
        assert f"AS {column}" in q, column
    assert "coalesce(p.chunk_ids, []) AS chunk_ids" in q and "coalesce(p.counterpart_chunk_ids, []) AS counterpart_chunk_ids" in q


def test_the_passage_caps_are_eight_removed_four_added_four_reworded():
    assert PASSAGE_CAPS == {"removed": 8, "added": 4, "reworded": 4}


def test_passages_are_capped_per_company_and_kind_and_the_true_totals_are_kept():
    _, pairs = select_temporal([pair_row()], "q")
    rows = ([passage_row("removed", n) for n in range(12)] + [passage_row("added", n) for n in range(6)]
            + [passage_row("reworded", n) for n in range(5)])
    passages, with_totals = select_passages(rows, pairs, "q")
    by_kind = {k: [p for p in passages if p["kind"] == k] for k in PASSAGE_CAPS}
    assert [len(v) for v in by_kind.values()] == [8, 4, 4]
    assert with_totals[0]["passage_totals"] == {"removed": 12, "added": 6, "reworded": 5}
    assert [p["kind"] for p in passages] == ["removed"] * 8 + ["added"] * 4 + ["reworded"] * 4
    assert "passage_totals" not in pairs[0]                       # the input is not mutated


def test_with_lexical_overlap_the_passages_that_talk_about_the_question_come_first_then_the_longer_ones():
    _, pairs = select_temporal([pair_row()], "q")
    rows = [passage_row("removed", 1, text="Generic long boilerplate sentence about many things. " * 6),
            passage_row("removed", 2, text="The Notified Advanced Computing NAC process has not resulted in approvals."),
            passage_row("removed", 3, text="A medium sentence about NAC.")]
    passages, _ = select_passages(rows, pairs, "What happened to the NAC process approvals?")
    assert [p["passage_id"] for p in passages] == ["i1:r002", "i1:r003", "i1:r001"]


def test_without_overlap_the_longer_passage_comes_first_and_ties_are_deterministic():
    _, pairs = select_temporal([pair_row()], "q")
    rows = [passage_row("removed", 1, text="short one"), passage_row("removed", 3, text="a" * 90), passage_row("removed", 2, text="b" * 90)]
    passages, _ = select_passages(rows, pairs, "Anything about Mars?")
    assert [p["passage_id"] for p in passages] == ["i1:r002", "i1:r003", "i1:r001"]


def test_passages_of_a_not_compared_pair_or_an_unknown_company_are_dropped():
    _, pairs = select_temporal([pair_row("Intel", INTC, compared=False, reason="x"), pair_row()], "q")
    rows = [passage_row("removed", 1, cik=INTC), passage_row("removed", 2), passage_row("removed", 3, cik=999)]
    passages, with_totals = select_passages(rows, pairs, "q")
    assert [p["passage_id"] for p in passages] == ["i1:r002"]
    assert [p["passage_totals"]["removed"] for p in with_totals] == [0, 1]


def test_hybrid_reads_passages_only_for_compared_pairs_and_returns_them_beside_the_items():
    intel = pair_row("Intel", INTC, older="0000050863-25-000010", newer="0000050863-26-000011", compared=False, reason="r")
    d = Driver({"temporal": [pair_row(), intel], "passages": [passage_row("removed", 1)]})
    out = hybrid_retrieve("What changed for Nvidia?", d, Embedder())
    assert d.of("passages") == [{"pairs": [{"cik": NVDA, "older": OLD, "newer": NEW}]}]
    assert [p["passage_id"] for p in out["temporal_passages"]] == ["i1:r001"]
    assert [p["passage_totals"]["removed"] for p in out["temporal_pairs"]] == [1, 0]


def test_without_a_comparable_pair_no_passage_query_is_issued():
    d = Driver({"temporal": [pair_row(compared=False, reason="r")]})
    out = hybrid_retrieve("What changed?", d, Embedder())
    assert d.of("passages") == [] and out["temporal_passages"] == []
    assert hybrid_retrieve("What changed?", Driver(), Embedder())["temporal_passages"] == []
    assert R.vector_retrieve("q", Driver(), Embedder())["temporal_passages"] == []


# ============================================================ period-aware metrics

@pytest.mark.parametrize("question,years,dates", [
    ("What was Nvidia's revenue for the fiscal year ended January 29, 2023?", [], ["2023-01-29"]),
    ("Nvidia revenue in fiscal 2020?", [2020], []),
    ("Compare FY2021 and fiscal year 2019 net income.", [2019, 2021], []),
    ("What was AMD net income in 2019?", [2019], []),
    ("Revenue for Jan. 26, 2020 and 29 January 2023", [], ["2020-01-26", "2023-01-29"]),
    ("Revenue on 2021-01-31?", [], ["2021-01-31"]),
    ("Revenue between 2019 and 2021", [2019, 2020, 2021], []),
    ("Nvidia revenue FY22", [2022], []),
    ("What is Nvidia's revenue?", [], []),
    ("Did revenue exceed 2000 million or reach $2019 million?", [], []),          # amounts are not years
    ("The 2026 filing versus 2025", [2025, 2026], []),
])
def test_mentioned_periods_reads_fiscal_years_dates_and_year_ranges_out_of_the_question(question, years, dates):
    got = mentioned_periods(question)
    assert (got["years"], got["dates"]) == (years, dates)


def test_a_date_is_not_also_read_as_a_year_and_an_impossible_date_is_ignored():
    assert mentioned_periods("revenue for the year ended January 29, 2023 and 2022") == {"years": [2022], "dates": ["2023-01-29"]}
    assert mentioned_periods("on February 31, 2023") == {"years": [], "dates": []}


def test_the_number_of_requested_periods_is_capped():
    got = mentioned_periods("Revenue in " + ", ".join(str(y) for y in range(2005, 2025)))
    assert len(got["years"]) + len(got["dates"]) == R.MAX_MENTIONED_PERIODS


def test_the_metrics_query_takes_the_mentioned_years_and_dates_and_keeps_the_latest_periods():
    q = METRICS_QUERY
    assert "ORDER BY m.period_end DESC LIMIT $periods" in q       # the latest fetched periods stay as they were
    assert "$years" in q and "$dates" in q
    assert "t.period_end.year IN $years" in q and "toString(t.period_end) IN $dates" in q
    assert "duration({days: 380})" in q                            # each mentioned period brings its prior year


def test_hybrid_passes_the_mentioned_periods_and_reports_them_for_the_metrics_block():
    d = Driver()
    out = hybrid_retrieve("What was Nvidia's revenue in fiscal 2020?", d, Embedder())
    assert d.of("metrics") == [{"ids": [NVDA], "periods": METRIC_PERIODS_FETCHED, "years": [2020], "dates": []}]
    assert out["metric_periods"] == {"years": [2020], "dates": []}


def test_a_question_without_a_period_asks_for_none():
    d = Driver()
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", d, Embedder())
    assert d.of("metrics")[0]["years"] == [] and d.of("metrics")[0]["dates"] == [] and out["metric_periods"] == {"years": [], "dates": []}
