"""``graph_stats`` (the public /api/stats graph block): counts what the page says it counts, no edge-count mislabelled.

Changed by the review (M3): a paragraph unit (a 20-F filer's, or a filing with no risk-factor headlines) is not a risk
factor, so ``removed_risk_items`` counts removed HEADLINE units and ``removed_paragraphs`` the removed paragraph units.

Changed by the M1b hedging pass: the KEYS stay (they are the API contract) but what they count is documented as "the text check
found no matching text in a newer filing", not "verified absent": held-out gold found such an item gone as a standalone risk
factor in 4 of 4 cases, with 2 of the 4 merged into another risk factor. The page words them "no longer stand alone (text check)"."""

from semigraph.serve import main


class FakeGraph:
    """Answers the three queries graph_stats issues; records every Cypher text it is given."""

    def __init__(self, labels, removed=0, removed_paragraphs=0, relationships=22792):
        self.labels, self.relationships, self.queries = labels, relationships, []
        self.removed = [{"kind": kind, "n": n} for kind, n in (("headline", removed), ("paragraph", removed_paragraphs)) if n]

    def run_cypher(self, driver, query, **params):
        self.queries.append(query)
        if "labels(n)[0]" in query:
            return [{"label": label, "n": n} for label, n in self.labels.items()]
        if "count(r)" in query:
            return [{"n": self.relationships}]
        if "removed_in IS NOT NULL" in query:
            return self.removed
        raise AssertionError(f"unexpected query: {query}")


def stats_with(monkeypatch, fake):
    monkeypatch.setattr(main, "run_cypher", fake.run_cypher)
    return main.graph_stats(object())


def test_removed_and_total_risk_items_are_reported(monkeypatch):
    fake = FakeGraph({"EvidenceSpan": 3152, "RiskItem": 900, "Company": 26}, removed=41)
    stats = stats_with(monkeypatch, fake)
    assert stats["removed_risk_items"] == 41
    assert stats["risk_items"] == 900
    assert stats["nodes"] == {"EvidenceSpan": 3152, "RiskItem": 900, "Company": 26}
    assert stats["relationships"] == 22792


def test_removed_paragraphs_are_counted_apart_from_removed_risk_factors(monkeypatch):
    stats = stats_with(monkeypatch, FakeGraph({"RiskItem": 900}, removed=41, removed_paragraphs=7))
    assert stats["removed_risk_items"] == 41 and stats["removed_paragraphs"] == 7


def test_a_graph_with_only_removed_paragraphs_reports_zero_removed_risk_factors(monkeypatch):
    stats = stats_with(monkeypatch, FakeGraph({"RiskItem": 230}, removed=0, removed_paragraphs=9))
    assert stats["removed_risk_items"] == 0 and stats["removed_paragraphs"] == 9


def test_the_removed_query_groups_by_unit_kind_and_an_item_with_no_kind_counts_as_a_headline_unit(monkeypatch):
    fake = FakeGraph({"RiskItem": 5}, removed=1)
    stats_with(monkeypatch, fake)
    (query,) = [q for q in fake.queries if "removed_in IS NOT NULL" in q]
    assert "coalesce(i.unit_kind, 'headline') AS kind" in query and "count(i) AS n" in query


def test_the_edge_count_is_no_longer_published_as_lineages(monkeypatch):
    stats = stats_with(monkeypatch, FakeGraph({"RiskItem": 5}, removed=2))
    assert "deleted_risk_lineages" not in stats
    assert set(stats) == {"nodes", "relationships", "risk_items", "removed_risk_items", "removed_paragraphs"}


def test_a_graph_without_risk_items_reports_zero_and_skips_the_removed_query(monkeypatch):
    fake = FakeGraph({"EvidenceSpan": 10, "RiskFactor": 4})
    stats = stats_with(monkeypatch, fake)
    assert stats["removed_risk_items"] == 0 and stats["removed_paragraphs"] == 0
    assert stats["risk_items"] == 0
    assert not any("removed_in" in q for q in fake.queries), "no RiskItem label, so no query on it (no unknown-label warning)"


def test_no_query_counts_disclosure_edges(monkeypatch):
    fake = FakeGraph({"RiskItem": 3}, removed=1)
    stats_with(monkeypatch, fake)
    assert not any("DISCLOSES_RISK" in q for q in fake.queries)
    assert any("(i:RiskItem)" in q and "removed_in IS NOT NULL" in q for q in fake.queries)


def test_service_nodes_stay_out_of_the_public_counts(monkeypatch):
    # The ledger, cache and policy nodes (Svc*) are filtered in the label query itself.
    fake = FakeGraph({"Company": 26})
    stats_with(monkeypatch, fake)
    assert any("NOT label STARTS WITH 'Svc'" in q for q in fake.queries)


def test_the_stats_documentation_does_not_call_the_removed_count_verified():
    doc = " ".join((main.graph_stats.__doc__ or "").split())
    assert "verified absent" not in doc and "no matching text" in doc and "no longer stand alone" in doc
