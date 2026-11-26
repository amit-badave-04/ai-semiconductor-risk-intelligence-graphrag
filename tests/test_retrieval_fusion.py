"""I2 (docs/specs/m2-search.md R4-R7, R10, R11): the free dense legs, RRF and the slot policy. No database."""

import re
from datetime import date

import pytest
from agent_fakes import NVDA, TSMC
from agent_fakes import FakeDriver as _Base

from semigraph.retrieval import retriever as R
from semigraph.retrieval.answerer import build_blocks, template_fingerprint
from semigraph.retrieval.fusion import FusionConfig, SlotPolicy, rrf, select_slots

VEC = [0.1, 0.2, 0.3]
NEW = {R.FILER_EXCERPTS_QUERY: "filer", R.PERIOD_EXCERPTS_QUERY: "period", R.FUSION_MENTION_EXCERPTS_QUERY: "fusion_mention",
       # the current-only variants (I3a, decision 011: FusionConfig.current_only is on by default) answer as the plain legs
       R.FILER_CURRENT_EXCERPTS_QUERY: "filer", R.PERIOD_CURRENT_EXCERPTS_QUERY: "period"}


class Driver(_Base):
    NAMES = _Base.NAMES + ("filer", "period", "fusion_mention")

    def __init__(self, **layers):
        super().__init__(**layers)
        self._names = {**self._names, **NEW}


class Embedder:
    def __init__(self):
        self.calls = 0

    def encode_query(self, q):
        self.calls += 1
        return list(VEC)


def row(cid, acc="A1", digest=None, score=0.5):
    return {"chunk_id": cid, "score": score, "text": f"text {cid}", "source_url": f"u/{cid}",
            "content_hash": digest or f"h-{cid}", "accession_no": acc}


def search_where(q):
    return re.search(r"WHERE\s+(.*?)\s+LIMIT\s+\$candidates", q, re.S).group(1)


Q2 = "How does Nvidia depend on TSMC?"


def run(question=Q2, cfg=None, **layers):
    d, e = Driver(**layers), Embedder()
    out = R.hybrid_retrieve(question, d, e, fusion=cfg)
    return out, d, e


# --- flag off: today's exact path ---

def test_fusion_off_runs_same_queries_same_params():
    out, d, _ = run(excerpts=[row("c1")])
    assert [n for n in d.names() if n in {"excerpts", "filer", "period", "fusion_mention"}] == ["excerpts"]
    (p,) = d.params_of("excerpts")
    assert p == {"ids": [NVDA, TSMC], "vec": VEC, "k": 8, "candidates": R.EXCERPT_CANDIDATES}
    assert "leg_ranks" not in out
    assert out["chunks"][0]["chunk_id"] == "c1"


def test_fusion_off_query_sequence_is_the_golden_list():
    """The flag-off path runs exactly today's query names, in today's order, for a fixed question (a literal list)."""
    _, d, e = run(excerpts=[row("c1")])
    assert e.calls == 1
    assert d.names() == ["company_edges", "rule_edges", "metrics", "active_risks", "active_risks", "temporal", "excerpts"]
    assert d.params_of("excerpts") == [{"ids": [NVDA, TSMC], "vec": VEC, "k": 8, "candidates": R.EXCERPT_CANDIDATES}]


def test_fingerprint_is_2a6910aa5d_with_fusion_off():
    assert template_fingerprint() == "2a6910aa5d"


# --- the new queries ---

def test_filer_leg_has_no_mentions_hop():
    q = R.FILER_EXCERPTS_QUERY
    assert "MENTIONS" not in q
    assert search_where(q) == "node.retrievable = true AND node.filer_cik = $cik"
    assert " IN " not in search_where(q)
    for col in ("chunk_id", "score", "text", "source_url", "content_hash", "accession_no"):
        assert re.search(rf"\b{col}\b", q)


def test_period_query_uses_a_date_window_in_index():
    w = search_where(R.PERIOD_EXCERPTS_QUERY)
    assert w == "node.retrievable = true AND node.filer_cik = $cik AND node.valid_from >= $since AND node.valid_from < $until"
    assert "MENTIONS" not in R.PERIOD_EXCERPTS_QUERY


def test_mention_fusion_query_is_the_mention_shape_plus_hash_and_accession():
    q = R.FUSION_MENTION_EXCERPTS_QUERY
    assert search_where(q) == "node.retrievable = true" and "(node)-[:MENTIONS]->(c:Company) WHERE c.cik IN $ids" in q
    assert "content_hash" in q and "accession_no" in q
    assert "content_hash" not in R.EXCERPTS_QUERY and R.EXCERPTS_QUERY != q


# --- legs on one vector ---

def test_legs_reuse_one_query_vector():
    cfg = FusionConfig(filer=True, period=True)
    _, d, e = run("Nvidia and TSMC risks in fiscal 2024", cfg, fusion_mention=[row("m1")], filer=[row("f1")], period=[row("p1")])
    assert e.calls == 1
    for n in ("fusion_mention", "filer", "period"):
        assert all(p["vec"] == VEC for p in d.params_of(n))
    assert d.names().count("filer") == 2 and d.names().count("period") == 2
    assert [p["cik"] for p in d.params_of("filer")] == [NVDA, TSMC]


