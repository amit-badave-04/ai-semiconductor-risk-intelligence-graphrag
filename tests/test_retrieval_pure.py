"""Pure-logic tests for semigraph.retrieval — no Neo4j, no network, no real LLM.

Covers context-block assembly (notebook 14 build_blocks), anchor detection
from the packaged canonical dictionary, citation extraction/validation, the
answer() plumbing with a mocked retriever + llm, and the local hardened
plain-text call's battle scars (litellm fully mocked).
"""

import json
import logging
import re
from types import SimpleNamespace

import litellm
import pytest

import semigraph.retrieval.answerer as answerer_mod
import semigraph.retrieval.retriever as retriever_mod
from semigraph.retrieval import (
    CITE_RE,
    DEFAULT_ANCHOR_CIK,
    answer,
    build_blocks,
    detect_anchors,
    hybrid_retrieve,
    llm_text,
    vector_retrieve,
)
from semigraph.retrieval.retriever import (
    ACTIVE_RISKS_PER_ANCHOR,
    ACTIVE_RISKS_QUERY,
    ACTIVE_RISKS_TOP,
    COMPANY_EDGES_QUERY,
    EXCERPT_CANDIDATES,
    EXCERPTS_QUERY,
    METRICS_QUERY,
    RULE_EDGES_QUERY,
    RULES_PER_COMPANY,
    TEMPORAL_QUERY,
    VECTOR_QUERY,
    company_edges_query,
)

CID1 = "0001045810-26-000021:I.1:0320"
CID2 = "0001045810-26-000021:I.1A:0345"
CID3 = "0001045810-24-000029:I.1A:0152"


def synthetic_retrieval():
    return {
        "anchors": {"Nvidia": 1045810},
        "edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC",
                   "status": "Active", "quote": "We utilize foundries",
                   "chunk_ids": [CID1]}],
        "metrics": [{"company": "Nvidia", "metric": "revenue", "value": 60922000000.0,
                     "period_start": "2023-01-30", "period_end": "2024-01-28"}],
        "risks": [{"company": "Nvidia", "category": "supply_chain",
                   "summary": "Geographic concentration of suppliers",
                   "chunk_id": CID2, "score": 0.9}],
        "temporal": [{"company": "Nvidia", "lineage": "1045810:3",
                      "first_seen": "2023-02-24", "last_seen": "2025-02-26",
                      "example": "COVID-related supply disruption risk"}],
        "chunks": [{"chunk_id": CID3, "score": 0.88,
                    "text": "Export controls affect our China sales.",
                    "source_url": "https://www.sec.gov/x"}],
    }


# --- build_blocks (context assembly) ---

def test_build_blocks_collects_valid_ids_from_all_layers():
    blocks, full_context, valid_ids = build_blocks(synthetic_retrieval())
    assert valid_ids == {CID1, CID2, CID3}


def test_build_blocks_full_context_has_all_five_sections():
    _, full_context, _ = build_blocks(synthetic_retrieval())
    for header in ("RELATIONSHIPS:", "METRICS:", "ACTIVE RISKS:",
                   "DROPPED RISK LINEAGES:", "EXCERPTS:"):
        assert header in full_context
    # the bitemporal layer is surfaced in the context the model (and judge) sees
    assert "disclosed 2023-02-24 through 2025-02-26, then dropped" in full_context
    assert "60,922,000,000 USD" in full_context
    assert "Export controls affect our China sales." in full_context


def test_build_blocks_empty_layers_render_none_placeholders():
    empty = {"anchors": {}, "edges": [], "metrics": [], "risks": [],
             "temporal": [], "chunks": []}
    (e_b, m_b, k_b, t_b, c_b), full_context, valid_ids = build_blocks(empty)
    assert (e_b, m_b, k_b, t_b, c_b) == ("(none)",) * 5
    assert valid_ids == set()


