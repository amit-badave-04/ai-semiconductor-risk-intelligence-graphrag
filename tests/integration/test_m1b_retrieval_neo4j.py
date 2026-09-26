"""M1b retrieval queries against a real Neo4j - opt-in (RUN_NEO4J_TESTS=1; on Community also SEMIGRAPH_ALLOW_WIPE=1).

Builds a tiny synthetic graph by plain Cypher (no loaders: the RiskItem loader is a separate piece of work, so this
file pins the GRAPH CONTRACT the retrieval queries are written against) in a throwaway database, then runs the real
queries: TEMPORAL_QUERY (+ select_temporal), METRICS_QUERY, RULE_EDGES_QUERY and the three evidence queries.

Contract (docs/v2/M1B_PLAN.md D and G)
    (:RiskItem {item_id, accession_no, filer_cik, section_id, seq, headline, unit_kind, chunk_ids, is_current, is_new,
                removed_in, unsettled_in, lineage_id, char_start, char_end})
    (:RiskItem)-[:SUCCEEDED_BY {kind: 'unchanged'|'reworded'|'merged', sim_embed, sim_lex, decided_by}]->(:RiskItem)
    (newer:Filing)-[:SUPERSEDES {kind: 'rolled', items_compared, not_compared_reason}]->(older:Filing); (:Company)-[:FILED]->(:Filing)
    (:RiskPassage {passage_id, kind, older_accession, newer_accession, filer_cik, text, counterpart_text, similarity,
                   chunk_ids, counterpart_chunk_ids?})   with (item:RiskItem)-[:HAS_PASSAGE]->(passage)      [L.7]

Corpus
    Nvidia  n25 10-K (superseded)  n26 10-K (current)  q25 10-Q (superseded; the current annual also rolled over it)
            older items: o1 removed, o2 removed, o3 unchanged, o4 reworded, o5 merged, o6 removed in an EARLIER pair,
                         qi (an item of the 10-Q, 'removed_in' the current annual - must never be read as a removal),
                         o7 UNSETTLED (headline), o8 UNSETTLED (paragraph, no headline), o9 unsettled in an EARLIER pair,
                         qu (a 10-Q item whose 'unsettled_in' is the current annual - never read)
            newer items: n3 carried, n4 reworded, n5 merged target, n7 NEW (headline), n8 NEW (paragraph, no headline)
    AMD     a25 10-K, a26 10-K (current): one unchanged item each  -> a comparison in which nothing changed
            a26x 10-K/A current, no items (the partial-amendment overlay shape)
    Micron  m26 10-K current rolled over m25 10-K, NEITHER has items -> no comparison at all ("no data")
    Intel   t25 10-K current rolled over t24 10-K, items_compared=false, t25 has NO items -> "comparison not available"
    ASML    l25 10-K current rolled over l24 10-K, items_compared=false, both sides have items (one with a stale removed_in
            and one with a stale unsettled_in)
    Passages (Nvidia pair): removed x3 + reworded x1 in o4, added x1 in n4, one removed passage of a DIFFERENT pair (EARLIER)
"""

import pytest

from semigraph.retrieval.answerer import build_blocks, metrics_lines, sources_from_context
from semigraph.retrieval.context_layout import removal_supported_ids
from semigraph.retrieval.retriever import (
    METRIC_PERIODS_FETCHED,
    METRICS_QUERY,
    PASSAGES_QUERY,
    RULE_EDGES_QUERY,
    TEMPORAL_QUERY,
    mentioned_periods,
    run_cypher,
    select_passages,
    select_temporal,
)
from semigraph.retrieval.verify import answer_checks
from semigraph.serve import routes

