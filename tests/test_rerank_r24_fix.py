"""Decision 012 (R24 fix): the served rerank workload (192-token window, 25-candidate cap), the per-request time guard, the
onnxruntime session option, and the harnesses that must run with the guard off. No model files, no database."""

import ast
import hashlib
import importlib.util
import logging
import sys
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from test_retrieval_fusion import Driver, Embedder, Q2
from test_retrieval_rerank import FakeReranker, scored_row

from semigraph.retrieval import fusion, rerank
from semigraph.retrieval import retriever as R
from semigraph.retrieval.fusion import FusionConfig

ROOT = Path(__file__).resolve().parents[1]


def settings(**kw):
    from semigraph.config import Settings

    return Settings(_env_file=None, **kw)


def retrieve(reranker, cfg, rerank_budget_s=None, **layers):
    d = Driver(**layers)
    return R.hybrid_retrieve(Q2, d, Embedder(), fusion=cfg, reranker=reranker, rerank_budget_s=rerank_budget_s), d


@pytest.fixture
def fake_models(monkeypatch):
    """get_reranker as the serving config sees it, with no model files: records the window it is asked for."""
    asked = []
    monkeypatch.setattr(rerank, "get_reranker",
                        lambda key, root=None, threads=None, max_tokens=None: asked.append(max_tokens) or object())
    return asked


# --- 1. the served workload lives in FusionConfig / serving_config ---

def test_frozen_defaults_keep_todays_behaviour():
    c = FusionConfig()
    assert (c.rerank_depth, c.rerank_max_tokens, c.rerank_max_candidates) == (20, 384, None)
    assert c.rerank_max_tokens == rerank.MAX_TOKENS
    assert fusion.SERVING_CONFIG.rerank_max_candidates is None          # no rerank stage: nothing to cap


def test_serving_config_serves_192_tokens_and_25_candidates_at_depth_20(fake_models):
    on = fusion.serving_config(settings(retrieval_rerank="minilm"))
    assert (on.rerank, on.rerank_depth, on.rerank_max_tokens, on.rerank_max_candidates) == ("minilm", 20, 192, 25)
    assert fake_models == [192]                                         # the model is warmed at the served window


def test_the_served_workload_is_part_of_the_digest_and_the_fingerprint(fake_models, monkeypatch):
    from semigraph.retrieval.answerer import template_fingerprint

    monkeypatch.setattr("semigraph.config.get_settings", lambda: settings(retrieval_rerank="minilm"))
    served = fusion.serving_digest()
    frozen = replace(fusion.serving_config(settings(retrieval_rerank="minilm")), rerank_max_tokens=384, rerank_max_candidates=None)
    assert served != hashlib.sha256(repr(frozen).encode()).hexdigest()[:8]
    assert template_fingerprint(fusion=True) != "e5c72794de"            # the shipped examples carry the old one
    assert template_fingerprint(fusion=True) == "f604c90263"


def test_flag_off_fingerprint_is_still_2a6910aa5d(fake_models, monkeypatch):
    from semigraph.retrieval.answerer import template_fingerprint

    monkeypatch.setattr("semigraph.config.get_settings", lambda: settings(retrieval_rerank="minilm"))
    assert template_fingerprint() == template_fingerprint(fusion=False) == "2a6910aa5d"


# --- the cap: slot-guarantee heads first, then the fused order, lowest fused tail trimmed ---

def _fused(n):
    return [(f"f{i}", 1.0 - i / 100) for i in range(n)]


RANKED = {"mention": ["m1", "m2", "m3", "m4"], "filer:1": ["x1", "x2"], "keyword:1": ["k1"]}


def test_cap_keeps_the_heads_first_and_trims_the_fused_tail():
    cfg = FusionConfig(rerank="minilm", rerank_depth=20, rerank_max_candidates=10)
    assert R._rerank_candidates(_fused(30), RANKED, cfg) == ["m1", "m2", "m3", "x1", "x2", "f0", "f1", "f2", "f3", "f4"]


def test_a_head_that_is_also_in_the_fused_top_is_counted_once():
    ranked = {"mention": ["f2", "m2", "m3"]}
    cfg = FusionConfig(rerank="minilm", rerank_depth=20, rerank_max_candidates=4)
    assert R._rerank_candidates(_fused(30), ranked, cfg) == ["f2", "m2", "m3", "f0"]


def test_no_cap_is_todays_candidate_list():
    cfg = FusionConfig(rerank="minilm", rerank_depth=20)
    got = R._rerank_candidates(_fused(30), RANKED, cfg)
    assert got == [*(f"f{i}" for i in range(20)), "m1", "m2", "m3", "x1", "x2"]


