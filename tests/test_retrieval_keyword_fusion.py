"""I3a (docs/specs/m2-search.md R6, R8, R10, R12, S7; decision 011): the keyword legs inside ``hybrid_retrieve(fusion=...)``,
current-only legs, hydration of the keyword winners and the excerpt size bound. No database: a fake driver."""

import re

import pytest
from agent_fakes import NVDA, TSMC
from agent_fakes import FakeDriver as _Base

from semigraph.retrieval import keyword as K
from semigraph.retrieval import retriever as R
from semigraph.retrieval.answerer import build_blocks
from semigraph.retrieval.fusion import EXCERPT_MAX_CHARS, FusionConfig
from semigraph.serve import estimate

VEC = [0.1, 0.2, 0.3]
QUERIES = {R.FILER_EXCERPTS_QUERY: "filer", R.PERIOD_EXCERPTS_QUERY: "period", R.FUSION_MENTION_EXCERPTS_QUERY: "fusion_mention",
           R.FILER_CURRENT_EXCERPTS_QUERY: "filer_current", R.PERIOD_CURRENT_EXCERPTS_QUERY: "period_current",
           R.FUSION_MENTION_CURRENT_EXCERPTS_QUERY: "mention_current", K.HYDRATE_QUERY: "hydrate"}


class Driver(_Base):
    NAMES = _Base.NAMES + tuple(QUERIES.values())

    def __init__(self, **layers):
        super().__init__(**layers)
        self._names = {**self._names, **QUERIES}


class Embedder:
    def __init__(self):
        self.calls = 0

    def encode_query(self, q):
        self.calls += 1
        return list(VEC)


def dense(cid, acc="A1", digest=None, text=None, current=True):
    return {"chunk_id": cid, "score": 0.5, "text": text or f"dense text {cid}", "source_url": f"u/{cid}",
            "content_hash": digest or f"h-{cid}", "accession_no": acc, "is_current": current}


def kw(cid, text, *, filer=NVDA, acc=None, current=True, mentions=(), context=""):
    return {"chunk_id": cid, "text": text, "filer_cik": filer, "form": "10-K", "accession_no": acc or f"K-{cid}",
            "content_hash": f"kh-{cid}", "valid_from": "2025-02-26", "is_current": current, "mention_ciks": list(mentions),
            "context": context}


def hydrator(store):
    """What HYDRATE_QUERY answers: the stored spans that are retrievable and asked for."""
    return lambda p: [{"chunk_id": i, "text": store[i]["text"], "source_url": f"u/{i}"} for i in p["ids"]
                      if i in store and store[i].get("retrievable", True)]


Q2 = "How does Nvidia depend on TSMC?"


def run(question=Q2, cfg=None, index=None, **layers):
    d, e = Driver(**layers), Embedder()
    return R.hybrid_retrieve(question, d, e, fusion=cfg, keyword_index=index), d, e


SPANS = [kw("k1", "Nvidia depends on a single foundry for CoWoS packaging", filer=NVDA),
         kw("k2", "TSMC capacity for CoWoS is constrained", filer=TSMC)]
STORE = {"k1": {"text": "Nvidia depends on a single foundry for CoWoS packaging"}, "k2": {"text": "TSMC capacity for CoWoS"}}


# --- the keyword legs fuse and hydrate ---

def test_keyword_legs_fuse_into_the_slots_and_only_their_winners_are_hydrated():
    idx = K.build_index(SPANS, with_context=False)
    out, d, e = run("How does Nvidia depend on TSMC cowos?", FusionConfig(keyword=True), idx,
                    fusion_mention=[dense("m1")], hydrate=hydrator(STORE))
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert {"m1", "k1", "k2"} <= set(ids) and len(ids) <= 8
    assert [c["text"] for c in out["chunks"] if c["chunk_id"] == "k1"] == [STORE["k1"]["text"]]
    assert set(out["leg_ranks"]) == {"mention", f"keyword:{NVDA}", f"keyword:{TSMC}", "keyword:mention"}
    assert out["leg_ranks"][f"keyword:{NVDA}"] == ["k1"] and out["leg_ranks"][f"keyword:{TSMC}"] == ["k2"]
    assert d.names().count("hydrate") == 1 and sorted(d.params_of("hydrate")[0]["ids"]) == ["k1", "k2"]   # not the dense m1
    assert e.calls == 1                                        # no extra embedding (S7)
    assert set(out["chunks"][0]) == {"chunk_id", "score", "text", "source_url"}
    build_blocks(out)