NVDA, AMD, MICRON, INTEL, ASML = 1045810, 2488, 723125, 50863, 937966
N25, N26, Q25 = "0001045810-25-000023", "0001045810-26-000021", "0001045810-25-000099"
A25, A26, A26X = "0000002488-25-000010", "0000002488-26-000018", "0000002488-26-000021"
M25, M26 = "0000723125-25-000030", "0000723125-26-000031"
T24, T25 = "0000050863-25-000010", "0000050863-26-000011"
L24, L25 = "0000937966-25-000010", "0000937966-26-000011"
EARLIER = "0001045810-24-000029"


def item(item_id, acc, cik, seq, headline, *, kind="headline", is_new=False, removed_in=None, unsettled_in=None, length=1000,
         form="10-K", current=False):
    return {"item_id": item_id, "accession_no": acc, "filer_cik": cik, "form": form, "section_id": "I.1A", "seq": seq,
            "headline": headline, "text_hash": f"h-{item_id}", "unit_kind": kind,
            "chunk_ids": [f"{acc}:I.1A:{seq:04d}"], "is_current": current, "is_new": is_new,
            "removed_in": removed_in, "unsettled_in": unsettled_in, "lineage_id": f"{cik}:{seq}", "char_start": 0,
            "char_end": length}


ITEMS = [
    item("o1", N25, NVDA, 1, "China licensing risk", removed_in=N26, length=2000),
    item("o2", N25, NVDA, 2, "Hong Kong transition", removed_in=N26, length=800),
    item("o3", N25, NVDA, 3, "Acquisition risk"),
    item("o4", N25, NVDA, 4, "Old wording of customer concentration"),
    item("o5", N25, NVDA, 5, "Merged risk"),
    item("o6", N25, NVDA, 6, "Removed a year earlier", removed_in=EARLIER),
    item("qi", Q25, NVDA, 7, "Quarterly-only risk", removed_in=N26, form="10-Q"),
    item("o7", N25, NVDA, 10, "Licensing exposure of a customer channel", unsettled_in=N26, length=1200),
    item("o8", N25, NVDA, 12, None, kind="paragraph", unsettled_in=N26, length=400),
    item("o9", N25, NVDA, 13, "Unsettled a year earlier", unsettled_in=EARLIER),
    item("qu", Q25, NVDA, 14, "Quarterly-only unsettled risk", unsettled_in=N26, form="10-Q"),
    item("n3", N26, NVDA, 3, "Acquisition risk", current=True),
    item("n4", N26, NVDA, 4, "Customer concentration", current=True),
    item("n5", N26, NVDA, 5, "Combined regulatory risk", current=True),
    item("n7", N26, NVDA, 7, "Sovereign AI demand", is_new=True, current=True, length=1800),
    {**item("n8", N26, NVDA, 8, None, kind="paragraph", is_new=True, current=True, length=300),
     "chunk_ids": [f"{N26}:I.1A:0008"], "char_start": 507, "char_end": 807},     # = CHUNK8 below
    item("n9", N26, NVDA, 9, None, kind="paragraph", is_new=True, current=True, length=300),      # its first chunk is absent
    item("t1", T24, INTEL, 1, "Older Intel risk", removed_in=T25),          # a stale flag: the pair was not compared
    item("t2", T24, INTEL, 2, "Older Intel unsettled risk", unsettled_in=T25),      # a stale flag: the pair was not compared
    item("l1", L24, ASML, 1, "Older ASML risk", removed_in=L25),            # stale flags on a not-compared pair
    item("l3", L24, ASML, 3, "Older ASML unsettled risk", unsettled_in=L25),
    item("l2", L25, ASML, 2, "Newer ASML risk", is_new=True, current=True),
    item("a1", A25, AMD, 1, "Competition"),
    item("a2", A26, AMD, 1, "Competition", current=True),
]
SUCCEEDED = [
    {"old": "o3", "new": "n3", "kind": "unchanged", "se": 1.0, "sl": 1.0, "by": "hash"},
    {"old": "o4", "new": "n4", "kind": "reworded", "se": 0.91, "sl": 0.62, "by": "rules"},
    {"old": "o5", "new": "n5", "kind": "merged", "se": 0.8, "sl": 0.5, "by": "luna"},
    {"old": "a1", "new": "a2", "kind": "unchanged", "se": 1.0, "sl": 1.0, "by": "hash"},
]
COMPANIES = [{"cik": NVDA, "name": "Nvidia"}, {"cik": AMD, "name": "AMD"}, {"cik": MICRON, "name": "Micron"},
             {"cik": INTEL, "name": "Intel"}, {"cik": ASML, "name": "ASML"}]
