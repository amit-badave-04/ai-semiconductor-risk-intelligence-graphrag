"""M1b retrieval queries against a real Neo4j - opt-in (RUN_NEO4J_TESTS=1; on Community also SEMIGRAPH_ALLOW_WIPE=1).

Builds a tiny synthetic graph by plain Cypher (no loaders: the RiskItem loader is a separate piece of work, so this
file pins the GRAPH CONTRACT the retrieval queries are written against) in a throwaway database, then runs the real
queries: TEMPORAL_QUERY (+ select_temporal), METRICS_QUERY, RULE_EDGES_QUERY and the three evidence queries.

Contract (docs/v2/M1B_PLAN.md D and G)
    (:RiskItem {item_id, accession_no, filer_cik, section_id, seq, headline, unit_kind, chunk_ids, is_current, is_new,
                removed_in, lineage_id, char_start, char_end})
    (:RiskItem)-[:SUCCEEDED_BY {kind: 'unchanged'|'reworded'|'merged', sim_embed, sim_lex, decided_by}]->(:RiskItem)
    (newer:Filing)-[:SUPERSEDES {kind: 'rolled'}]->(older:Filing); (:Company)-[:FILED]->(:Filing)

Corpus
    Nvidia  n25 10-K (superseded)  n26 10-K (current)  q25 10-Q (superseded; the current annual also rolled over it)
            older items: o1 removed, o2 removed, o3 unchanged, o4 reworded, o5 merged, o6 removed in an EARLIER pair,
                         qi (an item of the 10-Q, 'removed_in' the current annual - must never be read as a removal)
            newer items: n3 carried, n4 reworded, n5 merged target, n7 NEW (headline), n8 NEW (paragraph, no headline)
    AMD     a25 10-K, a26 10-K (current): one unchanged item each  -> a comparison in which nothing changed
            a26x 10-K/A current, no items (the partial-amendment overlay shape)
    Micron  m26 10-K current rolled over m25 10-K, NEITHER has items -> no comparison at all ("no data")
"""

import pytest

from semigraph.retrieval.retriever import (
    METRIC_PERIODS_FETCHED,
    METRICS_QUERY,
    RULE_EDGES_QUERY,
    TEMPORAL_QUERY,
    run_cypher,
    select_temporal,
)
from semigraph.serve import routes

NVDA, AMD, MICRON = 1045810, 2488, 723125
N25, N26, Q25 = "0001045810-25-000023", "0001045810-26-000021", "0001045810-25-000099"
A25, A26, A26X = "0000002488-25-000010", "0000002488-26-000018", "0000002488-26-000021"
M25, M26 = "0000723125-25-000030", "0000723125-26-000031"
EARLIER = "0001045810-24-000029"


def item(item_id, acc, cik, seq, headline, *, kind="headline", is_new=False, removed_in=None, length=1000, form="10-K",
         current=False):
    return {"item_id": item_id, "accession_no": acc, "filer_cik": cik, "form": form, "section_id": "I.1A", "seq": seq,
            "headline": headline, "text_hash": f"h-{item_id}", "unit_kind": kind,
            "chunk_ids": [f"{acc}:I.1A:{seq:04d}"], "is_current": current, "is_new": is_new,
            "removed_in": removed_in, "lineage_id": f"{cik}:{seq}", "char_start": 0, "char_end": length}


ITEMS = [
    item("o1", N25, NVDA, 1, "China licensing risk", removed_in=N26, length=2000),
    item("o2", N25, NVDA, 2, "Hong Kong transition", removed_in=N26, length=800),
    item("o3", N25, NVDA, 3, "Acquisition risk"),
    item("o4", N25, NVDA, 4, "Old wording of customer concentration"),
    item("o5", N25, NVDA, 5, "Merged risk"),
    item("o6", N25, NVDA, 6, "Removed a year earlier", removed_in=EARLIER),
    item("qi", Q25, NVDA, 7, "Quarterly-only risk", removed_in=N26, form="10-Q"),
    item("n3", N26, NVDA, 3, "Acquisition risk", current=True),
    item("n4", N26, NVDA, 4, "Customer concentration", current=True),
    item("n5", N26, NVDA, 5, "Combined regulatory risk", current=True),
    item("n7", N26, NVDA, 7, "Sovereign AI demand", is_new=True, current=True, length=1800),
    item("n8", N26, NVDA, 8, None, kind="paragraph", is_new=True, current=True, length=300),
    item("a1", A25, AMD, 1, "Competition"),
    item("a2", A26, AMD, 1, "Competition", current=True),
]
SUCCEEDED = [
    {"old": "o3", "new": "n3", "kind": "unchanged", "se": 1.0, "sl": 1.0, "by": "hash"},
    {"old": "o4", "new": "n4", "kind": "reworded", "se": 0.91, "sl": 0.62, "by": "rules"},
    {"old": "o5", "new": "n5", "kind": "merged", "se": 0.8, "sl": 0.5, "by": "luna"},
    {"old": "a1", "new": "a2", "kind": "unchanged", "se": 1.0, "sl": 1.0, "by": "hash"},
]
COMPANIES = [{"cik": NVDA, "name": "Nvidia"}, {"cik": AMD, "name": "AMD"}, {"cik": MICRON, "name": "Micron"}]
FILINGS = [
    (NVDA, N25, "10-K", "2025-02-26", False), (NVDA, N26, "10-K", "2026-02-25", True), (NVDA, Q25, "10-Q", "2025-11-20", False),
    (AMD, A25, "10-K", "2025-02-05", False), (AMD, A26, "10-K", "2026-02-04", True), (AMD, A26X, "10-K/A", "2026-02-04", True),
    (MICRON, M25, "10-K", "2025-10-08", False), (MICRON, M26, "10-K", "2026-10-07", True),
]
SUPERSEDES = [(N26, N25, "rolled"), (N26, Q25, "rolled"), (A26, A25, "rolled"), (M26, M25, "rolled")]
CHUNK = f"{N26}:I.1A:0003"


