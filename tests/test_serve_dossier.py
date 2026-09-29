"""``retrieval.dossier`` (pure shaping) and ``serve.dossier_routes`` (HTTP layer): read-only company data, no LLM, no
embedder (M4, docs/v2/M4_PLAN.md 4.5). Every graph read is a fake ``run_cypher`` dispatch on query identity; the
route tests install a RAISING embedder on ``app.state`` so any accidental ``st.embedder`` touch fails the test loudly
instead of silently passing.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semigraph.retrieval import dossier
from semigraph.serve import dossier_routes
from semigraph.serve.guard import RateLimiter

TICKER = "NVDA"
CIK = 1045810


def _company_row():
    return [{"cik": CIK, "name": "Nvidia"}]


def _base_temporal_row(**overrides):
    row = {"company": "Nvidia", "cik": CIK, "change": "pair", "item_id": None, "headline": None,
          "older_headline": None, "unit_kind": None, "section_id": None, "seq": None, "length": None,
          "older_chunk_ids": [], "newer_chunk_ids": [], "decided_by": None, "sim_embed": None, "sim_lex": None,
          "lineage": None, "lead_text": None, "older_accession": "acc1", "older_form": "10-K",
          "older_date": "2025-02-20", "newer_accession": "acc2", "newer_form": "10-K", "newer_date": "2026-02-20",
          "compared": True, "not_compared_reason": None}
    row.update(overrides)
    return row


TEMPORAL_ROWS = [
    _base_temporal_row(change="pair"),
    _base_temporal_row(change="removed", item_id="item-old-1", headline="Old Risk", unit_kind="headline",
                       older_chunk_ids=["c1"]),
    _base_temporal_row(change="new", item_id="item-new-1", headline="New Risk", unit_kind="headline",
                       newer_chunk_ids=["c2"]),
    # A RiskItem's own chunk ids can span several evidence chunks: this item's older wording is quoted from BOTH
    # c1a (a fully dropped sentence) and c3 (a reworded one) — the join must pick up passages for either.
    _base_temporal_row(change="reworded", item_id="item-new-2", headline="Reworded New",
                       older_headline="Reworded Old", unit_kind="headline", older_chunk_ids=["c1a", "c3"],
                       newer_chunk_ids=["c4"]),
    _base_temporal_row(change="unsettled", item_id="item-old-2", headline="Unsettled Risk", unit_kind="headline"),
]

# graph/passages.py computes passages ONLY for items the aligner MATCHED across versions (reworded items) — a
# wholly removed or wholly new item never has one (no "before"/"after" text to diff). All three passages below
# therefore sit under the ONE reworded item pair: "removed"/"reworded" kind rows are owned by the OLDER item
# ("item-old-3", headline "Reworded Old" — NOT the same id the temporal row reports for this item), "added" kind
# rows are owned by the NEWER item ("item-new-2" — the SAME id the temporal row reports). The join is by CHUNK-ID
# OVERLAP against the temporal row's own `older_chunk_ids` (see dossier.py's docstring) — headline is a fallback
# only, so `item_headline` below is deliberately shared with the unrelated "item-old-1" (headline "Old Risk", NOT
# "Reworded Old") to prove the join never uses headline when chunk ids are present.
PASSAGE_ROWS = [
    {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p1", "kind": "removed",
     "item_id": "item-old-3", "item_headline": "Reworded Old", "item_unit_kind": "headline", "section_id": None,
     "lead_text": None, "text": "a sentence dropped from the older wording", "counterpart_text": None,
     "similarity": None, "chunk_ids": ["c1a"], "counterpart_chunk_ids": []},
    {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p2", "kind": "added",
     "item_id": "item-new-2", "item_headline": "Reworded New", "item_unit_kind": "headline", "section_id": None,
     "lead_text": None, "text": "a wholly new sentence in the newer wording", "counterpart_text": None,
     "similarity": None, "chunk_ids": ["c2"], "counterpart_chunk_ids": []},
    {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p3", "kind": "reworded",
     "item_id": "item-old-3", "item_headline": "Reworded Old", "item_unit_kind": "headline", "section_id": None,
     "lead_text": None, "text": "reworded old text", "counterpart_text": "reworded new text", "similarity": 0.5,
     "chunk_ids": ["c3"], "counterpart_chunk_ids": ["c4"]},
]


# ======================================================================== retrieval.dossier (pure)

def test_get_dossier_returns_none_for_a_ticker_outside_filers(monkeypatch):
    monkeypatch.setattr(dossier, "run_cypher", lambda *a, **k: pytest.fail("must not query the graph"))
    assert dossier.get_dossier(object(), "ZZZZ") is None


def test_get_dossier_returns_none_when_the_company_is_absent_from_the_graph(monkeypatch):
    monkeypatch.setattr(dossier, "run_cypher", lambda d, q, **p: [])
    assert dossier.get_dossier(object(), TICKER) is None


def test_get_dossier_shapes_every_block(monkeypatch):
    def fake_run_cypher(driver, query, **params):
        if query == dossier.COMPANY_QUERY:
            return _company_row()
        if query == dossier.FILINGS_QUERY:
            assert params == {"cik": CIK}
            return [{"accession_no": "acc1", "form": "10-K", "filing_date": "2026-02-20", "is_current": True,
                    "status": "current", "superseded_by": None}]
        if query == dossier.METRICS_QUERY:
            assert params == {"ids": [CIK], "periods": dossier.DOSSIER_METRIC_PERIODS, "years": [], "dates": []}
            return [{"cik": CIK, "company": "Nvidia", "metric": "revenue", "value": 1.0, "unit": "USD",
                    "period_start": "2025-02-01", "period_end": "2026-01-31"}]
        if query == dossier.DOSSIER_ACTIVE_RISKS_QUERY:
            assert params == {"cik": CIK}
            return [{"risk_id": "r1", "summary": "s", "category": "c", "last_evidenced_at": "2026-01-01"}]
        if query == dossier.company_edges_query(1):
            return [{"source": "Nvidia", "relation": "SUPPLIES_TO", "target": "X"}]
        if query == dossier.RULE_EDGES_QUERY:
            assert params["ids"] == [CIK] and params["include_neighbours"] is False
            assert params["per_company"] == dossier.RULES_PER_COMPANY
            return [{"source": "Nvidia", "relation": "AFFECTED_BY", "target": "Rule"}]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(dossier, "run_cypher", fake_run_cypher)
    result = dossier.get_dossier(object(), TICKER, data_as_of="2026-09-24")
    assert result["company"] == {"ticker": TICKER, "name": "Nvidia", "cik": CIK}
    assert result["data_as_of"] == "2026-09-24"
    assert len(result["filings"]) == 1
    assert len(result["metrics"]) == 1
    assert len(result["active_risks"]) == 1
    assert len(result["edges"]) == 1
    assert len(result["rules"]) == 1


def test_dossier_active_risks_query_never_uses_search_or_an_embedding(monkeypatch):
    assert "SEARCH" not in dossier.DOSSIER_ACTIVE_RISKS_QUERY
    assert "$vec" not in dossier.DOSSIER_ACTIVE_RISKS_QUERY


def test_get_risk_changes_returns_none_for_a_ticker_outside_filers(monkeypatch):
    monkeypatch.setattr(dossier, "run_cypher", lambda *a, **k: pytest.fail("must not query the graph"))
    assert dossier.get_risk_changes(object(), "ZZZZ") is None


def test_get_risk_changes_returns_none_when_the_company_is_absent_from_the_graph(monkeypatch):
    monkeypatch.setattr(dossier, "run_cypher", lambda d, q, **p: [])
    assert dossier.get_risk_changes(object(), TICKER) is None


def _stub_changes_driver(monkeypatch, temporal_rows, passage_rows, expect_pairs=None):
    def fake_run_cypher(driver, query, **params):
        if query == dossier.COMPANY_QUERY:
            return _company_row()
        if query == dossier.TEMPORAL_QUERY:
            assert params == {"ids": [CIK]}
            return temporal_rows
        if query == dossier.PASSAGES_QUERY:
            if expect_pairs is not None:
                assert params["pairs"] == expect_pairs
            return passage_rows
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(dossier, "run_cypher", fake_run_cypher)


def test_get_risk_changes_shapes_pairs_and_joins_passages_by_the_documented_rules(monkeypatch):
    _stub_changes_driver(monkeypatch, TEMPORAL_ROWS, PASSAGE_ROWS,
                         expect_pairs=[{"cik": CIK, "older": "acc1", "newer": "acc2"}])
    result = dossier.get_risk_changes(object(), TICKER, limit=20)
    assert result["company"] == {"ticker": TICKER, "name": "Nvidia", "cik": CIK}
    assert len(result["pairs"]) == 1
    pair = result["pairs"][0]
    assert pair["older"] == {"accession_no": "acc1", "form": "10-K", "filing_date": "2025-02-20"}
    assert pair["newer"] == {"accession_no": "acc2", "form": "10-K", "filing_date": "2026-02-20"}
    assert pair["compared"] is True and pair["not_compared_reason"] is None

    by_kind = {item["kind"]: item for item in pair["items"]}
    assert set(by_kind) == {"dropped", "new", "changed", "unsettled"}

    # A wholly dropped or wholly new item never has a RiskPassage (compute_passages only diffs MATCHED items) —
    # passages are always empty for them, whatever id they carry.
    dropped = by_kind["dropped"]
    assert dropped["older_item_id"] == "item-old-1" and dropped["newer_item_id"] is None
    assert dropped["passages"] == []

    new = by_kind["new"]
    assert new["newer_item_id"] == "item-new-1" and new["older_item_id"] is None
    assert new["passages"] == []

    changed = by_kind["changed"]
    # The temporal row's own item id (the newer item) — the older item id is unknowable from this query, so it is
    # left None rather than invented (see dossier.py's module docstring).
    assert changed["newer_item_id"] == "item-new-2" and changed["older_item_id"] is None
    # removed/reworded-kind passages (owned by the older item, joined by chunk-id overlap against the item's own
    # `older_chunk_ids`) then the added-kind one (owned by the newer item, joined by the shared id) — see
    # dossier._item_passages.
    assert changed["passages"] == [
        {"quote": "a sentence dropped from the older wording", "chunk_id": "c1a", "side": "older"},
        {"quote": "reworded old text", "chunk_id": "c3", "side": "older"},
        {"quote": "a wholly new sentence in the newer wording", "chunk_id": "c2", "side": "newer"},
    ]

    assert by_kind["unsettled"]["passages"] == []
    assert by_kind["unsettled"]["older_item_id"] is None and by_kind["unsettled"]["newer_item_id"] is None


def test_get_risk_changes_passes_limit_derived_caps_to_select_passages_too(monkeypatch):
    """PASSAGE_CAPS (4-8) would otherwise silently truncate a caller's larger limit (e.g. 50)."""
    _stub_changes_driver(monkeypatch, [], [])
    captured = {}
    real_select_passages = dossier.select_passages

    def spy(rows, pairs, question, caps=None):
        captured["caps"] = caps
        return real_select_passages(rows, pairs, question, caps=caps)

    monkeypatch.setattr(dossier, "select_passages", spy)
    dossier.get_risk_changes(object(), TICKER, limit=50)
    assert all(v == 50 for v in captured["caps"].values())