FILINGS = [
    (NVDA, N25, "10-K", "2025-02-26", False), (NVDA, N26, "10-K", "2026-02-25", True), (NVDA, Q25, "10-Q", "2025-11-20", False),
    (AMD, A25, "10-K", "2025-02-05", False), (AMD, A26, "10-K", "2026-02-04", True), (AMD, A26X, "10-K/A", "2026-02-04", True),
    (MICRON, M25, "10-K", "2025-10-08", False), (MICRON, M26, "10-K", "2026-10-07", True),
    (INTEL, T24, "10-K", "2025-02-14", False), (INTEL, T25, "10-K", "2026-02-13", True),
    (ASML, L24, "20-F", "2025-02-12", False), (ASML, L25, "20-F", "2026-02-11", True),
]
NOT_COMPARED = {(T25, T24): "the older filing's section is suspect (coverage 0.62)",
                (L25, L24): "the older filing's section text runs into sustainability chapters"}
SUPERSEDES = [(N26, N25, "rolled"), (N26, Q25, "rolled"), (A26, A25, "rolled"), (M26, M25, "rolled"),
              (T25, T24, "rolled"), (L25, L24, "rolled")]
CHUNK = f"{N26}:I.1A:0003"


def metric(metric_name, year, value):
    end = f"{year}-01-26"
    return {"id": f"{NVDA}:{metric_name}:{end}", "metric": metric_name, "value": value, "start": f"{year - 1}-01-27",
            "end": end, "accn": f"0001045810-{year % 100}-000001"}