def metric(metric_name, year, value):
    end = f"{year}-01-26"
    return {"id": f"{NVDA}:{metric_name}:{end}", "metric": metric_name, "value": value, "start": f"{year - 1}-01-27",
            "end": end, "accn": f"0001045810-{year % 100}-000001"}


METRICS = ([metric("revenue", y, 10.0 * (y - 2020)) for y in range(2021, 2027)]         # six fiscal years
           + [metric("rnd", 2026, 9.0), metric("rnd", 2025, 7.0)])
RULES = [
    {"id": "2026-19537", "title": "Implementation of Additional Export Controls", "day": "2026-03-12",
     "url": "https://www.federalregister.gov/d/2026-19537", "kind": "entity_list", "topics": ["china"], "abstract": "abs",
     "props": {"source": "federal_register", "link_method": "keyword", "external": True}},
    {"id": "2026-11111", "title": "An older rule", "day": "2026-01-05", "url": None, "kind": "other", "topics": [],
     "abstract": "abs2", "props": {}},                                      # an edge the loader has not stamped yet
]


def build(driver):
    with driver.session() as s:
        s.run("UNWIND $rows AS r CREATE (:Company {cik: r.cik, name: r.name})", rows=COMPANIES).consume()
        for cik, acc, form, day, current in FILINGS:
            s.run("MATCH (c:Company {cik: $cik}) CREATE (c)-[:FILED {date: date($day)}]->"
                  "(:Filing {accession_no: $acc, form: $form, filing_date: date($day), is_current: $current})",
                  cik=cik, acc=acc, form=form, day=day, current=current).consume()
        for newer, older, kind in SUPERSEDES:
            s.run("MATCH (n:Filing {accession_no: $n}), (o:Filing {accession_no: $o}) "
                  "CREATE (n)-[:SUPERSEDES {kind: $kind}]->(o)", n=newer, o=older, kind=kind).consume()
        s.run("UNWIND $rows AS i CREATE (r:RiskItem) SET r = i", rows=ITEMS).consume()
        s.run("UNWIND $rows AS e MATCH (o:RiskItem {item_id: e.old}), (n:RiskItem {item_id: e.new}) "
              "CREATE (o)-[:SUCCEEDED_BY {kind: e.kind, sim_embed: e.se, sim_lex: e.sl, decided_by: e.by}]->(n)",
              rows=SUCCEEDED).consume()
        s.run("UNWIND $rows AS m MATCH (c:Company {cik: $cik}) "
              "CREATE (c)-[:REPORTS_METRIC {accession_no: m.accn}]->(:Metric {metric_id: m.id, metric: m.metric, "
              "concept: 'Revenues', value: m.value, unit: 'USD', period_start: date(m.start), period_end: date(m.end)})",
              rows=METRICS, cik=NVDA).consume()
        s.run("UNWIND $rows AS r CREATE (:ExportControl {rule_id: r.id, title: r.title, date: date(r.day), url: r.url, "
              "kind: r.kind, topics: r.topics, relevant: true, abstract: r.abstract})", rows=RULES).consume()
        s.run("UNWIND $rows AS r MATCH (c:Company {cik: $cik}), (x:ExportControl {rule_id: r.id}) "
              "CREATE (c)-[a:AFFECTED_BY {status: 'Active', start_date: date(r.day)}]->(x) SET a += r.props",
              rows=RULES, cik=NVDA).consume()
        s.run("MATCH (f:Filing {accession_no: $acc}) CREATE (f)-[:HAS_SECTION]->(sec:FilingSection "
              "{section_key: $acc + ':I.1A', title: 'Item 1A. Risk Factors'}) "
              "CREATE (:EvidenceSpan {chunk_id: $chunk, text: 'Acquisitions may not deliver benefits.', "
              "source_url: 'https://www.sec.gov/x', status: 'current', is_current: true, retrievable: true, "
              "valid_to: date('9999-12-31')})-[:FROM_SECTION]->(sec)", acc=N26, chunk=CHUNK).consume()


