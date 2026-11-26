"""I3b (docs/specs/m2-search.md, owner: "have the reranker", "Local first, hosted later"): the local rerank stage. No database."""

import ast
import logging
from pathlib import Path

import pytest
from test_retrieval_fusion import Driver, Embedder, Q2, row

from semigraph.retrieval import fusion, rerank
from semigraph.retrieval import retriever as R
from semigraph.retrieval.fusion import FusionConfig, SlotPolicy, select_slots

SRC = Path(__file__).resolve().parents[1] / "src" / "semigraph"
MODELS = Path(__file__).resolve().parents[1] / "models"
MINILM = MODELS / "reranker-minilm-l6" / "onnx" / "model_quint8_avx2.onnx"
MXBAI = MODELS / "reranker-mxbai-xsmall" / "onnx" / "model_quantized.onnx"


class FakeReranker:
    """Scores a passage by a number written into its text ("score=7"); records every call."""

    def __init__(self):
        self.calls = []

    def rerank(self, query, candidates):
        self.calls.append((query, list(candidates)))
        return [float(c.rsplit("score=", 1)[1]) if "score=" in c else 0.0 for c in candidates]


def scored_row(cid, score, acc=None):
    r = row(cid, acc or f"A-{cid}")
    return {**r, "text": f"text {cid} score={score}"}


def retrieve(reranker, cfg, **layers):
    d = Driver(**layers)
    return R.hybrid_retrieve(Q2, d, Embedder(), fusion=cfg, reranker=reranker), d


# --- the cross-encoder itself (skipped without the downloaded models) ---

@pytest.mark.skipif(not MINILM.exists(), reason="reranker-minilm-l6 not downloaded")
def test_local_cross_encoder_scores_and_orders():
    enc = rerank.get_reranker("minilm", MODELS)
    assert enc is not None
    q = "Which supplier manufactures Nvidia's advanced GPUs?"
    passages = ["Nvidia relies on TSMC to fabricate its leading-edge GPU wafers.",
                "The company's headquarters lease expires in 2031 and covers office space in Santa Clara.",
                "Quarterly dividends are declared at the discretion of the board of directors."]
    scores = enc.rerank(q, passages)
    assert len(scores) == 3 and all(isinstance(s, float) for s in scores)
    assert scores[0] > scores[1] and scores[0] > scores[2]


@pytest.mark.skipif(not MXBAI.exists(), reason="reranker-mxbai-xsmall not downloaded")
def test_mxbai_feeds_only_the_inputs_the_graph_declares():
    enc = rerank.get_reranker("mxbai", MODELS)
    assert enc is not None
    names = {i.name for i in enc._session.get_inputs()}
    assert "token_type_ids" not in names or names >= {"input_ids", "attention_mask"}
    scores = enc.rerank("TSMC wafer supply", ["Nvidia depends on TSMC for wafer supply.", "Office lease in Santa Clara."])
    assert scores[0] > scores[1]


@pytest.mark.skipif(not MINILM.exists(), reason="reranker-minilm-l6 not downloaded")
def test_rerank_is_deterministic():
    enc = rerank.get_reranker("minilm", MODELS)
    cands = [f"Passage {i} about export controls on advanced chips and TSMC supply " * (i + 1) for i in range(5)]
    first, second = enc.rerank("export controls", cands), enc.rerank("export controls", cands)
    assert first == second
    assert enc.rerank("export controls", cands[:2]) == pytest.approx(first[:2], abs=1e-4)   # batch composition does not move scores


def test_rerank_long_passage_is_truncated_not_an_error():
    if not MINILM.exists():
        pytest.skip("reranker-minilm-l6 not downloaded")
    enc = rerank.get_reranker("minilm", MODELS)
    assert len(enc.rerank("q", ["word " * 20_000])) == 1


# --- the stage inside hybrid_retrieve ---