# The paragraph unit n8 starts 27 characters into its first chunk: the chunk's tail belongs to the previous unit.
CHUNK8 = f"{N26}:I.1A:0008"
CHUNK8_TAIL = "Tail of the previous unit. "
CHUNK8_TEXT = CHUNK8_TAIL + "We depend on TSMC for wafers. A second sentence follows."
PASSAGES = [
    {"passage_id": "o4:r000", "kind": "removed", "item": "o4", "older": N25, "newer": N26, "cik": NVDA,
     "text": "The Notified Advanced Computing, or NAC, process has not resulted in approvals for exports to China.",
     "counterpart": None, "similarity": None, "chunks": [f"{N25}:I.1A:0210", f"{N25}:I.1A:0211"]},
    {"passage_id": "o4:r001", "kind": "removed", "item": "o4", "older": N25, "newer": N26, "cik": NVDA,
     "text": "We transitioned some operations out of China and Hong Kong. " * 12, "counterpart": None, "similarity": None,
     "chunks": [f"{N25}:I.1A:0212"]},
    {"passage_id": "o4:r002", "kind": "removed", "item": "o4", "older": N25, "newer": N26, "cik": NVDA,
     "text": "A short removed sentence.", "counterpart": None, "similarity": None, "chunks": []},
    {"passage_id": "o4:w000", "kind": "reworded", "item": "o4", "older": N25, "newer": N26, "cik": NVDA,
     "text": "We impact revenue.", "counterpart": "We impacted revenue.", "similarity": 0.83,
     "chunks": [f"{N25}:I.1A:0140"], "counterpart_chunks": [f"{N26}:I.1A:0347"]},
    {"passage_id": "n4:a000", "kind": "added", "item": "n4", "older": N25, "newer": N26, "cik": NVDA,
     "text": "In April 2025 the government required licenses for H20.", "counterpart": None, "similarity": None,
     "chunks": [f"{N26}:I.1A:0350"]},
    {"passage_id": "o4:r000@old", "kind": "removed", "item": "o4", "older": EARLIER, "newer": N25, "cik": NVDA,
     "text": "A removed passage of a DIFFERENT pair.", "counterpart": None, "similarity": None, "chunks": [f"{EARLIER}:I.1A:0001"]},
]
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
        for (newer, older), reason in NOT_COMPARED.items():
            s.run("MATCH (n:Filing {accession_no: $n})-[r:SUPERSEDES]->(o:Filing {accession_no: $o}) "
                  "SET r.items_compared = false, r.not_compared_reason = $reason", n=newer, o=older, reason=reason).consume()
        s.run("MATCH (n:Filing {accession_no: $n})-[r:SUPERSEDES]->(o:Filing {accession_no: $o}) SET r.items_compared = true",
              n=N26, o=N25).consume()                                    # stamped explicitly: the same as no flag at all
        s.run("UNWIND $rows AS r MATCH (i:RiskItem {item_id: r.item}) "
              "CREATE (i)-[:HAS_PASSAGE]->(:RiskPassage {passage_id: r.passage_id, kind: r.kind, item_id: r.item, "
              "older_accession: r.older, newer_accession: r.newer, filer_cik: r.cik, text: r.text, "
              "counterpart_text: r.counterpart, similarity: r.similarity, chunk_ids: r.chunks, "
              "counterpart_chunk_ids: r.counterpart_chunks})",
              rows=[{**r, "counterpart_chunks": r.get("counterpart_chunks")} for r in PASSAGES]).consume()
        s.run("CREATE (:EvidenceSpan {chunk_id: $c, text: $t, char_start: 480, char_end: 600, status: 'current', "
              "is_current: true, retrievable: true})", c=CHUNK8, t=CHUNK8_TEXT).consume()
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
        assert set(by("new")) == {"n7", "n8", "n9"}
        assert set(by("reworded")) == {"n4"}               # 'merged' and 'unchanged' successors are not listed
        assert set(by("unsettled")) == {"o7", "o8"}        # not o9 (a year earlier), not qu (a 10-Q item); never in 'removed'
        assert pairs[0]["totals"] == {"removed": 2, "unsettled": 2, "new": 3, "reworded": 1}

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
        assert [i["item_id"] for i in new] == ["n7", "n8", "n9"] and new[1]["headline"] is None
        assert new[1]["unit_kind"] == "paragraph"

    def test_removed_items_are_ranked_longer_first_by_the_char_span(self, driver):
        items, _ = temporal(driver, [NVDA])
        assert [i["item_id"] for i in items if i["change"] == "removed"] == ["o1", "o2"]      # 2000 chars then 800

    def test_a_comparison_in_which_nothing_changed_is_still_reported(self, driver):
        items, pairs = temporal(driver, [AMD])
        assert items == [] and pairs[0]["totals"] == {"removed": 0, "unsettled": 0, "new": 0, "reworded": 0}
        assert (pairs[0]["older_accession"], pairs[0]["newer_accession"]) == (A25, A26)   # the 10-K/A overlay is not "current annual"

    def test_a_company_whose_filings_have_no_items_yields_no_comparison(self, driver):
        assert temporal(driver, [MICRON]) == ([], [])

    def test_several_anchors_come_back_together_each_with_its_own_pair(self, driver):
        _, pairs = temporal(driver, [NVDA, AMD, MICRON])
        assert sorted(p["company"] for p in pairs) == ["AMD", "Nvidia"]


# --------------------------------------------------------------------------- metrics: last periods per metric