def test_get_risk_changes_caps_the_limit_at_fifty(monkeypatch):
    _stub_changes_driver(monkeypatch, [], [])
    captured = {}
    real_select_temporal = dossier.select_temporal

    def spy(rows, question, caps=None, pairs=None):
        captured["caps"] = caps
        return real_select_temporal(rows, question, caps=caps, pairs=pairs)

    monkeypatch.setattr(dossier, "select_temporal", spy)
    dossier.get_risk_changes(object(), TICKER, limit=9999)
    assert all(v == dossier.RISK_CHANGES_LIMIT_CAP for v in captured["caps"].values())


def test_get_risk_changes_keeps_a_not_compared_pair_with_its_reason(monkeypatch):
    row = _base_temporal_row(compared=False, not_compared_reason="low_text_yield")

    def fake_run_cypher(driver, query, **params):
        if query == dossier.COMPANY_QUERY:
            return _company_row()
        if query == dossier.TEMPORAL_QUERY:
            return [row]
        if query == dossier.PASSAGES_QUERY:
            pytest.fail("a not-compared pair must never be queried for passages")
        raise AssertionError(query)

    monkeypatch.setattr(dossier, "run_cypher", fake_run_cypher)
    result = dossier.get_risk_changes(object(), TICKER)
    assert len(result["pairs"]) == 1
    pair = result["pairs"][0]
    assert pair["compared"] is False and pair["not_compared_reason"] == "low_text_yield"
    assert pair["items"] == []


