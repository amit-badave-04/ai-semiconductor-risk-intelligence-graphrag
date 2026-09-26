"""``graph_stats`` (the public /api/stats graph block): counts what the page says it counts, no edge-count mislabelled."""

from semigraph.serve import main


class FakeGraph:
    """Answers the three queries graph_stats issues; records every Cypher text it is given."""

    def __init__(self, labels, removed=0, relationships=22792):
        self.labels, self.removed, self.relationships, self.queries = labels, removed, relationships, []

    def run_cypher(self, driver, query, **params):
        self.queries.append(query)
        if "labels(n)[0]" in query:
            return [{"label": label, "n": n} for label, n in self.labels.items()]
        if "count(r)" in query:
            return [{"n": self.relationships}]
        if "removed_in IS NOT NULL" in query:
            return [{"n": self.removed}]
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


def test_the_edge_count_is_no_longer_published_as_lineages(monkeypatch):
    stats = stats_with(monkeypatch, FakeGraph({"RiskItem": 5}, removed=2))
    assert "deleted_risk_lineages" not in stats
    assert set(stats) == {"nodes", "relationships", "risk_items", "removed_risk_items"}


def test_a_graph_without_risk_items_reports_zero_and_skips_the_removed_query(monkeypatch):
    fake = FakeGraph({"EvidenceSpan": 10, "RiskFactor": 4})
    stats = stats_with(monkeypatch, fake)
    assert stats["removed_risk_items"] == 0
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