@pytest.fixture(scope="module")
def driver(scratch_database):
    with scratch_database("sgm1b") as (drv, _settings):
        build(drv)
        yield drv


def temporal(driver, ciks, question="What changed?"):
    return select_temporal(run_cypher(driver, TEMPORAL_QUERY, ids=ciks), question)


# --------------------------------------------------------------------------- the item-based temporal query

class TestTemporalQuery:
    def test_the_pair_is_the_current_annual_and_the_annual_it_rolled_over_never_the_10q(self, driver):
        _, pairs = temporal(driver, [NVDA])
        (pair,) = pairs
        assert (pair["older_accession"], pair["newer_accession"]) == (N25, N26)
        assert (pair["older_form"], pair["newer_form"]) == ("10-K", "10-K")
        assert (pair["older_date"], pair["newer_date"]) == ("2025-02-26", "2026-02-25")

    def test_removed_new_and_reworded_are_exactly_the_contract_definitions(self, driver):
        items, pairs = temporal(driver, [NVDA])
        by = lambda change: {i["item_id"]: i for i in items if i["change"] == change}   # noqa: E731
        assert set(by("removed")) == {"o1", "o2"}          # not o6 (removed a year earlier), not qi (a 10-Q item)
        assert set(by("new")) == {"n7", "n8"}
        assert set(by("reworded")) == {"n4"}               # 'merged' and 'unchanged' successors are not listed
        assert pairs[0]["totals"] == {"removed": 2, "new": 2, "reworded": 1}

    def test_a_reworded_row_carries_both_headlines_both_chunk_ids_and_the_decider(self, driver):
        items, _ = temporal(driver, [NVDA])
        (row,) = [i for i in items if i["change"] == "reworded"]
        assert row["headline"] == "Customer concentration" and row["older_headline"] == "Old wording of customer concentration"
        assert row["older_chunk_ids"] == [f"{N25}:I.1A:0004"] and row["newer_chunk_ids"] == [f"{N26}:I.1A:0004"]
        assert row["decided_by"] == "rules" and row["sim_embed"] == pytest.approx(0.91)

    def test_removed_items_cite_the_older_filing_and_new_items_the_newer(self, driver):
        items, _ = temporal(driver, [NVDA])
        removed = next(i for i in items if i["item_id"] == "o1")
        new = next(i for i in items if i["item_id"] == "n7")
        assert removed["older_chunk_ids"] == [f"{N25}:I.1A:0001"] and removed["newer_chunk_ids"] == []
        assert new["newer_chunk_ids"] == [f"{N26}:I.1A:0007"] and new["older_chunk_ids"] == []

    def test_a_paragraph_unit_has_no_headline_and_ranks_after_headline_units(self, driver):
        items, _ = temporal(driver, [NVDA])
        new = [i for i in items if i["change"] == "new"]
        assert [i["item_id"] for i in new] == ["n7", "n8"] and new[1]["headline"] is None
        assert new[1]["unit_kind"] == "paragraph"

    def test_removed_items_are_ranked_longer_first_by_the_char_span(self, driver):
        items, _ = temporal(driver, [NVDA])
        assert [i["item_id"] for i in items if i["change"] == "removed"] == ["o1", "o2"]      # 2000 chars then 800

    def test_a_comparison_in_which_nothing_changed_is_still_reported(self, driver):
        items, pairs = temporal(driver, [AMD])
        assert items == [] and pairs[0]["totals"] == {"removed": 0, "new": 0, "reworded": 0}
        assert (pairs[0]["older_accession"], pairs[0]["newer_accession"]) == (A25, A26)   # the 10-K/A overlay is not "current annual"

    def test_a_company_whose_filings_have_no_items_yields_no_comparison(self, driver):
        assert temporal(driver, [MICRON]) == ([], [])

    def test_several_anchors_come_back_together_each_with_its_own_pair(self, driver):
        _, pairs = temporal(driver, [NVDA, AMD, MICRON])
        assert sorted(p["company"] for p in pairs) == ["AMD", "Nvidia"]


# --------------------------------------------------------------------------- metrics: last periods per metric