def test_get_risk_changes_with_no_pairs_at_all_returns_an_empty_list(monkeypatch):
    _stub_changes_driver(monkeypatch, [], [])
    result = dossier.get_risk_changes(object(), TICKER)
    assert result["pairs"] == []


# ------------------------------------------------------------ finding 20: chunk-id join, not headline (regression)

def test_get_risk_changes_joins_paragraph_units_with_no_headline_by_chunk_id(monkeypatch):
    """A 20-F (paragraph-unit) filer's items carry no `headline` at all — the old headline-only join could never
    reach them (docs/v2/M4_PLAN.md finding 20). Mirrors the reviewer's dossier_join.py repro exactly: two reworded
    paragraph items in one pair, told apart only by chunk id."""
    rows = [
        _base_temporal_row(change="pair"),
        _base_temporal_row(change="reworded", item_id="new-P1", headline=None, older_headline=None,
                           unit_kind="paragraph", older_chunk_ids=["o1"], newer_chunk_ids=["n1"]),
        _base_temporal_row(change="reworded", item_id="new-P2", headline=None, older_headline=None,
                           unit_kind="paragraph", older_chunk_ids=["o2"], newer_chunk_ids=["n2"]),
    ]
    passages = [
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p1", "kind": "removed",
         "item_id": "old-P1", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "text of p1", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["o1"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p2", "kind": "reworded",
         "item_id": "old-P2", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "text of p2", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["o2"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p3", "kind": "added",
         "item_id": "new-P1", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "text of p3", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["n1"], "counterpart_chunk_ids": []},
    ]
    _stub_changes_driver(monkeypatch, rows, passages)
    result = dossier.get_risk_changes(object(), TICKER)
    by_newer_id = {it["newer_item_id"]: it for it in result["pairs"][0]["items"]}
    assert [p["quote"] for p in by_newer_id["new-P1"]["passages"]] == ["text of p1", "text of p3"]
    assert [p["quote"] for p in by_newer_id["new-P2"]["passages"]] == ["text of p2"]


def test_get_risk_changes_never_cross_attaches_two_items_that_share_an_older_headline(monkeypatch):
    """Two reworded items with the SAME older_headline but distinct chunk ids must each keep only their own
    passages — the exact "repeated headlines cross-attach" failure named in finding 20."""
    rows = [
        _base_temporal_row(change="pair"),
        _base_temporal_row(change="reworded", item_id="item-A", headline="A new", older_headline="Same Heading",
                           unit_kind="headline", older_chunk_ids=["cA"], newer_chunk_ids=["nA"]),
        _base_temporal_row(change="reworded", item_id="item-B", headline="B new", older_headline="Same Heading",
                           unit_kind="headline", older_chunk_ids=["cB"], newer_chunk_ids=["nB"]),
    ]
    passages = [
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pA", "kind": "reworded",
         "item_id": "old-A", "item_headline": "Same Heading", "item_unit_kind": "headline", "section_id": None,
         "lead_text": None, "text": "text of pA", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["cA"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pB", "kind": "reworded",
         "item_id": "old-B", "item_headline": "Same Heading", "item_unit_kind": "headline", "section_id": None,
         "lead_text": None, "text": "text of pB", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["cB"], "counterpart_chunk_ids": []},
    ]
    _stub_changes_driver(monkeypatch, rows, passages)
    result = dossier.get_risk_changes(object(), TICKER)
    by_newer_id = {it["newer_item_id"]: it for it in result["pairs"][0]["items"]}
    assert [p["quote"] for p in by_newer_id["item-A"]["passages"]] == ["text of pA"]
    assert [p["quote"] for p in by_newer_id["item-B"]["passages"]] == ["text of pB"]


def test_get_risk_changes_disambiguates_two_items_that_share_one_evidence_chunk(monkeypatch):
    """The deeper gap plain chunk-overlap alone does not catch: two ADJACENT paragraph items can legitimately share
    ONE evidence chunk (a chunker window straddling their boundary — see retriever.py's _LEAD_TEXT comment). Item A
    and item B share chunk "shared" but each also has a chunk unique to itself; the best-Jaccard-overlap assignment
    must send owner old-A's passages to item A only, and old-B's to item B only — never both to both."""
    rows = [
        _base_temporal_row(change="pair"),
        _base_temporal_row(change="reworded", item_id="new-A", headline=None, older_headline=None,
                           unit_kind="paragraph", older_chunk_ids=["shared", "a-only"], newer_chunk_ids=["nA"]),
        _base_temporal_row(change="reworded", item_id="new-B", headline=None, older_headline=None,
                           unit_kind="paragraph", older_chunk_ids=["shared", "b-only"], newer_chunk_ids=["nB"]),
    ]
    passages = [
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pA1", "kind": "reworded",
         "item_id": "old-A", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "old-A shared sentence", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["shared"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pA2", "kind": "removed",
         "item_id": "old-A", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "old-A own sentence", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["a-only"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pB1", "kind": "reworded",
         "item_id": "old-B", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "old-B shared sentence", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["shared"], "counterpart_chunk_ids": []},
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "pB2", "kind": "removed",
         "item_id": "old-B", "item_headline": None, "item_unit_kind": "paragraph", "section_id": None,
         "lead_text": None, "text": "old-B own sentence", "counterpart_text": None, "similarity": None,
         "chunk_ids": ["b-only"], "counterpart_chunk_ids": []},
    ]
    _stub_changes_driver(monkeypatch, rows, passages)
    result = dossier.get_risk_changes(object(), TICKER)
    by_newer_id = {it["newer_item_id"]: it for it in result["pairs"][0]["items"]}
    assert {p["quote"] for p in by_newer_id["new-A"]["passages"]} == {"old-A shared sentence", "old-A own sentence"}
    assert {p["quote"] for p in by_newer_id["new-B"]["passages"]} == {"old-B shared sentence", "old-B own sentence"}


def test_get_risk_changes_falls_back_to_headline_only_when_chunk_ids_are_entirely_absent(monkeypatch):
    rows = [
        _base_temporal_row(change="pair"),
        _base_temporal_row(change="reworded", item_id="item-new", headline="New", older_headline="Old Heading",
                           unit_kind="headline", older_chunk_ids=[], newer_chunk_ids=[]),
    ]
    passages = [
        {"cik": CIK, "older_accession": "acc1", "newer_accession": "acc2", "passage_id": "p1", "kind": "reworded",
         "item_id": "old-item", "item_headline": "Old Heading", "item_unit_kind": "headline", "section_id": None,
         "lead_text": None, "text": "headline-only fallback text", "counterpart_text": None, "similarity": None,
         "chunk_ids": [], "counterpart_chunk_ids": []},
    ]
    _stub_changes_driver(monkeypatch, rows, passages)
    result = dossier.get_risk_changes(object(), TICKER)
    passages_out = result["pairs"][0]["items"][0]["passages"]
    assert [p["quote"] for p in passages_out] == ["headline-only fallback text"]


# ======================================================================== serve.dossier_routes (HTTP)

class RaisingEmbedder:
    """Any attribute access fails the test loudly — dossier routes must never touch the embedder (D8)."""

    def __getattr__(self, name):
        raise AssertionError(f"dossier routes must never touch the embedder (accessed .{name})")


class FakeSettings:
    client_ip_header = ""
    read_rate_limit_per_minute = 5


@pytest.fixture
def app_client(monkeypatch):
    def _make(get_dossier=None, get_risk_changes=None, read_limit=5, snapshot=None):
        app = FastAPI()
        app.include_router(dossier_routes.router)
        settings = FakeSettings()
        settings.read_rate_limit_per_minute = read_limit
        app.state.settings = settings
        app.state.read_rate_limiter = RateLimiter(read_limit, 60)
        app.state.driver = object()
        app.state.embedder = RaisingEmbedder()
        app.state.snapshot = snapshot
        if get_dossier is not None:
            monkeypatch.setattr(dossier, "get_dossier", get_dossier)
        if get_risk_changes is not None:
            monkeypatch.setattr(dossier, "get_risk_changes", get_risk_changes)
        return TestClient(app)

    return _make


def test_dossier_route_200_with_cache_control_and_passes_the_snapshot_as_of(app_client):
    calls = []

    def fake_get_dossier(driver, ticker, *, data_as_of=None):
        calls.append((ticker, data_as_of))
        return {"company": {"ticker": ticker}, "data_as_of": data_as_of, "filings": []}

    client = app_client(get_dossier=fake_get_dossier, snapshot={"as_of": "2026-09-24"})
    resp = client.get("/api/company/NVDA/dossier")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert calls == [("NVDA", "2026-09-24")]


def test_dossier_route_lowercase_ticker_is_normalised(app_client):
    calls = []
    client = app_client(get_dossier=lambda driver, ticker, **kw: calls.append(ticker) or {"company": {}})
    client.get("/api/company/nvda/dossier")
    assert calls == ["NVDA"]


def test_dossier_route_404_for_a_ticker_outside_filers(app_client):
    client = app_client()
    resp = client.get("/api/company/ZZZZ/dossier")
    assert resp.status_code == 404


def test_dossier_route_404_when_the_shaping_function_finds_nothing(app_client):
    client = app_client(get_dossier=lambda driver, ticker, **kw: None)
    resp = client.get("/api/company/NVDA/dossier")
    assert resp.status_code == 404


def test_dossier_route_is_read_rate_limited(app_client):
    client = app_client(get_dossier=lambda driver, ticker, **kw: {"company": {}}, read_limit=1)
    assert client.get("/api/company/NVDA/dossier").status_code == 200
    assert client.get("/api/company/NVDA/dossier").status_code == 429


def test_dossier_route_caches_within_the_window(app_client):
    calls = []

    def fake_get_dossier(driver, ticker, **kw):
        calls.append(1)
        return {"company": {"ticker": ticker}}

    client = app_client(get_dossier=fake_get_dossier)
    client.get("/api/company/NVDA/dossier")
    client.get("/api/company/NVDA/dossier")
    assert len(calls) == 1


def test_dossier_route_never_touches_the_embedder(app_client):
    # RaisingEmbedder is installed on app.state by the fixture for every test in this class; a plain 200 here IS
    # the proof — any st.embedder access would raise inside the handler and surface through the test client.
    client = app_client(get_dossier=lambda driver, ticker, **kw: {"company": {"ticker": ticker}})
    assert client.get("/api/company/NVDA/dossier").status_code == 200


def test_risk_changes_route_200_and_caps_the_limit(app_client):
    seen = {}

    def fake_get_risk_changes(driver, ticker, *, limit=20):
        seen["limit"] = limit
        return {"company": {"ticker": ticker}, "pairs": []}

    client = app_client(get_risk_changes=fake_get_risk_changes)
    resp = client.get("/api/company/NVDA/risk-changes?limit=9999")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert seen["limit"] == 50


def test_risk_changes_route_404_for_a_ticker_outside_filers(app_client):
    client = app_client()
    resp = client.get("/api/company/ZZZZ/risk-changes")
    assert resp.status_code == 404


def test_risk_changes_route_never_touches_the_embedder(app_client):
    client = app_client(get_risk_changes=lambda driver, ticker, **kw: {"company": {"ticker": ticker}, "pairs": []})
    assert client.get("/api/company/NVDA/risk-changes").status_code == 200