def test_a_cap_above_the_candidate_count_changes_nothing():
    base = FusionConfig(rerank="minilm", rerank_depth=20)
    assert (R._rerank_candidates(_fused(30), RANKED, replace(base, rerank_max_candidates=25))
            == R._rerank_candidates(_fused(30), RANKED, base))


def test_the_cap_limits_what_the_reranker_scores():
    rows = [scored_row(f"m{i}", 10 - i) for i in range(10)]
    fake = FakeReranker()
    out, _ = retrieve(fake, FusionConfig(rerank="minilm", rerank_depth=20, rerank_max_candidates=4), fusion_mention=rows)
    assert len(fake.calls[0][1]) == 4 and len(out["chunks"]) == 8


# --- the window: per instance, never mixed by the process-wide cache ---

def test_get_reranker_cache_does_not_mix_windows(tmp_path, monkeypatch):
    built = []

    class Fake:
        def __init__(self, model_dir, onnx_file, max_tokens=None, batch_size=1, threads=1):
            built.append(max_tokens)

    folder, onnx_file = rerank.RERANK_MODELS["minilm"]
    (tmp_path / folder / "onnx").mkdir(parents=True)
    (tmp_path / folder / onnx_file).write_bytes(b"x")
    (tmp_path / folder / "tokenizer.json").write_text("{}")
    monkeypatch.setattr(rerank, "LocalCrossEncoder", Fake)
    rerank.get_reranker.cache_clear()
    try:
        a, b = rerank.get_reranker("minilm", tmp_path, max_tokens=192), rerank.get_reranker("minilm", tmp_path, max_tokens=384)
        assert a is not b and rerank.get_reranker("minilm", tmp_path, max_tokens=192) is a
        assert rerank.get_reranker("minilm", tmp_path) is b             # the default window stays the frozen 384
        assert built == [192, 384]
    finally:
        rerank.get_reranker.cache_clear()


def test_the_retriever_loads_the_reranker_at_the_configs_window(monkeypatch):
    asked = []
    monkeypatch.setattr(rerank, "get_reranker",
                        lambda key, root=None, threads=None, max_tokens=None: asked.append(max_tokens) or FakeReranker())
    rows = [scored_row("a", 1), scored_row("b", 2)]
    R.hybrid_retrieve(Q2, Driver(fusion_mention=rows), Embedder(), fusion=FusionConfig(rerank="minilm", rerank_max_tokens=192))
    assert asked == [192]


def _fake_ort_and_tokenizers(monkeypatch):
    seen = {"entries": {}, "truncation": None, "opts": None}

    class Opts:
        log_severity_level = 0
        intra_op_num_threads = inter_op_num_threads = None

        def __init__(self):
            seen["opts"] = self

        def add_session_config_entry(self, key, value):
            seen["entries"][key] = value

    class Session:
        def __init__(self, path, options, providers=None):
            pass

        def get_inputs(self):
            return [types.SimpleNamespace(name="input_ids")]

    class Tok:
        @classmethod
        def from_file(cls, path):
            return cls()

        def no_padding(self):
            pass

        def enable_truncation(self, max_length, strategy):
            seen["truncation"] = max_length

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(SessionOptions=Opts, InferenceSession=Session))
    monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=Tok))
    return seen


def test_session_spinning_is_off_with_one_thread_and_the_window_is_the_instances(monkeypatch, tmp_path):
    seen = _fake_ort_and_tokenizers(monkeypatch)
    rerank.LocalCrossEncoder(tmp_path, "m.onnx", max_tokens=192, threads=1)
    assert seen["entries"] == {"session.intra_op.allow_spinning": "0"}
    assert seen["opts"].intra_op_num_threads == 1 and seen["opts"].inter_op_num_threads == 1
    assert seen["truncation"] == 192


# --- 2. the time guard ---

class Clock:
    def __init__(self):
        self.now = 0.0

    def perf_counter(self):
        return self.now


def _encoder(clock, per_pair):
    enc = object.__new__(rerank.LocalCrossEncoder)
    enc._batch = 1

    def run(names, feeds):
        clock.now += per_pair
        return [np.array([[1.0]])]

    enc._session = types.SimpleNamespace(run=run)
    enc._batch_inputs = lambda q, ps: {}
    return enc


def test_encoder_abandons_between_pairs_once_past_the_deadline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rerank, "time", clock)
    enc = _encoder(clock, 0.5)
    with pytest.raises(rerank.RerankOverBudget) as err:
        enc.rerank_until("q", list("abcde"), 1.2)
    assert (err.value.done, err.value.total) == (3, 5)


def test_encoder_without_a_deadline_scores_everything(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rerank, "time", clock)
    assert _encoder(clock, 99.0).rerank("q", list("abc")) == [1.0, 1.0, 1.0]


def test_encoder_finishes_when_only_the_last_pair_crosses_the_deadline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rerank, "time", clock)
    assert len(_encoder(clock, 0.5).rerank_until("q", list("abc"), 1.2)) == 3