class TestMetricsQuery:
    def test_each_metric_returns_its_own_last_periods_not_the_newest_rows_across_metrics(self, driver):
        rows = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=METRIC_PERIODS_FETCHED)
        revenue = [r["period_end"] for r in rows if r["metric"] == "revenue"]
        rnd = [r["period_end"] for r in rows if r["metric"] == "rnd"]
        assert revenue == ["2026-01-26", "2025-01-26", "2024-01-26", "2023-01-26"]      # six exist; the last four
        assert rnd == ["2026-01-26", "2025-01-26"]                                       # the older metric is not crowded out

    def test_rows_are_keyed_by_cik_and_carry_unit_and_period(self, driver):
        row = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=1)[0]
        assert row["cik"] == NVDA and row["company"] == "Nvidia" and row["unit"] == "USD"
        assert row["period_start"] == "2025-01-27" and row["period_end"] == "2026-01-26"

    def test_the_period_count_is_a_parameter(self, driver):
        assert len(run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=2)) == 4        # 2 revenue + 2 rnd


# --------------------------------------------------------------------------- external rules

class TestRuleEdges:
    def rows(self, driver):
        return run_cypher(driver, RULE_EDGES_QUERY, ids=[NVDA], include_neighbours=False, per_company=8)

    def test_rules_come_newest_first_with_id_date_and_url(self, driver):
        rows = self.rows(driver)
        assert [r["rule_id"] for r in rows] == ["2026-19537", "2026-11111"]
        assert rows[0]["date"] == "2026-03-12" and rows[0]["url"].endswith("2026-19537") and rows[0]["kind"] == "entity_list"
        assert rows[0]["relation"] == "AFFECTED_BY" and rows[0]["source"] == "Nvidia"

    def test_provenance_is_read_when_the_loader_stamped_it_and_defaults_to_keyword_external_otherwise(self, driver):
        stamped, unstamped = self.rows(driver)
        for row in (stamped, unstamped):
            assert (row["link_source"], row["link_method"], row["external"]) == ("federal_register", "keyword", True)

    def test_the_per_company_cap_applies(self, driver):
        rows = run_cypher(driver, RULE_EDGES_QUERY, ids=[NVDA], include_neighbours=False, per_company=1)
        assert [r["rule_id"] for r in rows] == ["2026-19537"]


# --------------------------------------------------------------------------- the evidence drawer

class TestEvidenceQueries:
    def test_a_chunk_inside_a_risk_item_returns_the_item_headline(self, driver):
        (row,) = run_cypher(driver, routes.EVIDENCE_QUERY, id=CHUNK)
        assert row["item_headlines"] == ["Acquisition risk"] and row["form"] == "10-K" and row["filer"] == "Nvidia"
        assert row["section_title"].startswith("Item 1A") and row["is_current"] is True

    def test_a_chunk_outside_every_item_has_no_headline_and_still_resolves(self, driver):
        with driver.session() as s:
            s.run("MATCH (sec:FilingSection {section_key: $k}) CREATE (:EvidenceSpan {chunk_id: $c, text: 't', "
                  "status: 'current', is_current: true, retrievable: true, valid_to: date('9999-12-31')})-[:FROM_SECTION]->(sec)",
                  k=f"{N26}:I.1A", c=f"{N26}:I.1A:0099").consume()
        (row,) = run_cypher(driver, routes.EVIDENCE_QUERY, id=f"{N26}:I.1A:0099")
        assert row["item_headlines"] == []

    def test_an_xbrl_fact_resolves_to_its_metric_company_and_first_disclosing_accession(self, driver):
        (row,) = run_cypher(driver, routes.XBRL_EVIDENCE_QUERY, id=f"{NVDA}:revenue:2026-01-26")
        assert row["value"] == 60.0 and row["unit"] == "USD" and row["company"] == "Nvidia" and row["cik"] == NVDA
        assert row["accession_no"] == "0001045810-26-000001"          # the edge's accession; that filing is not in this graph
        assert row["form"] is None and row["period_start"] == "2025-01-27"

    def test_an_unknown_xbrl_fact_and_an_unknown_rule_resolve_to_nothing(self, driver):
        assert run_cypher(driver, routes.XBRL_EVIDENCE_QUERY, id=f"{NVDA}:revenue:1999-01-01") == []
        assert run_cypher(driver, routes.FR_EVIDENCE_QUERY, id="1999-00001") == []

    def test_a_federal_register_rule_resolves_to_its_node(self, driver):
        (row,) = run_cypher(driver, routes.FR_EVIDENCE_QUERY, id="2026-19537")
        assert row["document_number"] == "2026-19537" and row["publication_date"] == "2026-03-12"
        assert row["kind"] == "entity_list" and row["topics"] == ["china"] and row["relevant"] is True
        assert row["url"].endswith("2026-19537")