def test_no_hydration_query_when_no_keyword_winner():
    unrelated = K.build_index([kw("z1", "zzz yyy")], with_context=False)
    out, d, _ = run(Q2, FusionConfig(keyword=True), unrelated, fusion_mention=[dense("m1")], hydrate=hydrator(STORE))
    assert "hydrate" not in d.names() and [c["chunk_id"] for c in out["chunks"]] == ["m1"]      # nothing matches
    _, d, _ = run(Q2, FusionConfig(), fusion_mention=[dense("m1")])
    assert "hydrate" not in d.names()


def test_hydration_drops_a_winner_that_is_no_longer_retrievable():
    idx = K.build_index(SPANS, with_context=False)
    store = {**STORE, "k1": {**STORE["k1"], "retrievable": False}}
    out, _, _ = run("Nvidia TSMC cowos packaging foundry", FusionConfig(keyword=True), idx, fusion_mention=[dense("m1")],
                    hydrate=hydrator(store))
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert "k1" not in ids and "k2" in ids and "m1" in ids


def test_keyword_legs_need_an_index_and_a_matching_context_setting():
    out, d, _ = run(Q2, FusionConfig(keyword=True), None, fusion_mention=[dense("m1")])      # no index: legs are skipped
    assert list(out["leg_ranks"]) == ["mention"] and "hydrate" not in d.names()
    with pytest.raises(ValueError):                           # a config bug, not a runtime condition
        run(Q2, FusionConfig(keyword=True, keyword_context=True), K.build_index(SPANS, with_context=False))


def test_context_field_never_rendered():
    spans = [kw("k1", "We rely on one foundry.", context="zzctxmark Nvidia NVDA 10-K 2025")]
    idx = K.build_index(spans, with_context=True)
    out, d, _ = run("Nvidia zzctxmark", FusionConfig(keyword=True, keyword_context=True), idx, fusion_mention=[],
                    hydrate=hydrator({"k1": {"text": "We rely on one foundry."}}))
    assert [c["chunk_id"] for c in out["chunks"]] == ["k1"]
    _, full_context, _ = build_blocks(out)
    assert "zzctxmark" not in repr(out["chunks"]) and "zzctxmark" not in repr(out["leg_ranks"])
    assert "zzctxmark" not in full_context and "one foundry" in full_context
    assert "zzctxmark" not in repr(d.calls)                   # nor in any query parameter but the ids


def test_defaulted_anchor_has_no_keyword_filer_leg_but_keeps_the_mention_leg():
    idx = K.build_index([kw("k1", "export controls matter", filer=NVDA, mentions=[NVDA])], with_context=False)
    out, _, _ = run("what are export controls?", FusionConfig(keyword=True), idx, fusion_mention=[],
                    hydrate=hydrator({"k1": {"text": "export controls matter"}}))
    assert out["anchor_defaulted"] and list(out["leg_ranks"]) == ["mention", "keyword:mention"]
    assert out["leg_ranks"]["keyword:mention"] == ["k1"]


def test_a_huge_question_is_capped_before_the_index():
    idx = K.build_index(SPANS, with_context=False)
    out, _, _ = run("cowos " + "lorem ipsum " * 2_000, FusionConfig(keyword=True), idx, fusion_mention=[],
                    hydrate=hydrator(STORE))
    assert out["leg_ranks"]["keyword:mention"] == [] and out["anchor_defaulted"]    # no anchor in the question: Nvidia default


# --- current-only legs (decision 011): a pre-filter inside each leg's query ---

def where(q):
    return re.search(r"WHERE\s+(.*?)\s+LIMIT\s+\$candidates", q, re.S).group(1)


def test_current_query_variants_add_the_in_index_filter_and_nothing_else():
    for plain, current in ((R.FILER_EXCERPTS_QUERY, R.FILER_CURRENT_EXCERPTS_QUERY),
                           (R.PERIOD_EXCERPTS_QUERY, R.PERIOD_CURRENT_EXCERPTS_QUERY),
                           (R.FUSION_MENTION_EXCERPTS_QUERY, R.FUSION_MENTION_CURRENT_EXCERPTS_QUERY)):
        assert where(current) == where(plain).replace("node.retrievable = true", "node.retrievable = true AND node.is_current = true")
        assert current.replace(" AND node.is_current = true", "") == plain
        assert " IN " not in where(current) and " OR " not in where(current)