class TestMetricsQuery:
    def test_each_metric_returns_its_own_last_periods_not_the_newest_rows_across_metrics(self, driver):
        rows = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=METRIC_PERIODS_FETCHED, years=[], dates=[])
        revenue = [r["period_end"] for r in rows if r["metric"] == "revenue"]
        rnd = [r["period_end"] for r in rows if r["metric"] == "rnd"]
        assert revenue == ["2026-01-26", "2025-01-26", "2024-01-26", "2023-01-26"]      # six exist; the last four
        assert rnd == ["2026-01-26", "2025-01-26"]                                       # the older metric is not crowded out

    def test_rows_are_keyed_by_cik_and_carry_unit_and_period(self, driver):
        row = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=1, years=[], dates=[])[0]
        assert row["cik"] == NVDA and row["company"] == "Nvidia" and row["unit"] == "USD"
        assert row["period_start"] == "2025-01-27" and row["period_end"] == "2026-01-26"

    def test_the_period_count_is_a_parameter(self, driver):
        assert len(run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=2, years=[], dates=[])) == 4        # 2 revenue + 2 rnd


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


# --------------------------------------------------------------------------- pairs the loader could not compare

class TestNotComparedPairs:
    def test_a_pair_marked_not_compared_comes_back_even_when_one_side_has_no_items(self, driver):
        """Intel: t25 has NO RiskItems. The has-items guards used to drop the pair, so the block read "(none)"."""
        items, pairs = temporal(driver, [INTEL])
        (pair,) = pairs
        assert pair["compared"] is False and pair["not_compared_reason"] == NOT_COMPARED[(T25, T24)]
        assert (pair["older_accession"], pair["newer_accession"]) == (T24, T25)
        assert items == [] and pair["totals"] == {"removed": 0, "unsettled": 0, "new": 0, "reworded": 0}     # t1 / t2: stale flags, not read

    def test_no_item_row_is_read_for_a_not_compared_pair_whose_items_carry_stale_flags(self, driver):
        items, pairs = temporal(driver, [ASML])
        assert pairs[0]["compared"] is False and items == []           # l1 'removed_in', l3 'unsettled_in' and l2 'is_new' are ignored

    def test_an_explicit_items_compared_true_and_no_flag_at_all_both_mean_compared(self, driver):
        (nvidia,), (amd,) = temporal(driver, [NVDA])[1], temporal(driver, [AMD])[1]
        assert nvidia["compared"] is True and nvidia["not_compared_reason"] is None    # stamped true
        assert amd["compared"] is True and amd["not_compared_reason"] is None          # no flag on the edge

    def test_the_block_says_comparison_not_available_with_the_reason_and_nothing_else(self, driver):
        items, pairs = temporal(driver, [INTEL, ASML])
        block = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": items,
                              "temporal_pairs": pairs})[0].temporal_block
        assert block.count("comparison not available (") == 2
        assert NOT_COMPARED[(T25, T24)] in block and NOT_COMPARED[(L25, L24)] in block
        assert "none found" not in block and "Removed" not in block and "Added" not in block and "Not matched" not in block


# --------------------------------------------------------------------------- unsettled items (older items the text check could not settle)

UNSETTLED_HEADING = ("Not matched (the text check could not verify whether these older risk factors still appear; they may have "
                     "been removed or absorbed into another risk factor) - showing 2 of 2:")