def test_build_blocks_edge_without_chunk_ids_key():
    r = synthetic_retrieval()
    r["edges"] = [{"source": "A", "relation": "COMPETES_WITH", "target": "B",
                   "status": None, "quote": None, "chunk_ids": None}]
    blocks, _, valid_ids = build_blocks(r)
    assert "- A COMPETES_WITH B (status=None)" in blocks[0]
    assert CID1 not in valid_ids


# --- anchor detection (packaged canonical dictionary; deterministic, no LLM) ---

def test_detect_anchors_finds_canonical_entities():
    anchors = detect_anchors("How does Nvidia depend on TSMC?")
    assert anchors.get("Nvidia") == 1045810
    assert "TSMC" in anchors


def test_detect_anchors_is_word_bounded_and_case_insensitive():
    assert "Nvidia" in detect_anchors("what does NVDA disclose?") or \
           "Nvidia" in detect_anchors("what does nvidia disclose?")
    assert detect_anchors("nothing about semiconductors here") == {}


# --- citation grammar ---

def test_cite_re_extracts_valid_ids_only():
    text = (f"Revenue grew [{CID1}]. Risks remain [{CID2}]."
            " Bogus [not-a-chunk] and [12345] ignored.")
    assert set(CITE_RE.findall(text)) == {CID1, CID2}


# --- answer() plumbing with mocked retriever + llm ---

def test_answer_returns_full_context_and_flags_hallucinations(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve",
                        lambda q, d, e, k_chunks=8, hops=2: synthetic_retrieval())
    fake_answer_text = (f"Nvidia depends on TSMC [{CID1}]."
                        f" Made-up claim [0009999999-99-999999:I.1A:0001].")
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        return fake_answer_text

    out = answer("How does Nvidia depend on TSMC?", driver=None, embedder=None,
                 strategy="hybrid", llm=fake_llm)
    assert out["answer"] == fake_answer_text
    assert out["citations"] == sorted([CID1, "0009999999-99-999999:I.1A:0001"])
    assert out["hallucinated"] == {"0009999999-99-999999:I.1A:0001"}
    assert out["valid_ids"] == {CID1, CID2, CID3}
    assert out["chunk_ids"] == [CID3]
    # the full context string is what the answering model saw
    assert "DROPPED RISK LINEAGES:" in out["context"]
    # and the prompt embedded the question + every block
    assert "How does Nvidia depend on TSMC?" in prompts[0]
    assert "=== RISK LINEAGES DROPPED FROM THE LATEST ANNUAL REPORT (bitemporal layer) ===" in prompts[0]


def test_answer_vector_strategy_dispatch(monkeypatch):
    calls = []

    def fake_vector(q, d, e, k=8):
        calls.append(k)
        return {"anchors": {}, "edges": [], "metrics": [], "risks": [],
                "temporal": [], "chunks": []}

    monkeypatch.setattr(answerer_mod, "vector_retrieve", fake_vector)
    out = answer("q", None, None, strategy="vector", llm=lambda p: "No context.", k_chunks=5)
    assert calls == [5]
    assert out["cited"] == set() and out["hallucinated"] == set()


def test_answer_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown strategy"):
        answer("q", None, None, strategy="cypher", llm=lambda p: "x")


# --- llm_text battle scars (litellm mocked) ---

def make_resp(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])


