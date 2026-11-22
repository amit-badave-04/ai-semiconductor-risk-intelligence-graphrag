"""M2a spec section 2: the context-only query columns (quote ids' currency, anchor ciks, the rule abstract) must not widen the
Company API payload. Pure: the graph read is a fake."""

from semigraph.retrieval import dossier
from semigraph.retrieval import retriever as R

OLD_EDGE_KEYS = {"source", "relation", "target", "status", "quote", "chunk_ids"}
OLD_RULE_KEYS = OLD_EDGE_KEYS | {"rule_id", "date", "url", "kind", "link_source", "link_method", "external"}


def test_the_company_payload_keeps_exactly_the_old_edge_and_rule_keys(monkeypatch):
    edge = {k: "x" for k in OLD_EDGE_KEYS} | {"status": "Active"} | {"source_cik": 1, "target_cik": 2, "evidence": [{"id": "a"}]}
    rule = {k: "x" for k in OLD_RULE_KEYS} | {"status": "Active"} | {"abstract": "ABSTRACT TEXT"}

    def fake(driver, query, **params):
        if query == R.RULE_EDGES_QUERY:
            return [dict(rule)]
        if query == R.company_edges_query(1):
            return [dict(edge)]
        return [{"cik": 1, "name": "Nvidia"}]

    monkeypatch.setattr(dossier, "run_cypher", fake)
    payload = dossier.get_dossier(object(), "NVDA")
    assert set(payload["edges"][0]) == OLD_EDGE_KEYS and set(payload["rules"][0]) == OLD_RULE_KEYS
    assert "ABSTRACT TEXT" not in str(payload) and "evidence" not in payload["edges"][0]


def test_the_queries_do_return_the_new_context_columns():
    assert "AS evidence" in R.COMPANY_EDGES_QUERY and "source_cik" in R.COMPANY_EDGES_QUERY
    assert "x.abstract AS abstract" in R.RULE_EDGES_QUERY


def test_the_company_api_returns_only_active_relations_but_the_query_keeps_every_status(monkeypatch):
    def edge(target, status):
        return {"source": "Huawei", "relation": "CUSTOMER_OF", "target": target, "status": status, "quote": "q",
                "chunk_ids": []}

    rows = [edge("Qualcomm", "Historical"), edge("TSMC", "Active")]

    def fake(driver, query, **params):
        if query == R.company_edges_query(1):
            return [dict(r) for r in rows]
        if query == R.RULE_EDGES_QUERY:
            return []
        return [{"cik": 1, "name": "Huawei"}]

    monkeypatch.setattr(dossier, "run_cypher", fake)
    assert [e["target"] for e in dossier.get_dossier(object(), "NVDA")["edges"]] == ["TSMC"]
    assert "status" not in R.company_edges_query(1).replace("rel.status AS status", "")   # no filter in the shared query