class TestUnsettledItems:
    def rows(self, driver):
        return {r["item_id"]: r for r in run_cypher(driver, TEMPORAL_QUERY, ids=[NVDA]) if r["change"] == "unsettled"}

    def test_exactly_the_older_items_whose_unsettled_in_names_the_current_annual_come_back(self, driver):
        assert set(self.rows(driver)) == {"o7", "o8"}      # not o9 (a year earlier), not qu (a 10-Q item), not the removed items

    def test_an_unsettled_row_has_the_removed_column_shape_and_cites_the_older_filing(self, driver):
        rows = self.rows(driver)
        row = rows["o7"]
        assert row["headline"] == "Licensing exposure of a customer channel" and row["unit_kind"] == "headline"
        assert row["older_chunk_ids"] == [f"{N25}:I.1A:0010"] and row["newer_chunk_ids"] == []
        assert row["length"] == 1200 and row["older_headline"] is None and row["decided_by"] is None
        assert (row["older_accession"], row["newer_accession"], row["compared"]) == (N25, N26, True)
        assert rows["o8"]["headline"] is None and rows["o8"]["unit_kind"] == "paragraph"

    def test_the_totals_state_the_unsettled_count_apart_from_the_removed_count(self, driver):
        items, pairs = temporal(driver, [NVDA])
        assert pairs[0]["totals"]["unsettled"] == 2 and pairs[0]["totals"]["removed"] == 2
        assert [i["item_id"] for i in items if i["change"] == "unsettled"] == ["o7", "o8"]     # headline unit first, the paragraph last
        assert not {i["item_id"] for i in items if i["change"] == "removed"} & {"o7", "o8"}

    def test_a_pair_that_was_not_compared_reads_no_unsettled_row_whatever_its_items_carry(self, driver):
        for cik in (INTEL, ASML):
            items, pairs = temporal(driver, [cik])
            assert items == [] and pairs[0]["totals"]["unsettled"] == 0 and pairs[0]["compared"] is False

    def test_the_block_lists_them_after_removed_and_before_added_and_they_never_support_a_removal_claim(self, driver):
        items, pairs = temporal(driver, [NVDA])
        blocks, context, valid = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [],
                                               "temporal": items, "temporal_pairs": pairs})
        lines = blocks.temporal_block.splitlines()
        at = lines.index(UNSETTLED_HEADING)
        assert lines[at + 1:at + 3] == [f'- "Licensing exposure of a customer channel" [{N25}:I.1A:0010]',
                                        f'- "(untitled paragraph, section I.1A)" [{N25}:I.1A:0012]']
        # (headings re-worded in the M1b hedging pass: "Removed - showing" -> "No longer appears as a separate ...",
        #  "Added - showing" -> "No matching ... found in the earlier filing")
        assert next(i for i, ln in enumerate(lines) if ln.startswith("No longer appears as a separate ")) < at
        assert at < next(i for i, ln in enumerate(lines) if ln.startswith("No matching ") and " found in the earlier filing - showing" in ln)
        assert {f"{N25}:I.1A:0010", f"{N25}:I.1A:0012"} <= valid
        supported = removal_supported_ids(context)
        assert {f"{N25}:I.1A:0001", f"{N25}:I.1A:0002"} <= supported
        assert not {f"{N25}:I.1A:0010", f"{N25}:I.1A:0012"} & supported
        text = f"The licensing exposure risk factor was removed [{N25}:I.1A:0010]."
        checks = answer_checks(text, {f"{N25}:I.1A:0010"}, valid, context, sources=sources_from_context(context))
        assert len(checks.removal_claims) == 1


# --------------------------------------------------------------------------- passages (contract L.7)

PAIR = [{"cik": NVDA, "older": N25, "newer": N26}]


