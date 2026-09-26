"""M1b retriever: per-metric period selection, external-rule columns, the item-based temporal query and its ranking.

The queries are pinned as text (no Neo4j here); tests/integration/test_m1b_retrieval_neo4j.py runs them against a
real server when RUN_NEO4J_TESTS=1. Row shapes are the contract with the RiskItem loader (docs/v2/M1B_PLAN.md D, G).
"""

import re

import pytest

from semigraph.retrieval import retriever as R
from semigraph.retrieval.retriever import (
    METRIC_PERIODS_FETCHED,
    METRIC_PERIODS_SHOWN,
    METRICS_QUERY,
    RULE_EDGES_QUERY,
    TEMPORAL_CAPS,
    TEMPORAL_QUERY,
    hybrid_retrieve,
    select_temporal,
    vector_retrieve,
)

NVDA, TSMC, AMD = 1045810, 1046179, 2488
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
    """Records (query, params); answers by query kind."""

    def __init__(self, responses=None):
        self.calls, self.responses = [], responses or {}

    def session(self, **kw):
        return _Session(self)

    @staticmethod
    def classify(query):
        table = {METRICS_QUERY: "metrics", TEMPORAL_QUERY: "temporal", RULE_EDGES_QUERY: "rules"}
        return table.get(query, "other")

    def of(self, kind):
        return [p for q, p in self.calls if self.classify(q) == kind]


class Embedder:
    def encode_query(self, question):
        return [0.1, 0.2]


# ---------------------------------------------------------------- metrics: last periods PER metric, keyed by cik

def test_metrics_query_picks_the_last_periods_per_metric_in_a_call_subquery():
    q = METRICS_QUERY
    assert "CALL (c, metric) {" in q                        # the scoped form RULE_EDGES_QUERY proves on this server
    assert "WITH DISTINCT c, seed.metric AS metric" in q    # one subquery run per (company, metric)
    assert "m.metric = metric" in q
    assert "ORDER BY m.period_end DESC LIMIT $periods" in q
    assert "LIMIT 20" not in q                              # the "newest 20 rows across four metrics" bug
    for col in ("c.cik AS cik", "c.name AS company", "m.metric AS metric", "m.value AS value", "m.unit AS unit",
                "AS period_start", "AS period_end"):
        assert col in q


def test_the_period_constants_show_three_fiscal_years_and_fetch_one_more_as_the_year_over_year_base():
    assert METRIC_PERIODS_SHOWN == 3 and METRIC_PERIODS_FETCHED == METRIC_PERIODS_SHOWN + 1


def test_hybrid_asks_the_metrics_query_for_the_fetched_period_count():
    d = Driver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, Embedder())
    # a question that names no fiscal year or date asks for no extra periods (two empty lists), so the rows are unchanged
    assert d.of("metrics") == [{"ids": [NVDA, TSMC], "periods": METRIC_PERIODS_FETCHED, "years": [], "dates": []}]


# ---------------------------------------------------------------- external rules: id, date, provenance

def test_rule_edges_carry_the_document_number_date_url_and_link_provenance_with_honest_defaults():
    q = RULE_EDGES_QUERY
    for col in ("x.rule_id AS rule_id", "toString(x.date) AS date", "x.url AS url", "x.kind AS kind"):
        assert col in q
    # the loader may not have stamped these yet: a missing property means what the current loader does
    assert "coalesce(r.source, 'federal_register') AS link_source" in q
    assert "coalesce(r.link_method, 'keyword') AS link_method" in q
    assert "coalesce(r.external, true) AS external" in q
    assert "*" not in q                                     # still no variable-length path through a rule node


# ---------------------------------------------------------------- temporal: the item-based query

def return_columns(member: str) -> list[str]:
    """The ``AS <name>`` output columns of one UNION member's RETURN clause, in order."""
    ret = member[member.rindex("RETURN"):]
    return re.findall(r"\bAS\s+(\w+)", ret)


def test_temporal_query_reads_the_riskitem_contract_and_none_of_the_old_lineage_status():
    q = TEMPORAL_QUERY
    assert "DISCLOSES_RISK" not in q and "Deleted" not in q and "lineage_id IS NOT NULL" not in q
    assert "c.cik IN $ids" in q
    # the pair: the current annual, and the annual filing it rolled over (not a 10-Q, not an amendment overlay)
    # (the edge is bound as ``sup`` so its ``items_compared`` / ``not_compared_reason`` come back with the pair)
    assert "(cur:Filing {is_current: true})-[sup:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)" in q
    members = q.count("UNION ALL") + 1
    assert q.count("cur.form IN ['10-K', '10-K/A', '20-F', '20-F/A']") == members
    assert q.count("prev.form IN ['10-K', '10-K/A', '20-F', '20-F/A']") == members
    # both ends must actually have items (an amendment overlay or a filing without a risk section has none)
    assert q.count("EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) }") == members
    assert q.count("EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) }") == members
    # removed / new / reworded exactly as the graph contract defines them
    assert "i.removed_in = cur.accession_no" in q
    assert "i.is_new = true" in q
    assert "-[s:SUCCEEDED_BY {kind: 'reworded'}]->" in q
    for change in ("pair", "removed", "new", "reworded"):
        assert f"'{change}' AS change" in q