class FakeCompletion:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_llm_text_never_sends_sampling_params_and_disables_thinking(monkeypatch):
    fake = FakeCompletion([make_resp("answer text")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="anthropic/claude-sonnet-5") == "answer text"
    call = fake.calls[0]
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in call
    assert call["thinking"] == {"type": "disabled"}
    assert call["num_retries"] == 2


def test_llm_text_truncation_regenerates_with_doubled_budget(monkeypatch):
    fake = FakeCompletion([make_resp("partial", finish_reason="length"),
                           make_resp("full answer")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="m", max_tokens=1200) == "full answer"
    assert [c["max_tokens"] for c in fake.calls] == [1200, 2400]


def test_llm_text_transient_backs_off_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(answerer_mod.time, "sleep", sleeps.append)
    err = litellm.RateLimitError(message="429", llm_provider="anthropic", model="m")
    fake = FakeCompletion([err, make_resp("ok")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="m") == "ok"
    assert sleeps == [15]


def test_llm_text_empty_content_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(answerer_mod, "completion", FakeCompletion([make_resp(None)] * 4))
    with pytest.raises(RuntimeError, match="llm_text failed after 4 attempts"):
        llm_text("p", model="m")


# ==========================================================================
# C2 — retriever query shapes, per-anchor risks, in-index filters, anchor honesty.
# A fake driver records every (query, params); no Neo4j, no network.
# ==========================================================================

NVDA, TSMC = 1045810, 1046179
EDGE_KEYS = {"source", "relation", "target", "status", "quote", "chunk_ids"}
RISK_KEYS = {"company", "summary", "category", "chunk_id", "score"}
QUERY_VEC = [0.25, 0.5, 0.75, 1.0]

# index filter properties declared at index creation (the C1 property contract)
DECLARED_FILTERS = {
    "evidence_embedding": {"is_current", "retrievable", "filer_cik", "form", "valid_from", "valid_to"},
    "risk_embedding": {"is_current", "filer_cik", "valid_from", "valid_to"},
}
# the only predicate grammar allowed inside SEARCH ... WHERE on Neo4j 2026.05
PREDICATE_RE = re.compile(r"^\w+\.\w+\s*(=|<=|>=|<|>)\s*(\$\w+|true|false|-?\d+)$")


class FakeEmbedder:
    def encode_query(self, question: str) -> list[float]:
        return list(QUERY_VEC)


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self._driver.calls.append((query, params))
        kind = self._driver.classify(query)
        rows = self._driver.responses.get(kind, [])
        return rows(params) if callable(rows) else rows


class FakeDriver:
    """Records (query, params); answers by query kind (a list, or a callable(params) -> list)."""

    def __init__(self, responses: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def session(self, **kw):
        return _FakeSession(self)

    @staticmethod
    def classify(query: str) -> str:
        if query in {company_edges_query(h) for h in range(1, 6)}:
            return "company_edges"
        table = {RULE_EDGES_QUERY: "rules", ACTIVE_RISKS_QUERY: "risks", METRICS_QUERY: "metrics",
                 TEMPORAL_QUERY: "temporal", EXCERPTS_QUERY: "excerpts", VECTOR_QUERY: "vector"}
        return table.get(query, "unknown")

    def of(self, kind: str) -> list[dict]:
        return [p for q, p in self.calls if self.classify(q) == kind]


def edge_row(source, relation, target, status="Active", quote=None, chunk_ids=None) -> dict:
    return {"source": source, "relation": relation, "target": target, "status": status,
            "quote": quote, "chunk_ids": chunk_ids}


def risk_row(company, chunk_id, score, category="regulatory", summary="s") -> dict:
    return {"company": company, "summary": summary, "category": category,
            "chunk_id": chunk_id, "score": score}


def search_where(query: str) -> str:
    """The predicate text inside ``SEARCH ... (VECTOR INDEX i FOR $vec WHERE <this> LIMIT ...)``."""
    m = re.search(r"SEARCH\s+\w+\s+IN\s+\(VECTOR INDEX\s+\w+\s+FOR\s+\$vec\s+WHERE\s+(.*?)\s+LIMIT\b",
                  query, re.S)
    assert m, f"no in-index WHERE in: {query}"
    return m.group(1)


# --- C2.2 (i): company relations — four company types only, Company endpoints ---

def test_company_edges_query_has_only_the_four_company_relation_types():
    q = company_edges_query(2)
    assert "SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH*1..2" in q
    assert "AFFECTED_BY" not in q and "ExportControl" not in q
    assert "(b:Company)" in q and "b:ExportControl" not in q
    assert "{hops}" in COMPANY_EDGES_QUERY and "{hops}" not in q


def test_company_edges_query_keeps_the_v1_row_shape():
    q = company_edges_query(2)
    for col in ("AS source", "AS relation", "AS target", "AS status", "AS quote", "AS chunk_ids"):
        assert col in q
    assert "RETURN DISTINCT" in q


@pytest.mark.parametrize("hops", [1, 2, 3])
def test_company_edges_query_interpolates_hops(hops):
    assert f"*1..{hops}]" in company_edges_query(hops)


@pytest.mark.parametrize("bad", [0, -1, "2; DROP", None, 2.5])
def test_company_edges_query_rejects_invalid_hops(bad):
    with pytest.raises((ValueError, TypeError)):
        company_edges_query(bad)


def test_hybrid_rejects_invalid_hops_before_embedding_or_querying():
    calls = []

    class CountingEmbedder:
        def encode_query(self, question):
            calls.append(question)
            return list(QUERY_VEC)

    d = FakeDriver()
    with pytest.raises(ValueError, match="hops"):
        hybrid_retrieve("How does Nvidia depend on TSMC?", d, CountingEmbedder(), hops=0)
    assert calls == [] and d.calls == []


# --- C2.2 (ii): AFFECTED_BY — rules only at hop 1 from a company, capped per company ---

def test_rule_edges_query_shape():
    q = RULE_EDGES_QUERY
    assert "(c)-[r:AFFECTED_BY]->(x:ExportControl)" in q
    assert "ORDER BY x.date DESC" in q and "LIMIT $per_company" in q
    assert "c.name AS source" in q and "x.title AS target" in q
    for col in ("AS relation", "AS status", "AS quote", "AS chunk_ids"):
        assert col in q
    # never walks ExportControl -> Company, never a variable-length path
    assert "*" not in q
    assert "(x)-" not in q and "<-[r:AFFECTED_BY]" not in q
    # neighbours come from the four company relation types only
    assert "SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH" in q


def test_hybrid_runs_rule_query_for_the_anchors_only_with_a_tight_cap():
    """Rule lines cost ~100 tokens each: 12 rules x (anchor + 16 neighbours) added ~8k tokens per
    answer. Anchors get the 8 newest relevant rules; neighbour rules are off until M2 ranks them."""
    d = FakeDriver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    (params,) = d.of("rules")
    assert params["ids"] == [NVDA, TSMC]
    assert params["per_company"] == RULES_PER_COMPANY == 8
    assert params["include_neighbours"] is False


def test_neighbour_rules_can_be_switched_on_for_deeper_traversals(monkeypatch):
    from semigraph.retrieval import retriever

    monkeypatch.setattr(retriever, "NEIGHBOUR_RULES", True)
    d = FakeDriver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    assert d.of("rules")[0]["include_neighbours"] is True
    d = FakeDriver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder(), hops=1)
    assert d.of("rules")[0]["include_neighbours"] is False       # never beyond hop 1 when hops=1


def test_hops_one_keeps_v1_semantics_anchor_rules_only():
    d = FakeDriver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder(), hops=1)
    assert d.of("rules")[0]["include_neighbours"] is False
    assert "*1..1]" in next(q for q, _ in d.calls if d.classify(q) == "company_edges")


def test_edges_are_company_rows_then_rule_rows_with_identical_shape():
    company = [edge_row("Nvidia", "DEPENDS_ON", "TSMC", chunk_ids=["c1"])]
    rules = [edge_row("Nvidia", "AFFECTED_BY", "Rule A"), edge_row("TSMC", "AFFECTED_BY", "Rule A")]
    d = FakeDriver({"company_edges": company, "rules": rules})
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    assert out["edges"] == company + rules
    assert all(set(e) == EDGE_KEYS for e in out["edges"])


# --- C2.3: per-anchor in-index risk search, merged and re-ranked ---

def test_active_risks_query_is_in_index_filtered_and_keeps_row_keys():
    q = ACTIVE_RISKS_QUERY
    assert "VECTOR INDEX risk_embedding" in q
    assert search_where(q) == "rf.filer_cik = $cik AND rf.is_current = true"
    assert "DISCLOSES_RISK {status:'Active'}" in q and "HAS_EVIDENCE" in q
    for col in ("AS company", "AS summary", "AS category", "AS chunk_id", "score"):
        assert col in q


def test_risks_run_once_per_anchor_with_integer_cik_and_merge_top_six_by_score():
    per_cik = {
        NVDA: [risk_row("Nvidia", f"n{i}", s) for i, s in enumerate((0.95, 0.90, 0.60, 0.50))],
        TSMC: [risk_row("TSMC", f"t{i}", s) for i, s in enumerate((0.93, 0.80, 0.70, 0.40))],
    }
    d = FakeDriver({"risks": lambda p: per_cik[p["cik"]]})
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    calls = d.of("risks")
    assert [p["cik"] for p in calls] == [NVDA, TSMC]
    assert all(isinstance(p["cik"], int) and p["vec"] == QUERY_VEC for p in calls)
    assert all(p["candidates"] == ACTIVE_RISKS_PER_ANCHOR == 20 for p in calls)
    assert [r["chunk_id"] for r in out["risks"]] == ["n0", "t0", "n1", "t1", "t2", "n2"]
    assert len(out["risks"]) == ACTIVE_RISKS_TOP == 6
    assert all(set(r) == RISK_KEYS for r in out["risks"])
    scores = [r["score"] for r in out["risks"]]
    assert scores == sorted(scores, reverse=True)


def test_risks_fewer_than_six_are_all_kept():
    d = FakeDriver({"risks": lambda p: [risk_row("Nvidia", "n0", 0.5)] if p["cik"] == NVDA else []})
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    assert [r["chunk_id"] for r in out["risks"]] == ["n0"]


def test_duplicate_anchor_ciks_do_not_duplicate_risk_rows(monkeypatch):
    monkeypatch.setattr(retriever_mod, "detect_anchors",
                        lambda q: {"Nvidia": NVDA, "NVIDIA Corp": NVDA})
    d = FakeDriver({"risks": [risk_row("Nvidia", "n0", 0.9)]})
    out = hybrid_retrieve("q", d, FakeEmbedder())
    assert len(d.of("risks")) == 1
    assert [r["chunk_id"] for r in out["risks"]] == ["n0"]
    assert d.of("metrics")[0]["ids"] == [NVDA]


# --- C2.4: excerpts (mention semantics kept, freshness in-index) ---

def test_excerpts_query_filters_freshness_in_index_and_keeps_mentions_semantics():
    q = EXCERPTS_QUERY
    assert search_where(q) == "node.retrievable = true"
    assert "(node)-[:MENTIONS]->(c:Company) WHERE c.cik IN $ids" in q
    # the IN (needs 2026.06 inside SEARCH) sits AFTER the search clause, never inside it
    assert q.index("c.cik IN $ids") > q.index("MENTIONS")
    assert ("RETURN DISTINCT node.chunk_id AS chunk_id, score, node.text AS text, "
            "node.source_url AS source_url") in q
    assert "ORDER BY score DESC LIMIT $k" in q


def test_hybrid_excerpt_params():
    d = FakeDriver({"excerpts": [{"chunk_id": CID3, "score": 0.9, "text": "t", "source_url": "u"}]})
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder(), k_chunks=5)
    (p,) = d.of("excerpts")
    assert p["ids"] == [NVDA, TSMC] and p["k"] == 5 and p["vec"] == QUERY_VEC
    assert p["candidates"] == EXCERPT_CANDIDATES == 60
    assert out["chunks"][0]["chunk_id"] == CID3


def test_vector_retrieve_filters_retrievable_in_index():
    assert search_where(VECTOR_QUERY) == "node.retrievable = true"
    d = FakeDriver({"vector": [{"chunk_id": CID3, "score": 0.9, "text": "t", "source_url": "u"}]})
    out = vector_retrieve("q", d, FakeEmbedder(), k=7)
    (p,) = d.of("vector")
    assert p == {"k": 7, "vec": QUERY_VEC}
    assert set(out) == {"anchors", "edges", "metrics", "risks", "temporal", "chunks"}


# --- 2026.05 compatibility guard: every SEARCH ... WHERE stays inside the verified grammar ---

@pytest.mark.parametrize("query, index", [
    (ACTIVE_RISKS_QUERY, "risk_embedding"),
    (EXCERPTS_QUERY, "evidence_embedding"),
    (VECTOR_QUERY, "evidence_embedding"),
])
def test_search_where_uses_only_verified_predicates_on_declared_index_properties(query, index):
    where = search_where(query)
    predicates = [p.strip() for p in re.split(r"\s+AND\s+", where)]
    for p in predicates:
        assert PREDICATE_RE.match(p), f"predicate outside the 2026.05 grammar: {p!r}"
        prop = p.split()[0].split(".")[1]
        assert prop in DECLARED_FILTERS[index], f"{prop} is not declared WITH on {index}"
    for banned in (" IN ", " OR ", "NOT ", "<>", "IS NULL", "IS NOT NULL"):
        assert banned not in where


# --- C2.1: the metrics query selects the unit ---

def test_metrics_query_selects_unit():
    assert "m.unit AS unit" in METRICS_QUERY
    assert "ORDER BY m.period_end DESC LIMIT 20" in METRICS_QUERY


def test_metric_units_flow_through_hybrid_result():
    rows = [{"company": "TSMC", "metric": "revenue", "value": 1.0, "unit": "TWD",
             "period_start": "2024-01-01", "period_end": "2024-12-31"}]
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", FakeDriver({"metrics": rows}),
                          FakeEmbedder())
    assert out["metrics"] == rows