class OverBudget(FakeReranker):
    def rerank_until(self, query, candidates, deadline):
        self.calls.append((query, list(candidates), deadline))
        raise rerank.RerankOverBudget(done=3, total=len(candidates))


def test_over_budget_serves_the_fused_order_logs_and_counts(caplog):
    rows = [scored_row("a", 1), scored_row("b", 9), scored_row("c", 5)]
    before = rerank.over_budget_count()
    with caplog.at_level(logging.WARNING):
        out, _ = retrieve(OverBudget(), FusionConfig(rerank="minilm"), fusion_mention=rows, rerank_budget_s=1.8)
    assert [c["chunk_id"] for c in out["chunks"]] == ["a", "b", "c"] and "rerank_s" not in out    # the fused order
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "rerank over budget (" in text and "> 1.80 s, 3 of 3 pairs): serving the fused order" in text
    assert rerank.over_budget_count() == before + 1


def test_the_deadline_counts_the_candidate_text_fetch(monkeypatch):
    seen = {}

    class Spy(FakeReranker):
        def rerank_until(self, query, candidates, deadline):
            seen["deadline"] = deadline
            return [0.0] * len(candidates)

    monkeypatch.setattr(R.time, "perf_counter", lambda: 100.0)
    retrieve(Spy(), FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 1)], rerank_budget_s=1.8)
    assert seen["deadline"] == pytest.approx(101.8)                    # began (before the fetch) + the budget


def test_budget_zero_calls_the_reranker_exactly_as_before():
    fake = FakeReranker()                                               # only rerank(query, candidates): never asked for a deadline
    out, _ = retrieve(fake, FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 1), scored_row("b", 2)],
                      rerank_budget_s=0)
    assert len(fake.calls) == 1 and "rerank_s" in out


def test_the_budget_defaults_to_the_setting(monkeypatch):
    monkeypatch.setattr("semigraph.config.get_settings", lambda: settings(retrieval_rerank_budget_s=0.9))
    spy = OverBudget()
    retrieve(spy, FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 1)])
    assert spy.calls[0][2] is not None
    monkeypatch.setattr("semigraph.config.get_settings", lambda: settings(retrieval_rerank_budget_s=0))
    plain = FakeReranker()
    retrieve(plain, FusionConfig(rerank="minilm"), fusion_mention=[scored_row("a", 1)])
    assert len(plain.calls) == 1


def test_budget_setting_default_and_validation():
    from pydantic import ValidationError

    assert settings().retrieval_rerank_budget_s == 1.8
    assert settings(retrieval_rerank_budget_s=0).retrieval_rerank_budget_s == 0
    with pytest.raises(ValidationError):
        settings(retrieval_rerank_budget_s=-1)


def test_the_budget_is_not_part_of_the_serving_config():
    assert "budget" not in repr(fusion.SERVING_CONFIG).lower()
    assert not any("budget" in f for f in FusionConfig.__dataclass_fields__)


# --- the harnesses run with the guard off ---

def _calls(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


@pytest.mark.parametrize("path", ["scripts/m2_bench.py", "scripts/m2_answer_eval.py"])
def test_every_harness_retrieval_call_disables_the_guard(path):
    calls = _calls(path, "hybrid_retrieve")
    assert calls
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("rerank_budget_s"), ast.Constant) and kw["rerank_budget_s"].value == 0, (path, call.lineno)


def test_run_deployed_fused_retrieval_disables_the_guard_and_the_flag_off_call_is_unchanged():
    """eval-deployed (and so build_examples.py) measures with the guard off; the flag-off call stays exactly today's."""
    tree = ast.parse((ROOT / "src/semigraph/eval/bakeoff.py").read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_deployed")
    fused = next(n.value for n in ast.walk(run) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "fused")
    on = next(v for v in ast.walk(fused) if isinstance(v, ast.Dict) and v.keys)
    assert {k.value: v.value for k, v in zip(on.keys, on.values) if isinstance(v, ast.Constant)} == {"rerank_budget_s": 0}


# --- 5. the bench arm that is the served config ---

def test_bench_served_arm_is_the_served_config(fake_models):
    spec = importlib.util.spec_from_file_location("m2_bench_served", ROOT / "scripts" / "m2_bench.py")
    mb = importlib.util.module_from_spec(spec)
    sys.modules["m2_bench_served"] = mb
    spec.loader.exec_module(mb)
    assert "served" in mb.ARMS
    assert mb.arm_config("served") == fusion.serving_config(settings(retrieval_rerank="minilm"))
    frozen = mb.arm_config("all+rerank_minilm")
    assert (frozen.rerank_depth, frozen.rerank_max_tokens, frozen.rerank_max_candidates) == (20, 384, None)