def test_no_filer_leg_for_defaulted_anchor():
    out, d, _ = run("what are the main risks?", FusionConfig(filer=True), fusion_mention=[row("m1")])
    assert out["anchor_defaulted"] and "filer" not in d.names()


def test_period_leg_only_with_mentioned_periods():
    cfg = FusionConfig(filer=True, period=True)
    _, d, _ = run(Q2, cfg, fusion_mention=[row("m1")])
    assert "period" not in d.names() and "filer" in d.names()
    _, d, _ = run("Nvidia supply risk in FY2024", cfg, fusion_mention=[row("m1")])
    (p,) = d.params_of("period")
    assert p["cik"] == NVDA and p["since"] == date(2023, 1, 1) and p["until"] == date(2025, 7, 1)


def test_period_window_catches_quarters_of_a_fiscal_year_filed_in_the_prior_calendar_year():
    """Nvidia's fiscal 2025 ends in January 2025: its Q1-Q3 10-Qs were filed in calendar 2024."""
    since, until = R._period_window({"years": [2025], "dates": []})
    assert since <= date(2024, 5, 29) and until > date(2025, 2, 26)
    assert R._period_window({"years": [], "dates": []}) is None
    assert R._period_window({"years": [], "dates": ["2024-01-28"]}) == (date(2023, 1, 1), date(2025, 7, 1))


def test_disabled_legs_do_not_run_and_mention_off_skips_its_query():
    _, d, _ = run(Q2, FusionConfig(mention=False, filer=True), filer=[row("f1")])
    assert "fusion_mention" not in d.names() and "excerpts" not in d.names()


# --- fused output ---

def test_fused_chunks_replace_slots_and_render_unchanged():
    cfg = FusionConfig(filer=True)
    out, d, _ = run(Q2, cfg, fusion_mention=[row("m1", "A1"), row("m2", "A2")],
                    filer=lambda p: [row(f"f{p['cik']}", "A3")])
    assert d.params_of("excerpts") == []
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert set(ids) == {"m1", "m2", f"f{NVDA}", f"f{TSMC}"} and len(ids) <= 8
    assert set(out["chunks"][0]) == {"chunk_id", "score", "text", "source_url"}
    assert out["leg_ranks"]["mention"] == ["m1", "m2"]
    assert out["leg_ranks"][f"filer:{NVDA}"] == [f"f{NVDA}"]
    build_blocks(out)                    # ignores the new key


def test_leg_ranks_are_logged_to_depth_50_and_fused_to_depth_20():
    rows = [row(f"m{i:02d}", acc=f"A{i}") for i in range(60)]
    out, d, _ = run(Q2, FusionConfig(), fusion_mention=rows)
    assert len(out["leg_ranks"]["mention"]) == 50
    assert d.params_of("fusion_mention")[0]["k"] == 50


# --- pure: rrf and slots ---

def test_rrf_deterministic_on_ties():
    a = rrf({"x": ["b", "a"], "y": ["a", "b"]})
    assert [c for c, _ in a] == ["a", "b"] and a[0][1] == a[1][1]
    assert rrf({"y": ["a", "b"], "x": ["b", "a"]}) == a
    assert rrf({"x": ["z"]})[0][1] == pytest.approx(1 / 61)


def test_rrf_only_counts_the_fusion_depth():
    got = dict(rrf({"x": [f"c{i}" for i in range(30)]}, depth=20))
    assert len(got) == 20


def _rows(*items):
    return {cid: row(cid, acc, h) for cid, acc, h in items}


def test_slots_never_exceed_k_chunks():
    ids = [f"c{i:02d}" for i in range(30)]
    rows = {c: row(c, acc=f"A{i}") for i, c in enumerate(ids)}
    got = select_slots(rrf({"m": ids}), rows, filer_lists=[ids[:5]], mention_list=ids, policy=SlotPolicy())
    assert len(got) == 8
    assert len(select_slots(rrf({"m": ids}), rows, filer_lists=[], mention_list=ids, policy=SlotPolicy(k_chunks=3))) == 3


@pytest.mark.parametrize("n_anchors,per", [(1, 2), (2, 2), (3, 1), (4, 1)])
def test_guaranteed_filer_slots_per_anchor(n_anchors, per):
    mention = [f"m{i}" for i in range(10)]            # would fill all 8 slots on their own
    filers = [[f"f{a}-{i}" for i in range(5)] for a in range(n_anchors)]
    rows = {c: row(c, acc=c) for c in mention + [c for f in filers for c in f]}
    got = {c for c, _ in select_slots(rrf({"m": mention}), rows, filer_lists=filers, mention_list=mention, policy=SlotPolicy())}
    for f in filers:
        assert len(got & set(f)) == per
    assert len(got & set(mention)) == 8 - per * n_anchors >= 2