def test_rerank_reorders_the_fused_candidates():
    cfg = FusionConfig(rerank="minilm", rerank_depth=5)
    rows = [scored_row("a", 1), scored_row("b", 9), scored_row("c", 5)]
    fake = FakeReranker()
    out, _ = retrieve(fake, cfg, fusion_mention=rows)
    assert [c["chunk_id"] for c in out["chunks"]] == ["b", "c", "a"]
    assert fake.calls[0][0] == Q2 and len(fake.calls[0][1]) == 3


def test_rerank_keeps_slot_guarantees():
    mention = [scored_row(f"m{i}", 100 - i) for i in range(10)]      # the reranker loves every mention row
    filer = {1045810: [scored_row("fN", 0)], 1046179: [scored_row("fT", 0)]}   # and scores both filer-own rows lowest
    cfg = FusionConfig(filer=True, rerank="minilm", rerank_depth=3)       # depth 3 would not even reach the filer rows
    out, _ = retrieve(FakeReranker(), cfg, fusion_mention=mention, filer=lambda p: filer[p["cik"]])
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert {"fN", "fT"} <= set(ids) and len(ids) <= 8
    assert ids[:1] == ["m0"]                                                 # reranked order, not the fused order, leads


def test_rerank_decides_which_rows_fill_the_guaranteed_slots():
    mention = [scored_row(f"m{i}", 100 - i) for i in range(10)]
    filer = {cik: [scored_row(f"{n}1", 0), scored_row(f"{n}2", 1), scored_row(f"{n}3", 50)] for cik, n in ((1045810, "fN"), (1046179, "fT"))}
    cfg = FusionConfig(filer=True, rerank="minilm", rerank_depth=20)
    out, _ = retrieve(FakeReranker(), cfg, fusion_mention=mention, filer=lambda p: filer[p["cik"]])
    ids = {c["chunk_id"] for c in out["chunks"]}
    assert {"fN3", "fN2", "fT3", "fT2"} <= ids and not ({"fN1", "fT1"} & ids)   # the leg's #3 outscores #1: it wins the slot


def test_heads_sort_by_rerank_score_and_keep_leg_order_beyond_the_head():
    got = R._by_rerank(["a", "b", "c", "d", "e"], {"a": 0.0, "b": 1.0, "c": 5.0, "d": 9.0})
    assert got == ["c", "b", "a", "d", "e"]


def test_no_text_candidates_return_the_fused_order_and_no_seconds(monkeypatch):
    monkeypatch.setattr(R, "hydrate", lambda driver, ids: {})
    rows = [{**row("a", "A-a"), "text": None}, {**row("b", "A-b"), "text": None}]
    fake = FakeReranker()
    out, _ = retrieve(fake, FusionConfig(rerank="minilm"), fusion_mention=rows)
    assert fake.calls == [] and "rerank_s" not in out


def test_select_slots_keeps_the_given_score_order():
    rows = {c: row(c, acc=c) for c in "xyz"}
    got = select_slots([("z", 9.0), ("x", 3.0), ("y", -1.0)], rows, filer_lists=[], mention_list=[], policy=SlotPolicy())
    assert [c for c, _ in got] == ["z", "x", "y"]


def test_rerank_off_is_the_fused_order_and_never_calls_the_reranker():
    rows = [scored_row("a", 1), scored_row("b", 9)]
    fake = FakeReranker()
    out, _ = retrieve(fake, FusionConfig(), fusion_mention=rows)
    assert [c["chunk_id"] for c in out["chunks"]] == ["a", "b"] and fake.calls == []
    assert "rerank_s" not in out


def test_rerank_records_its_seconds_and_scores_become_the_chunk_scores():
    out, _ = retrieve(FakeReranker(), FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 2.5)])
    assert out["rerank_s"] >= 0 and out["chunks"][0]["score"] == 2.5