# --- C2.5: anchor honesty ---

def test_anchor_defaulted_true_when_no_company_detected(caplog):
    d = FakeDriver()
    with caplog.at_level(logging.INFO, logger="semigraph.retrieval"):
        out = hybrid_retrieve("nothing about semiconductors here", d, FakeEmbedder())
    assert out["anchor_defaulted"] is True
    assert out["anchors"] == {}                      # v1 shape kept: Nvidia is NOT injected
    assert d.of("metrics")[0]["ids"] == [DEFAULT_ANCHOR_CIK]   # ... but v1 behaviour kept
    assert d.of("rules")[0]["ids"] == [DEFAULT_ANCHOR_CIK]
    assert [p["cik"] for p in d.of("risks")] == [DEFAULT_ANCHOR_CIK]
    infos = [r for r in caplog.records
             if r.levelno == logging.INFO and "anchor" in r.getMessage().lower()]
    assert infos and "default" in infos[0].getMessage().lower()


def test_anchor_defaulted_false_when_company_detected(caplog):
    with caplog.at_level(logging.INFO, logger="semigraph.retrieval"):
        out = hybrid_retrieve("How does Nvidia depend on TSMC?", FakeDriver(), FakeEmbedder())
    assert out["anchor_defaulted"] is False
    assert out["anchors"] == {"Nvidia": NVDA, "TSMC": TSMC}
    assert not [r for r in caplog.records if "default" in r.getMessage().lower()]


def test_hybrid_result_shape_is_v1_plus_anchor_defaulted_only():
    out = hybrid_retrieve("How does Nvidia depend on TSMC?", FakeDriver(), FakeEmbedder())
    assert set(out) == {"anchors", "edges", "metrics", "risks", "temporal", "chunks",
                        "anchor_defaulted"}
    assert out["edges"] == [] and out["risks"] == []


def test_hybrid_issues_expected_query_set_and_temporal_unchanged():
    d = FakeDriver()
    hybrid_retrieve("How does Nvidia depend on TSMC?", d, FakeEmbedder())
    kinds = [d.classify(q) for q, _ in d.calls]
    assert kinds.count("company_edges") == kinds.count("rules") == kinds.count("metrics") == 1
    assert kinds.count("temporal") == kinds.count("excerpts") == 1
    assert kinds.count("risks") == 2 and "unknown" not in kinds
    assert d.of("temporal") == [{"ids": [NVDA, TSMC]}]