def test_two_mention_slots_are_guaranteed_against_filer_dominance():
    mention = ["m0", "m1", "m2"]
    filer = [f"f{i}" for i in range(20)]
    rows = {c: row(c, acc=c) for c in mention + filer}
    fused = rrf({"f": filer, "m": mention}, depth=20)
    got = {c for c, _ in select_slots(fused, rows, filer_lists=[filer], mention_list=mention, policy=SlotPolicy())}
    assert {"m0", "m1"} <= got


def test_accession_cap_is_three():
    ids = [f"c{i}" for i in range(10)]
    rows = {c: row(c, acc="SAME") for c in ids}
    assert len(select_slots(rrf({"m": ids}), rows, filer_lists=[], mention_list=ids, policy=SlotPolicy())) == 3


def test_dedup_on_content_hash():
    rows = _rows(("a", "A1", "H"), ("b", "A2", "H"), ("c", "A3", "K"))
    got = [c for c, _ in select_slots(rrf({"m": ["a", "b", "c"]}), rows, filer_lists=[], mention_list=["a", "b", "c"],
                                      policy=SlotPolicy())]
    assert got == ["a", "c"]


def test_ties_by_chunk_id_in_the_output_order():
    rows = _rows(("b", "A1", "1"), ("a", "A2", "2"))
    got = select_slots([("b", 0.5), ("a", 0.5)], rows, filer_lists=[], mention_list=[], policy=SlotPolicy())
    assert [c for c, _ in got] == ["a", "b"]


def _legs_with_equal_rank_multisets(first, second):
    """``first`` sits at ranks (1, 2, 8) of three legs and ``second`` at (8, 1, 2): the same reciprocals summed in a
    different order, which differ by one ulp in floating point (1/61 + 1/62 + 1/68)."""
    def leg(n, rank_first, rank_second):
        ids = [f"f{n}-{i}" for i in range(1, 9)]
        ids[rank_first - 1], ids[rank_second - 1] = first, second
        return ids
    return {"1": leg(1, 1, 8), "2": leg(2, 2, 1), "3": leg(3, 8, 2)}


@pytest.mark.parametrize("first,second", [("a", "z"), ("z", "a")])
def test_rrf_ties_are_by_chunk_id_even_when_float_sums_differ_by_an_ulp(first, second):
    got = [c for c, _ in rrf(_legs_with_equal_rank_multisets(first, second))]
    assert got.index("a") < got.index("z")


def test_superseded_excerpts_are_reported_not_hidden():
    """freshness keeps a superseded filing's risk section retrievable; the fusion legs return is_current so it is visible."""
    for q in (R.FILER_EXCERPTS_QUERY, R.PERIOD_EXCERPTS_QUERY, R.FUSION_MENTION_EXCERPTS_QUERY):
        assert "node.is_current AS is_current" in q
    old = {**row("old1", "A0"), "is_current": False}
    new = {**row("new1", "A1"), "is_current": True}
    out, _, _ = run(Q2, FusionConfig(), fusion_mention=[old, new])
    assert out["superseded_ids"] == ["old1"]
    assert set(out["chunks"][0]) == {"chunk_id", "score", "text", "source_url"}


def test_the_period_query_comment_records_the_date_range_probe():
    """The DATE range in the index WHERE was probed on G (Neo4j 2026.07.1, the production version), 2026-11-26."""
    src = open(R.__file__, encoding="utf-8").read()
    i = src.index("PERIOD_EXCERPTS_QUERY = ")
    note = src[i - 600:i]
    assert "Probed read-only" in note and "2026.07.1" in note and "UNVERIFIED" not in note


def test_workspace_path_embeds_the_question_once(monkeypatch):
    from test_retrieval_workspace import _sec_retrieval

    from semigraph.retrieval import workspace as ws
    seen = {}

    class Stop(Exception):
        pass

    def hybrid(question, driver, embedder, **kw):
        seen["hybrid"] = kw.get("query_vec")
        return _sec_retrieval()

    def wsr(question, wid, driver, embedder, **kw):
        seen["ws"] = kw.get("query_vec")
        raise Stop

    monkeypatch.setattr(ws, "hybrid_retrieve", hybrid)
    monkeypatch.setattr(ws, "workspace_retrieve", wsr)
    e = Embedder()
    with pytest.raises(Stop):
        next(ws.stream_workspace_answer("q", object(), e, workspace_id="w" * 12))
    assert e.calls == 1 and seen == {"hybrid": VEC, "ws": VEC}


def test_chunk_ranks_labels_an_rrf_score_on_the_fused_path():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import compare_retrieval as cr
    plain = {"chunks": [{"chunk_id": "c", "score": 0.9}]}
    assert cr.chunk_ranks(plain) == [{"rank": 1, "chunk_id": "c", "score": 0.9}]
    assert cr.chunk_ranks({**plain, "leg_ranks": {}})[0]["score_kind"] == "rrf"