def test_rerank_failure_falls_back_to_the_fused_order(caplog):
    class Broken:
        def rerank(self, q, c):
            raise RuntimeError("boom")

    rows = [scored_row("a", 1), scored_row("b", 9)]
    with caplog.at_level(logging.WARNING):
        out, _ = retrieve(Broken(), FusionConfig(rerank="minilm"), fusion_mention=rows)
    assert [c["chunk_id"] for c in out["chunks"]] == ["a", "b"] and "rerank" in caplog.text


def test_wrong_score_count_falls_back_to_the_fused_order():
    class Short:
        def rerank(self, q, c):
            return [1.0]

    out, _ = retrieve(Short(), FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 1), scored_row("b", 9)])
    assert [c["chunk_id"] for c in out["chunks"]] == ["a", "b"]


# --- a missing model never crashes ---

def test_missing_model_disables_rerank(tmp_path, caplog):
    rerank.get_reranker.cache_clear()
    with caplog.at_level(logging.WARNING):
        assert rerank.get_reranker("minilm", tmp_path) is None
    assert "minilm" in caplog.text and "disabled" in caplog.text


def test_missing_model_serves_the_fused_order(tmp_path, monkeypatch):
    rerank.get_reranker.cache_clear()
    monkeypatch.setattr(rerank, "default_root", lambda: tmp_path)
    rows = [scored_row("a", 1), scored_row("b", 9)]
    d = Driver(fusion_mention=rows)
    out = R.hybrid_retrieve(Q2, d, Embedder(), fusion=FusionConfig(rerank="minilm"))
    assert [c["chunk_id"] for c in out["chunks"]] == ["a", "b"] and "rerank_s" not in out
    rerank.get_reranker.cache_clear()


def test_the_model_loads_once_per_process_and_only_when_asked(monkeypatch, tmp_path):
    rerank.get_reranker.cache_clear()
    loads = []
    monkeypatch.setattr(rerank, "LocalCrossEncoder", lambda *a, **k: loads.append(a) or object())
    (tmp_path / "reranker-minilm-l6" / "onnx").mkdir(parents=True)
    (tmp_path / "reranker-minilm-l6" / "onnx" / "model_quint8_avx2.onnx").write_bytes(b"x")
    (tmp_path / "reranker-minilm-l6" / "tokenizer.json").write_text("{}")
    assert loads == []                                                     # importing and configuring load nothing
    a, b = rerank.get_reranker("minilm", tmp_path), rerank.get_reranker("minilm", tmp_path)
    assert a is b and len(loads) == 1
    rerank.get_reranker.cache_clear()


def test_unknown_model_key_is_refused():
    with pytest.raises(ValueError):
        rerank.get_reranker("nope", Path("."))


# --- scope: never on the upload / workspace-document path ---

def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {node.module or "", *(f"{node.module or ''}.{a.name}" for a in node.names)}
    return names


def test_upload_path_has_no_reranker_import():
    files = [*(SRC / "uploads").rglob("*.py"), SRC / "retrieval" / "workspace.py", SRC / "retrieval" / "workspace_async.py",
             *(SRC / "serve").rglob("*.py")]
    assert len(files) > 5
    for f in files:
        bad = {n for n in _imports(f) if n.split(".")[-1] == "rerank" or n.endswith("retrieval.rerank")}
        assert not bad, f"{f.name} imports the reranker: {bad}"


def test_workspace_sec_part_never_reranks():
    rt = fusion.FusionRuntime(FusionConfig(filer=True, rerank="minilm", rerank_depth=20), index=object())
    assert fusion.sec_fusion_kwargs(rt)["fusion"].rerank is None
    assert fusion.sec_fusion_kwargs(rt, as_of="2025-01-01")["fusion"].rerank is None


# --- settings and the fingerprint ---

def test_settings_default_none_and_validated():
    from pydantic import ValidationError

    from semigraph.config import Settings

    s = Settings(_env_file=None)
    assert s.retrieval_rerank == "none" and isinstance(s.retrieval_rerank_model_dir, Path)
    assert Settings(_env_file=None, retrieval_rerank="mxbai").retrieval_rerank == "mxbai"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retrieval_rerank="cohere")