class TestPassagesQuery:
    def rows(self, driver, pairs=PAIR):
        return run_cypher(driver, PASSAGES_QUERY, pairs=pairs)

    def test_only_the_passages_of_the_requested_pair_are_read(self, driver):
        ids = [r["passage_id"] for r in self.rows(driver)]
        assert ids == ["n4:a000", "o4:r000", "o4:r001", "o4:r002", "o4:w000"]        # not the EARLIER pair's passage

    def test_a_pair_with_no_passages_or_a_not_compared_pair_returns_nothing(self, driver):
        assert self.rows(driver, [{"cik": AMD, "older": A25, "newer": A26}]) == []
        assert self.rows(driver, [{"cik": INTEL, "older": T24, "newer": T25}]) == []
        assert self.rows(driver, []) == []

    def test_each_row_carries_its_item_the_kind_the_text_and_the_chunk_ids_of_the_filing_it_is_quoted_from(self, driver):
        by = {r["passage_id"]: r for r in self.rows(driver)}
        removed, added = by["o4:r000"], by["n4:a000"]
        assert removed["kind"] == "removed" and removed["item_id"] == "o4" and removed["cik"] == NVDA
        assert removed["item_headline"] == "Old wording of customer concentration" and removed["item_unit_kind"] == "headline"
        assert removed["chunk_ids"] == [f"{N25}:I.1A:0210", f"{N25}:I.1A:0211"] and removed["section_id"] == "I.1A"
        assert added["kind"] == "added" and added["item_id"] == "n4" and added["item_headline"] == "Customer concentration"
        assert added["chunk_ids"] == [f"{N26}:I.1A:0350"]                              # the NEWER filing's chunk
        assert by["o4:r002"]["chunk_ids"] == [] and removed["counterpart_chunk_ids"] == []

    def test_a_reworded_passage_carries_the_counterpart_text_and_its_optional_chunk_ids(self, driver):
        reworded = {r["passage_id"]: r for r in self.rows(driver)}["o4:w000"]
        assert reworded["text"] == "We impact revenue." and reworded["counterpart_text"] == "We impacted revenue."
        assert reworded["similarity"] == pytest.approx(0.83) and reworded["counterpart_chunk_ids"] == [f"{N26}:I.1A:0347"]

    def test_ranked_capped_and_totalled_they_reach_the_answer_context_and_its_removal_check(self, driver):
        items, pairs = temporal(driver, [NVDA], "What happened to the NAC process approvals for China?")
        passages, pairs = select_passages(run_cypher(driver, PASSAGES_QUERY, pairs=PAIR), pairs, "What happened to the NAC process approvals for China?")
        assert pairs[0]["passage_totals"] == {"removed": 3, "added": 1, "reworded": 1}
        assert [p["passage_id"] for p in passages if p["kind"] == "removed"][0] == "o4:r000"       # overlaps the question
        blocks, context, valid = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [],
                                               "temporal": items, "temporal_pairs": pairs, "temporal_passages": passages})
        block = blocks.temporal_block
        hedge = "a differently worded version of the same statement may exist"
        assert f"Passages of surviving risk factors whose wording was not found in the newer filing (showing 3 of 3; {hedge}):" in block
        assert f"Passages of surviving risk factors whose wording was not found in the older filing (showing 1 of 1; {hedge}):" in block
        assert "Passages of surviving risk factors that were reworded (showing 1 of 1):" in block
        assert (f'- in "Old wording of customer concentration": "The Notified Advanced Computing, or NAC, process has not '
                f'resulted in approvals for exports to China." [{N25}:I.1A:0210] [{N25}:I.1A:0211]') in block
        assert f'later wording: "We impacted revenue." [{N26}:I.1A:0347]' in block
        # the removal check reads the same block back: removed ITEMS and removed PASSAGES, nothing else
        supported = removal_supported_ids(context)
        assert {f"{N25}:I.1A:0001", f"{N25}:I.1A:0002", f"{N25}:I.1A:0210", f"{N25}:I.1A:0211", f"{N25}:I.1A:0212"} <= supported
        assert f"{N26}:I.1A:0350" not in supported and f"{N25}:I.1A:0140" not in supported and f"{N26}:I.1A:0347" not in supported
        assert f"{N25}:I.1A:0010" in valid and f"{N25}:I.1A:0010" not in supported      # an unsettled item is citable, never a removal
        assert {f"{N25}:I.1A:0210", f"{N26}:I.1A:0350"} <= valid


# --------------------------------------------------------------------------- M3: a paragraph unit's first sentence