def test_every_union_member_of_the_temporal_query_returns_the_same_columns_in_the_same_order():
    members = TEMPORAL_QUERY.split("UNION ALL")
    assert len(members) == 4
    columns = [return_columns(m) for m in members]
    assert all(c == columns[0] for c in columns), columns
    for needed in ("company", "cik", "change", "item_id", "headline", "older_headline", "unit_kind", "section_id", "seq",
                   "length", "older_chunk_ids", "newer_chunk_ids", "decided_by", "sim_embed", "sim_lex", "lineage",
                   "older_accession", "older_form", "older_date", "newer_accession", "newer_form", "newer_date"):
        assert needed in columns[0]


def test_the_item_length_is_the_char_span_then_about_a_thousand_characters_per_chunk_then_zero():
    """The RiskItem contract may or may not carry ``char_start``/``char_end``: the length rank must not collapse to 0."""
    assert "coalesce(i.char_end - i.char_start, size(coalesce(i.chunk_ids, [])) * 1000, 0) AS length" in TEMPORAL_QUERY


def test_the_temporal_query_binds_only_the_anchor_ids():
    d = Driver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, Embedder())
    assert d.of("temporal") == [{"ids": [NVDA, TSMC]}]


# ---------------------------------------------------------------- temporal: ranking, caps, totals

def pair(company="Nvidia", cik=NVDA, older=OLD, newer=NEW):
    return {"company": company, "cik": cik, "change": "pair", "item_id": None, "headline": None,
            "older_headline": None, "unit_kind": None, "section_id": None, "seq": None, "length": None,
            "older_chunk_ids": [], "newer_chunk_ids": [], "decided_by": None, "sim_embed": None, "sim_lex": None,
            "lineage": None, "older_accession": older, "older_form": "10-K", "older_date": "2025-02-26",
            "newer_accession": newer, "newer_form": "10-K", "newer_date": "2026-02-25"}


def item(change, n, *, headline="h", kind="headline", length=1000, company="Nvidia", cik=NVDA, older_h=None,
         decided_by=None):
    acc = OLD if change == "removed" else NEW
    return {"company": company, "cik": cik, "change": change, "item_id": f"{acc}:I.1A:{n}",
            "headline": headline if headline != "h" else f"headline {n}", "older_headline": older_h,
            "unit_kind": kind, "section_id": "I.1A", "seq": n, "length": length,
            "older_chunk_ids": [f"{OLD}:I.1A:{n:04d}"] if change in ("removed", "reworded") else [],
            "newer_chunk_ids": [f"{NEW}:I.1A:{n:04d}"] if change in ("new", "reworded") else [],
            "decided_by": decided_by, "sim_embed": None, "sim_lex": None, "lineage": f"{cik}:{n}",
            "older_accession": None, "older_form": None, "older_date": None,
            "newer_accession": None, "newer_form": None, "newer_date": None}


def test_the_caps_are_eight_removed_eight_new_four_reworded():
    assert TEMPORAL_CAPS == {"removed": 8, "new": 8, "reworded": 4}


def test_no_riskitem_data_yields_nothing():
    assert select_temporal([], "How have Nvidia's risk disclosures changed?") == ([], [])


def test_a_pair_with_no_changes_is_kept_so_the_answer_can_say_none_were_found():
    items, pairs = select_temporal([pair()], "q")
    assert items == []
    assert pairs == [{"company": "Nvidia", "cik": NVDA, "older_accession": OLD, "older_form": "10-K",
                      "older_date": "2025-02-26", "newer_accession": NEW, "newer_form": "10-K",
                      "newer_date": "2026-02-25", "compared": True, "not_compared_reason": None,
                      "totals": {"removed": 0, "new": 0, "reworded": 0}}]


def test_lists_are_capped_and_the_true_totals_are_stated():
    rows = ([pair()] + [item("removed", n) for n in range(21)] + [item("new", n) for n in range(12)]
            + [item("reworded", n) for n in range(9)])
    items, pairs = select_temporal(rows, "q")
    by_change = {c: [i for i in items if i["change"] == c] for c in ("removed", "new", "reworded")}
    assert [len(v) for v in by_change.values()] == [8, 8, 4]
    assert pairs[0]["totals"] == {"removed": 21, "new": 12, "reworded": 9}
    assert [i["change"] for i in items] == ["removed"] * 8 + ["new"] * 8 + ["reworded"] * 4   # fixed group order