def test_current_only_drops_superseded_rows_from_new_legs():
    cfg = FusionConfig(filer=True, period=True)                                  # current_only is on by default
    _, d, _ = run("Nvidia supply risk in FY2024", cfg, fusion_mention=[dense("m1")], filer_current=[dense("f1")],
                  period_current=[dense("p1")])
    assert {"filer_current", "period_current", "fusion_mention"} <= set(d.names())
    assert not {"filer", "period", "mention_current"} & set(d.names())           # the mention leg is unchanged by current_only
    _, d, _ = run("Nvidia supply risk in FY2024", FusionConfig(filer=True, period=True, current_only=False),
                  fusion_mention=[dense("m1")], filer=[dense("f1")], period=[dense("p1")])
    assert {"filer", "period", "fusion_mention"} <= set(d.names()) and not {"filer_current", "period_current"} & set(d.names())
    _, d, _ = run(Q2, FusionConfig(mention_current_only=True), mention_current=[dense("m1")])
    assert d.names().count("mention_current") == 1 and "fusion_mention" not in d.names()


def test_current_only_keyword_legs_never_return_a_superseded_span_and_the_slots_still_fill():
    spans = [kw("old", "cowos packaging cowos packaging cowos", current=False)] + [
        kw(f"n{i}", "cowos packaging capacity", acc=f"K-n{i}") for i in range(10)]
    store = {s["chunk_id"]: {"text": s["text"]} for s in spans}
    idx = K.build_index(spans, with_context=False)
    on, _, _ = run("nvidia cowos packaging", FusionConfig(keyword=True), idx, fusion_mention=[], hydrate=hydrator(store))
    assert "old" not in [c["chunk_id"] for c in on["chunks"]] and len(on["chunks"]) == 8
    assert "old" not in on["leg_ranks"][f"keyword:{NVDA}"] and "old" not in on["leg_ranks"]["keyword:mention"]
    off, _, _ = run("nvidia cowos packaging", FusionConfig(keyword=True, current_only=False), idx, fusion_mention=[],
                    hydrate=hydrator(store))
    assert "old" in off["leg_ranks"][f"keyword:{NVDA}"] and "old" in off["superseded_ids"]


def test_flag_off_still_runs_todays_single_excerpt_query():
    out, d, _ = run(Q2, None, None, excerpts=[dense("c1")])
    assert d.names()[-1] == "excerpts" and "leg_ranks" not in out and not {*QUERIES.values()} & set(d.names())


# --- R12: the excerpt size bound ---

def test_fused_excerpt_chars_within_estimate():
    assert EXCERPT_MAX_CHARS == estimate.CHUNK_TEXT_MAX_CHARS
    long = "x" * 20_000
    mention = [dense(f"m{i}", acc=f"A{i}", text=long) for i in range(12)]
    spans = [kw(f"k{i}", "cowos packaging", acc=f"B{i}") for i in range(12)]
    store = {s["chunk_id"]: {"text": long} for s in spans}
    out, _, _ = run("nvidia cowos packaging", FusionConfig(filer=True, keyword=True), K.build_index(spans, with_context=False),
                    fusion_mention=mention, filer_current=[dense(f"f{i}", acc=f"C{i}", text=long) for i in range(12)],
                    hydrate=hydrator(store))
    assert len(out["chunks"]) <= estimate.K_CHUNKS == 8
    assert all(len(c["text"]) <= EXCERPT_MAX_CHARS for c in out["chunks"]) and out["chunks"]
    chunks_block = build_blocks(out)[0].chunks_block
    assert len(chunks_block) <= estimate.K_CHUNKS * (estimate.CHUNK_TEXT_MAX_CHARS + estimate.EXCERPT_FRAMING_CHARS)


def test_short_excerpts_are_untouched():
    out, _, _ = run(Q2, FusionConfig(), fusion_mention=[dense("m1", text="short text")])
    assert out["chunks"][0]["text"] == "short text"


def test_a_row_without_text_is_dropped_not_a_crash():
    picked = [("a", 0.9), ("b", 0.8)]
    rows = {"a": dense("a", text="x"), "b": {**dense("b"), "text": None}}
    out = R._with_text(Driver(), picked, {"a": {**rows["a"]}, "b": {**rows["b"], "text": ""}})
    assert [c["chunk_id"] for c in out] == ["a"]
    rows["b"]["text"] = None
    assert [c["chunk_id"] for c in R._with_text(Driver(hydrate=hydrator({"b": {"text": None}})), picked,
                                                {"a": rows["a"], "b": {k: v for k, v in rows["b"].items() if k != "text"}})] == ["a"]


def test_superseded_ids_list_only_excerpts_that_were_served():
    idx = K.build_index([kw("old", "cowos packaging foundry", current=False)], with_context=False)
    out, _, _ = run("Nvidia TSMC cowos packaging foundry", FusionConfig(keyword=True, current_only=False), idx,
                    fusion_mention=[dense("m1")], hydrate=hydrator({"old": {"text": "cowos", "retrievable": False}}))
    assert "old" not in [c["chunk_id"] for c in out["chunks"]] and "old" not in out["superseded_ids"]