class TestParagraphLead:
    def rows(self, driver):
        items, _ = temporal(driver, [NVDA])
        return {i["item_id"]: i for i in items}

    def test_the_first_sentence_of_a_paragraph_unit_starts_where_the_unit_starts_not_where_its_chunk_starts(self, driver):
        lead = self.rows(driver)["n8"]["lead_text"]
        assert lead.startswith("We depend on TSMC for wafers.") and not lead.startswith("Tail of the previous unit")

    def test_a_unit_whose_first_chunk_is_unknown_and_a_headline_item_have_no_lead_text(self, driver):
        rows = self.rows(driver)
        assert rows["n9"]["lead_text"] is None and rows["n7"]["lead_text"] is None and rows["n7"]["headline"] == "Sovereign AI demand"

    def test_the_block_labels_the_paragraph_unit_by_its_first_sentence_and_says_paragraphs(self, driver):
        items, pairs = temporal(driver, [NVDA])
        block = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": items,
                              "temporal_pairs": pairs})[0].temporal_block
        assert f'- "We depend on TSMC for wafers." [{N26}:I.1A:0008]' in block
        assert f'- "(untitled paragraph, section I.1A)" [{N26}:I.1A:0009]' in block
        assert ("No matching risk factor or paragraph found in the earlier filing - showing 3 of 3 risk factors and paragraphs "
                "(new, or a restructured older risk factor or paragraph):") in block


# --------------------------------------------------------------------------- period-aware metrics

class TestPeriodAwareMetrics:
    def periods(self, driver, **named):
        rows = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=METRIC_PERIODS_FETCHED,
                          **{"years": [], "dates": [], **named})
        return [r["period_end"] for r in rows if r["metric"] == "revenue"], [r["period_end"] for r in rows if r["metric"] == "rnd"]

    def test_a_named_year_brings_that_year_and_the_one_before_it_besides_the_latest_periods(self, driver):
        revenue, rnd = self.periods(driver, years=[2022])
        assert revenue == ["2026-01-26", "2025-01-26", "2024-01-26", "2023-01-26", "2022-01-26", "2021-01-26"]
        assert rnd == ["2026-01-26", "2025-01-26"]                               # rnd has no 2022: nothing extra

    def test_the_oldest_year_returns_itself_and_has_no_prior_year_to_return(self, driver):
        revenue, _ = self.periods(driver, years=[2021])
        assert revenue == ["2026-01-26", "2025-01-26", "2024-01-26", "2023-01-26", "2021-01-26"]

    def test_nvidias_fiscal_2020_style_naming_matches_the_year_of_the_period_end(self, driver):
        """Fiscal 2022 ends 2022-01-26: the year of the period END, never the year the period started."""
        assert "2022-01-26" in self.periods(driver, years=mentioned_periods("What was revenue in fiscal 2022?")["years"])[0]

    def test_a_named_date_is_the_same_as_the_year_it_ends_in_and_naming_both_adds_no_duplicates(self, driver):
        by_date = self.periods(driver, dates=["2022-01-26"])[0]
        assert by_date == self.periods(driver, years=[2022])[0] == self.periods(driver, years=[2022], dates=["2022-01-26"])[0]

    def test_a_question_with_no_period_or_a_period_with_no_data_changes_nothing(self, driver):
        latest = self.periods(driver)[0]
        assert latest == ["2026-01-26", "2025-01-26", "2024-01-26", "2023-01-26"]
        assert self.periods(driver, years=[1999])[0] == latest and self.periods(driver, years=[2026])[0] == latest

    def test_the_block_shows_the_named_year_with_its_computed_change_against_the_year_before(self, driver):
        rows = run_cypher(driver, METRICS_QUERY, ids=[NVDA], periods=METRIC_PERIODS_FETCHED, years=[2022], dates=[])
        lines, ids = metrics_lines(rows, years=[2022])
        heads = [ln for ln in lines if ln.startswith("Nvidia:")]
        assert heads == [f"Nvidia: fiscal year ended {y}-01-26" for y in (2026, 2025, 2024, 2022, 2021)]
        assert any("2022-01-26" in ln and "computed: +100.0% vs fiscal year ended 2021-01-26" in ln for ln in lines)
        assert f"xbrl:{NVDA}:revenue:2022-01-26" in ids and f"xbrl:{NVDA}:revenue:2021-01-26" in ids