def test_headline_units_outrank_paragraph_units_even_when_the_paragraph_is_longer_and_more_relevant():
    rows = [pair(),
            item("removed", 1, headline="Unrelated boilerplate risk", kind="headline", length=300),
            item("removed", 2, headline="China licensing risk of export controls", kind="paragraph", length=5000)]
    items, _ = select_temporal(rows, "Did Nvidia drop its China licensing export controls risk?")
    assert [i["seq"] for i in items] == [1, 2]


def test_within_headline_units_the_items_the_question_talks_about_come_first_then_the_length_band():
    """Changed by the review (M7): this used to expect [4, 3, 2, 1] (length band first, the question only breaking ties),
    which answered "which China licensing risk was removed?" with the two longest UNRELATED items ahead of the short
    on-topic one. Similarity now ranks before the band; with no overlap at all the band still decides (next test)."""
    rows = [pair(),
            item("removed", 1, headline="Short but on topic China", length=200),          # band 0
            item("removed", 2, headline="Medium generic", length=700),                     # band 1
            item("removed", 3, headline="Long generic", length=2000),                      # band 2
            item("removed", 4, headline="Long on topic China licensing", length=1600)]     # band 2, relevant
    items, _ = select_temporal(rows, "Which China licensing risk was removed?")
    assert [i["seq"] for i in items] == [4, 1, 3, 2]


def test_ties_are_broken_by_length_then_item_id_so_the_order_is_deterministic():
    rows = [pair(), item("new", 3, length=900), item("new", 1, length=900), item("new", 2, length=950)]
    items, _ = select_temporal(rows, "q")
    assert [i["seq"] for i in items] == [2, 1, 3]


def test_generic_question_words_do_not_count_as_similarity():
    """"risk", "disclosures", "changed" appear in every headline: they must not decide the order."""
    rows = [pair(),
            item("removed", 1, headline="Risk disclosures changed generic", length=900),
            item("removed", 2, headline="Hong Kong transition", length=900)]
    items, _ = select_temporal(rows, "How have the risk disclosures changed since the Hong Kong transition?")
    assert [i["seq"] for i in items] == [2, 1]


def test_each_company_is_capped_and_totalled_separately_and_keeps_its_pair_order():
    rows = ([pair("Nvidia", NVDA)] + [item("removed", n) for n in range(10)]
            + [pair("AMD", AMD)] + [item("removed", n, company="AMD", cik=AMD) for n in range(3)])
    items, pairs = select_temporal(rows, "q")
    assert [p["company"] for p in pairs] == ["Nvidia", "AMD"]
    assert [p["totals"]["removed"] for p in pairs] == [10, 3]
    assert sum(1 for i in items if i["company"] == "Nvidia") == 8 and sum(1 for i in items if i["company"] == "AMD") == 3


def test_item_rows_are_flat_dicts_that_keep_company_and_lineage_for_the_comparison_script():
    """scripts/compare_retrieval.py keys temporal rows by ``company|lineage``."""
    items, _ = select_temporal([pair(), item("reworded", 5, older_h="Old wording", decided_by="rules")], "q")
    (row,) = items
    assert row["company"] == "Nvidia" and row["lineage"] == f"{NVDA}:5"
    assert row["change"] == "reworded" and row["older_headline"] == "Old wording" and row["decided_by"] == "rules"
    assert row["older_chunk_ids"] and row["newer_chunk_ids"]
    assert "older_accession" not in row and "totals" not in row        # pair facts live in the pair rows


# ---------------------------------------------------------------- wiring

def test_hybrid_ranks_with_the_question_and_returns_items_and_pairs():
    rows = [pair(), item("removed", 1, headline="Generic", length=900),
            item("removed", 2, headline="China licensing", length=900)]
    d = Driver({"temporal": rows})
    out = hybrid_retrieve("What China licensing risk did Nvidia remove?", d, Embedder())
    assert [t["seq"] for t in out["temporal"]] == [2, 1]
    assert out["temporal_pairs"][0]["totals"]["removed"] == 2


def test_with_no_riskitem_data_hybrid_reports_empty_temporal_layers():
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", Driver(), Embedder())
    assert out["temporal"] == [] and out["temporal_pairs"] == []


def test_vector_retrieve_reports_empty_temporal_pairs_too():
    out = vector_retrieve("q", Driver(), Embedder())
    assert out["temporal"] == [] and out["temporal_pairs"] == []


def test_select_temporal_is_exported_for_reuse():
    assert R.select_temporal is select_temporal
    with pytest.raises(TypeError):
        select_temporal()          # requires rows and a question