def test_serving_config_gains_the_rerank_only_when_the_setting_is_on(monkeypatch):
    from semigraph.config import Settings

    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: object())  # CI has no model files
    assert fusion.serving_config(Settings(_env_file=None)) == fusion.SERVING_CONFIG
    on = fusion.serving_config(Settings(_env_file=None, retrieval_rerank="minilm"))
    assert on.rerank == "minilm" and on.rerank_depth == fusion.RERANK_DEPTH and fusion.SERVING_CONFIG.rerank is None


def test_rerank_setting_moves_the_serving_digest(monkeypatch):
    from semigraph.config import Settings

    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: object())
    off = fusion.serving_digest()
    monkeypatch.setattr("semigraph.config.get_settings", lambda: Settings(_env_file=None, retrieval_rerank="minilm"))
    assert fusion.serving_digest() != off


def test_flag_off_fingerprint_is_untouched():
    from semigraph.retrieval.answerer import template_fingerprint

    assert template_fingerprint() == "2a6910aa5d"


def test_missing_model_gives_the_fingerprint_with_rerank_off(monkeypatch):
    from semigraph.config import Settings
    from semigraph.retrieval.answerer import template_fingerprint

    off = template_fingerprint(fusion=True)
    monkeypatch.setattr("semigraph.config.get_settings", lambda: Settings(_env_file=None, retrieval_rerank="minilm"))
    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: None)
    assert template_fingerprint(fusion=True) == off                          # nothing reranks, so the key does not say it does
    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: object())
    assert template_fingerprint(fusion=True) != off


def test_build_fusion_runtime_drops_rerank_when_the_model_is_missing(monkeypatch):
    from test_serve_fusion_flag import Drv, SNAP, db_row

    from semigraph.config import Settings
    from semigraph.retrieval import keyword as K

    st = Settings(_env_file=None, retrieval_fusion=True, retrieval_rerank="minilm")
    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: None)
    assert K.build_fusion_runtime(Drv([db_row("c1", "export controls")]), st, SNAP).config.rerank is None
    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: object())
    assert K.build_fusion_runtime(Drv([db_row("c1", "export controls")]), st, SNAP).config.rerank == "minilm"


def test_model_loading_is_serialised(monkeypatch, tmp_path):
    import threading
    import time

    rerank.get_reranker.cache_clear()
    loads = []

    def slow(*a, **k):
        loads.append(1)
        time.sleep(0.2)
        return object()

    monkeypatch.setattr(rerank, "LocalCrossEncoder", slow)
    (tmp_path / "reranker-minilm-l6" / "onnx").mkdir(parents=True)
    (tmp_path / "reranker-minilm-l6" / "onnx" / "model_quint8_avx2.onnx").write_bytes(b"x")
    (tmp_path / "reranker-minilm-l6" / "tokenizer.json").write_text("{}")
    ts = [threading.Thread(target=rerank.get_reranker, args=("minilm", tmp_path)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(loads) == 1
    rerank.get_reranker.cache_clear()


def test_threads_setting_defaults_to_one_and_model_dir_is_absolute():
    from semigraph.config import Settings

    s = Settings(_env_file=None)
    assert s.retrieval_rerank_threads == 1 and s.retrieval_rerank_model_dir.is_absolute()


def test_the_bench_refuses_a_rerank_arm_without_its_model(monkeypatch):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("m2_bench_r", Path(__file__).resolve().parents[1] / "scripts" / "m2_bench.py")
    mb = importlib.util.module_from_spec(spec)
    sys.modules["m2_bench_r"] = mb
    spec.loader.exec_module(mb)
    monkeypatch.setattr(rerank, "get_reranker", lambda key, root=None, **kw: None)
    with pytest.raises(SystemExit, match="model"):
        mb.require_reranker(FusionConfig(rerank="minilm"))
    mb.require_reranker(FusionConfig())
    mb.require_reranker(None)
