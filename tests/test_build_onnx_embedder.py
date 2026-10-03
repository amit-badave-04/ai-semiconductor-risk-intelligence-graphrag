"""scripts/build_onnx_embedder.py: accuracy_level=4 on every MatMulNBits node, and the in-build gates that prove it.

M5 decision 2.1: the shipped 8-bit model never set ``accuracy_level``, so ONNX Runtime dequantized every weight to fp32 on
every call (1.2 s per question instead of 0.3 s). The builder now sets it, and ``--verify-patched`` proves in the image
build that (a) every MatMulNBits node carries it and (b) the vectors stay within cosine mean 0.998 / min 0.997 of an
unpatched twin. The ``onnx`` package is dev-only (not in the shipped serve venv), so every test that builds a graph skips
there; the pure gate, parser and constant tests run in both environments.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_onnx_embedder.py"
spec = importlib.util.spec_from_file_location("build_onnx_embedder_script", SCRIPT)
bo = importlib.util.module_from_spec(spec)
sys.modules["build_onnx_embedder_script"] = bo
spec.loader.exec_module(bo)

REAL_MODEL = ROOT / "models" / "qwen3-embedding-0.6b-q8" / "model_q8.onnx"
EXAMPLES = ROOT / "src" / "semigraph" / "artifacts" / "examples.json"
TINY_DIM = 256

requires_onnx = pytest.mark.skipif(importlib.util.find_spec("onnx") is None,
                                   reason="onnx is a dev-only package (not installed in the shipped serve venv)")
requires_quantizer = pytest.mark.skipif(
    importlib.util.find_spec("onnx") is None or importlib.util.find_spec("onnx_ir") is None,
    reason="onnx + onnx-ir are dev-only packages (not installed in the shipped serve venv)")
requires_real_model = pytest.mark.skipif(not REAL_MODEL.exists(), reason=f"{REAL_MODEL} is not built on this machine")


# ---------------------------------------------------------------- fixtures shared with the sibling script tests

def write_tiny_embedder(directory: Path, seed: int = 0) -> Path:
    """An fp32 stand-in for the Qwen3 export that ORT can run in about a millisecond.

    ``input_ids`` + ``attention_mask`` -> ``last_hidden_state`` [1, T, 256] (a masked cumulative sum of token embeddings
    through one MatMul, so the last position depends on every token); weights live in ``model.onnx_data`` like the real
    export; ``tokenizer.json`` beside it is character-level. Returns the model path."""
    from onnx import TensorProto, helper, numpy_helper
    import onnx
    from tokenizers import Regex, Tokenizer, models, pre_tokenizers

    rng = np.random.default_rng(seed)
    init = [numpy_helper.from_array(rng.standard_normal((96, TINY_DIM)).astype(np.float32), "E"),
            numpy_helper.from_array((rng.standard_normal((TINY_DIM, TINY_DIM)) / 16).astype(np.float32), "W"),
            numpy_helper.from_array(np.array([-1], dtype=np.int64), "ax"),
            numpy_helper.from_array(np.array(1, dtype=np.int64), "cumsum_axis")]
    nodes = [helper.make_node("Gather", ["E", "input_ids"], ["emb"]),
             helper.make_node("Cast", ["attention_mask"], ["maskf"], to=TensorProto.FLOAT),
             helper.make_node("Unsqueeze", ["maskf", "ax"], ["mask3"]),
             helper.make_node("Mul", ["emb", "mask3"], ["masked"]),
             helper.make_node("CumSum", ["masked", "cumsum_axis"], ["csum"]),
             helper.make_node("MatMul", ["csum", "W"], ["last_hidden_state"], name="proj")]
    graph = helper.make_graph(
        nodes, "tiny",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, [1, "T"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [1, "T"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, [1, "T", TINY_DIM])], initializer=init)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    directory.mkdir(parents=True, exist_ok=True)
    onnx.save_model(model, str(directory / "model.onnx"), save_as_external_data=True, all_tensors_to_one_file=True,
                    location="model.onnx_data", size_threshold=1024)
    vocab = {"[UNK]": 0, **{chr(c): c - 31 for c in range(32, 127)}}
    tokenizer = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Split(Regex("."), behavior="isolated")
    tokenizer.save(str(directory / "tokenizer.json"))
    return directory / "model.onnx"


def build_tiny_quantized(tmp_path: Path, level: int) -> Path:
    """The tiny embedder run through the real ``quantize`` (8-bit, block 128); returns .../out/model_q8.onnx."""
    fp32 = write_tiny_embedder(tmp_path / "fp32")
    out = tmp_path / "out" / "model_q8.onnx"
    bo.quantize(fp32, fp32.with_name("model.onnx_data"), out, 8, 128, accuracy_level=level)
    shutil.copy(fp32.with_name("tokenizer.json"), out.parent / "tokenizer.json")
    return out


def fake_download(tiny_dir: Path):
    files = {"onnx/model.onnx": tiny_dir / "model.onnx", "onnx/model.onnx_data": tiny_dir / "model.onnx_data",
             "tokenizer.json": tiny_dir / "tokenizer.json"}
    return lambda repo, filename: files[filename]


# ---------------------------------------------------------------- hand-made graphs for the attribute tests

def nbits_node(name: str, accuracy_level: int | None = None):
    from onnx import helper
    node = helper.make_node("MatMulNBits", ["a", "b", "s"], [f"{name}_out"], name=name, domain="com.microsoft",
                            K=256, N=256, bits=8, block_size=128)
    if accuracy_level is not None:
        node.attribute.append(helper.make_attribute("accuracy_level", accuracy_level))
    return node


def model_of(*nodes):
    from onnx import helper
    return helper.make_model(helper.make_graph(list(nodes), "g", [], []))


def attributes(node) -> dict:
    from onnx import helper
    names = [a.name for a in node.attribute]
    assert len(names) == len(set(names)), f"duplicate attributes on {node.name}: {names}"
    return {a.name: helper.get_attribute_value(a) for a in node.attribute}


def effective_levels(proto) -> set[int]:
    return {attributes(n).get("accuracy_level", 0) for n in proto.graph.node if n.op_type == "MatMulNBits"}


# ---------------------------------------------------------------- set_accuracy_level / count_nodes_missing_accuracy_level

@requires_onnx
def test_set_accuracy_level_changes_only_the_nodes_that_differ():
    from onnx import helper
    plain = helper.make_node("MatMul", ["x", "w"], ["y"], name="plain")
    proto = model_of(nbits_node("absent"), nbits_node("zero", 0), nbits_node("already", 4), plain)

    changed = bo.set_accuracy_level(proto, 4)

    assert changed == 2
    assert [attributes(n).get("accuracy_level") for n in proto.graph.node] == [4, 4, 4, None]


@requires_onnx
def test_set_accuracy_level_is_idempotent_and_never_duplicates_the_attribute():
    proto = model_of(nbits_node("a"), nbits_node("b", 1))

    assert bo.set_accuracy_level(proto, 4) == 2
    assert bo.set_accuracy_level(proto, 4) == 0
    assert all(list(attributes(n)).count("accuracy_level") == 1 for n in proto.graph.node)


@requires_onnx
def test_set_accuracy_level_keeps_the_other_attributes():
    proto = model_of(nbits_node("a"))

    bo.set_accuracy_level(proto, 4)

    assert attributes(proto.graph.node[0]) == {"K": 256, "N": 256, "bits": 8, "block_size": 128, "accuracy_level": 4}


@requires_onnx
def test_counting_treats_an_absent_attribute_as_level_zero():
    proto = model_of(nbits_node("absent"), nbits_node("zero", 0), nbits_node("three", 3), nbits_node("four", 4))

    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 3
    assert bo.count_nodes_missing_accuracy_level(proto, 3) == 3
    assert bo.count_nodes_missing_accuracy_level(proto, 0) == 2
    assert bo.count_matmul_nbits(proto) == 4


@requires_onnx
def test_setting_level_zero_overwrites_a_positive_level_and_leaves_absent_nodes_alone():
    proto = model_of(nbits_node("absent"), nbits_node("four", 4))

    assert bo.set_accuracy_level(proto, 0) == 1
    assert effective_levels(proto) == {0}
    assert "accuracy_level" not in attributes(proto.graph.node[0])
    assert bo.count_nodes_missing_accuracy_level(proto, 0) == 0


@requires_onnx
def test_nodes_inside_subgraphs_are_found_patched_and_counted():
    from onnx import helper
    branch = helper.make_graph([nbits_node("inner")], "then", [], [])
    other = helper.make_graph([], "else", [], [])
    cond = helper.make_node("If", ["c"], ["o"], then_branch=branch, else_branch=other)
    proto = model_of(nbits_node("outer"), cond)

    assert bo.count_matmul_nbits(proto) == 2
    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 2
    assert bo.set_accuracy_level(proto, 4) == 2
    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 0


@requires_onnx
def test_a_graph_without_matmulnbits_is_a_noop():
    from onnx import helper
    proto = model_of(helper.make_node("MatMul", ["x", "w"], ["y"], name="plain"))

    assert bo.count_matmul_nbits(proto) == 0
    assert bo.set_accuracy_level(proto, 4) == 0
    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 0


@requires_onnx
@pytest.mark.parametrize("level", [-1, 5, 99])
def test_levels_outside_the_ort_range_are_rejected(level):
    proto = model_of(nbits_node("a"))

    with pytest.raises(ValueError, match="accuracy_level"):
        bo.set_accuracy_level(proto, level)
    with pytest.raises(ValueError, match="accuracy_level"):
        bo.count_nodes_missing_accuracy_level(proto, level)


# ---------------------------------------------------------------- the cosine gate (pure numpy)

def rotated(angle: float, rows: int = 4, dim: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """``rows`` unit vectors and copies turned by ``angle`` radians (cosine between the pairs = cos(angle))."""
    a = np.zeros((rows, dim), dtype=np.float32)
    a[:, 0] = 1.0
    b = np.zeros_like(a)
    b[:, 0], b[:, 1] = np.cos(angle), np.sin(angle)
    return a, b


def test_gate_thresholds_are_the_pre_registered_ones():
    assert bo.PATCH_COS_MEAN_MIN == 0.998
    assert bo.PATCH_COS_MIN_MIN == 0.997


def test_cosine_gate_passes_identical_vectors():
    a, _ = rotated(0.0)

    report = bo.cosine_gate(a, a.copy())

    assert report["passed"] is True
    assert report["questions"] == 4
    assert report["cosine_mean"] == pytest.approx(1.0) and report["cosine_min"] == pytest.approx(1.0)
    assert (report["mean_threshold"], report["min_threshold"]) == (0.998, 0.997)


def test_cosine_gate_fails_when_the_mean_is_below_0_998():
    a, b = rotated(np.arccos(0.9975))  # every pair at 0.9975: min is fine (>= 0.997), mean is not (< 0.998)

    report = bo.cosine_gate(a, b)

    assert report["cosine_min"] >= 0.997 and report["cosine_mean"] < 0.998
    assert report["passed"] is False


def test_cosine_gate_fails_when_one_question_is_below_0_997_even_if_the_mean_is_fine():
    a, b = rotated(0.0, rows=50)
    b[0, 0], b[0, 1] = np.cos(np.arccos(0.996)), np.sin(np.arccos(0.996))  # one pair at 0.996

    report = bo.cosine_gate(a, b)

    assert report["cosine_mean"] >= 0.998 and report["cosine_min"] < 0.997
    assert report["passed"] is False


def test_cosine_gate_passes_exactly_at_the_thresholds(monkeypatch):
    monkeypatch.setattr(bo, "PATCH_COS_MEAN_MIN", 1.0)
    monkeypatch.setattr(bo, "PATCH_COS_MIN_MIN", 1.0)
    a, _ = rotated(0.0)

    assert bo.cosine_gate(a, a.copy())["passed"] is True  # >=, not >


def test_cosine_gate_normalizes_its_inputs():
    a, b = rotated(0.0)

    assert bo.cosine_gate(a * 3.0, b * 0.5)["cosine_mean"] == pytest.approx(1.0)


def test_cosine_gate_rejects_empty_and_mismatched_inputs():
    a, _ = rotated(0.0)
    with pytest.raises(ValueError, match="no vectors"):
        bo.cosine_gate(a[:0], a[:0])
    with pytest.raises(ValueError, match="shape"):
        bo.cosine_gate(a, a[:2])


# ---------------------------------------------------------------- the built-in question list and the prompt

def test_the_gate_has_at_least_twenty_distinct_realistic_questions():
    qs = bo.GATE_QUESTIONS

    assert len(qs) >= 20
    assert len(set(qs)) == len(qs)
    assert all(isinstance(q, str) and q == q.strip() and 20 < len(q) < 600 for q in qs)


@pytest.mark.skipif(not EXAMPLES.exists(), reason="examples.json not present")
def test_the_gate_questions_are_exactly_the_53_example_questions():
    """M5 decision 2.1 gates "on the 53 questions". The script is copied alone into the Docker model stage, so the list is
    inlined; this pin makes any change to examples.json a deliberate decision about the gate."""
    expected = [e["question"] for e in json.loads(EXAMPLES.read_text(encoding="utf-8"))["examples"]]

    assert list(bo.GATE_QUESTIONS) == expected
    assert len(expected) == 53


def test_the_inlined_query_prompt_is_the_serving_prompt():
    embeddings = pytest.importorskip("semigraph.embeddings")

    assert bo.QUERY_PROMPT == embeddings.QUERY_PROMPT
    assert not bo.QUERY_PROMPT.endswith(" ")  # the spike scripts used "Query: " (a trailing space): production does not


# ---------------------------------------------------------------- command line

def test_cli_defaults_keep_the_old_behaviour_and_level_4_is_opt_in():
    """Level 4 failed its corpus-parity gate (M5_DECISIONS 1.4): a bare invocation must still build today's model."""
    args = bo.parse_args([])

    assert args.accuracy_level == 0 and bo.ACCURACY_LEVEL_DEFAULT == 0
    assert args.verify is True and args.verify_patched is False and args.keep_unpatched is False
    assert args.bits == 8 and args.block_size == 128 and args.out == "models/qwen3-embedding-0.6b-q8"


def test_cli_accepts_the_dockerfile_flags():
    args = bo.parse_args(["--out", "/models/x", "--no-verify", "--verify-patched", "--accuracy-level", "3"])

    assert (args.out, args.verify, args.verify_patched, args.accuracy_level) == ("/models/x", False, True, 3)


@pytest.mark.parametrize("bad", ["5", "-1", "four"])
def test_cli_rejects_accuracy_levels_outside_0_to_4(bad):
    with pytest.raises(SystemExit) as raised:
        bo.parse_args(["--accuracy-level", bad])

    assert raised.value.code == 2


def test_cli_refuses_to_verify_a_patch_that_is_level_zero(capsys):
    with pytest.raises(SystemExit) as raised:
        bo.parse_args(["--verify-patched", "--accuracy-level", "0"])

    assert raised.value.code == 2
    assert "verify-patched" in capsys.readouterr().err


def test_the_module_documents_the_dockerfile_command():
    assert "--no-verify --accuracy-level 4 --verify-patched" in bo.__doc__
    assert "ONLY after the owner approves" in bo.__doc__  # level 4 is not shipped until the owner decides
    assert "tokenizers" in bo.__doc__  # the model stage must install it for the gate


def test_the_dockerfile_does_not_opt_in_to_level_4_yet():
    """Guard: the image build keeps today's model until the owner approves the patched one."""
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "--accuracy-level" not in dockerfile and "--verify-patched" not in dockerfile


def test_missing_gate_dependencies_names_what_the_model_stage_lacks(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None if name == "tokenizers" else object())

    assert bo.missing_gate_dependencies() == ["tokenizers"]

    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    assert bo.missing_gate_dependencies() == []


# ---------------------------------------------------------------- the real quantizer on the tiny model

@requires_quantizer
@pytest.mark.parametrize("level", [4, 2])
def test_quantize_sets_the_level_on_every_matmulnbits_node(tmp_path, level):
    import onnx
    model = build_tiny_quantized(tmp_path, level)

    proto = onnx.load(str(model), load_external_data=False)

    assert bo.count_matmul_nbits(proto) == 1
    assert bo.count_nodes_missing_accuracy_level(proto, level) == 0
    assert effective_levels(proto) == {level}
    assert (model.parent / "model_q8.onnx_data").exists()  # the memory-lean sidecar layout is unchanged


@requires_quantizer
def test_quantize_with_level_zero_reproduces_the_old_unpatched_build(tmp_path):
    import onnx
    model = build_tiny_quantized(tmp_path, 0)

    proto = onnx.load(str(model), load_external_data=False)

    assert [("accuracy_level" in attributes(n)) for n in proto.graph.node if n.op_type == "MatMulNBits"] == [False]


@requires_quantizer
def test_the_post_quantize_pass_alone_is_enough_when_the_quantizer_ignores_the_level(tmp_path, monkeypatch):
    """A newer onnxruntime could stop honouring DefaultWeightOnlyQuantConfig(accuracy_level=...). The builder must not
    depend on it: set_accuracy_level runs on the quantized proto every time."""
    import onnx
    from onnxruntime.quantization import matmul_nbits_quantizer as mq
    real = mq.DefaultWeightOnlyQuantConfig
    monkeypatch.setattr(mq, "DefaultWeightOnlyQuantConfig",
                        lambda **kw: real(**{k: v for k, v in kw.items() if k != "accuracy_level"}))

    proto = onnx.load(str(build_tiny_quantized(tmp_path, 4)), load_external_data=False)

    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 0


@requires_quantizer
def test_ort_runs_the_quantized_level_four_model(tmp_path):
    ort = pytest.importorskip("onnxruntime")
    model = build_tiny_quantized(tmp_path, 4)
    ids = np.array([[3, 9, 27, 81, 5]], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}

    patched = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"]).run(None, feeds)[0]

    assert patched.shape == (1, 5, TINY_DIM) and np.isfinite(patched).all()


# ---------------------------------------------------------------- verify_patched on tiny models

def unit_rows(n: int, seed: int = 0) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal((n, 16)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


@requires_quantizer
def test_verify_patched_compares_against_a_level_zero_twin_beside_the_sidecar_and_removes_it(tmp_path):
    import onnx
    model = build_tiny_quantized(tmp_path, 4)
    before = sorted(p.name for p in model.parent.iterdir())
    seen = []

    def embed(model_path, tokenizer_path, questions):
        proto = onnx.load(str(model_path), load_external_data=False)
        seen.append((model_path, model_path.parent, effective_levels(proto), tokenizer_path.name, len(questions)))
        return unit_rows(len(questions))

    report = bo.verify_patched(model.parent, 4, questions=("What is a?", "What is b?", "What is c?"), embed=embed)

    assert [s[2] for s in seen] == [{4}, {0}]  # patched model first, then the level-0 twin
    assert seen[0][0] == model and seen[1][0] != model and seen[1][1] == model.parent
    assert {s[3] for s in seen} == {"tokenizer.json"} and {s[4] for s in seen} == {3}
    assert sorted(p.name for p in model.parent.iterdir()) == before  # the twin is gone
    assert report["passed"] is True and report["failure"] is None
    assert report["matmulnbits_nodes"] == 1 and report["nodes_missing_accuracy_level"] == 0
    assert report["questions"] == 3 and report["cosine_min"] == pytest.approx(1.0)
    assert report["accuracy_level"] == 4


@requires_quantizer
def test_verify_patched_fails_on_a_cosine_below_the_gate_and_still_removes_the_twin(tmp_path):
    model = build_tiny_quantized(tmp_path, 4)
    before = sorted(p.name for p in model.parent.iterdir())

    def embed(model_path, tokenizer_path, questions):
        return unit_rows(len(questions), seed=1 if "unpatched" in model_path.name else 0)  # unrelated vectors

    report = bo.verify_patched(model.parent, 4, questions=("What is a?", "What is b?"), embed=embed)

    assert report["passed"] is False and "cosine" in report["failure"]
    assert sorted(p.name for p in model.parent.iterdir()) == before


@requires_quantizer
def test_verify_patched_removes_the_twin_when_embedding_raises(tmp_path):
    model = build_tiny_quantized(tmp_path, 4)
    before = sorted(p.name for p in model.parent.iterdir())

    def embed(model_path, tokenizer_path, questions):
        if "unpatched" in model_path.name:
            raise RuntimeError("session failed")
        return unit_rows(len(questions))

    with pytest.raises(RuntimeError, match="session failed"):
        bo.verify_patched(model.parent, 4, questions=("What is a?",), embed=embed)

    assert sorted(p.name for p in model.parent.iterdir()) == before


@requires_quantizer
def test_verify_patched_fails_fast_when_a_node_lacks_the_level_without_embedding(tmp_path):
    model = build_tiny_quantized(tmp_path, 0)

    def embed(*args):
        raise AssertionError("must not embed when the attribute check already failed")

    report = bo.verify_patched(model.parent, 4, questions=("What is a?",), embed=embed)

    assert report["passed"] is False
    assert report["nodes_missing_accuracy_level"] == 1 and report["cosine_mean"] is None
    assert "accuracy_level" in report["failure"]


@requires_quantizer
def test_verify_patched_fails_when_the_graph_has_no_matmulnbits(tmp_path):
    fp32 = write_tiny_embedder(tmp_path / "fp32")  # never quantized
    shutil.copy(fp32, fp32.with_name("model_q8.onnx"))

    report = bo.verify_patched(fp32.parent, 4, questions=("What is a?",),
                               embed=lambda *a: (_ for _ in ()).throw(AssertionError("must not embed")))

    assert report["passed"] is False and report["matmulnbits_nodes"] == 0
    assert "no MatMulNBits" in report["failure"]


@requires_quantizer
def test_verify_patched_runs_the_real_embedder_on_the_tiny_model(tmp_path):
    model = build_tiny_quantized(tmp_path, 4)

    report = bo.verify_patched(model.parent, 4, questions=bo.GATE_QUESTIONS[:20])

    assert report["questions"] == 20
    assert report["cosine_min"] > 0.99  # int8 activations on a tiny random graph: close, and ORT accepted level 4
    assert report["passed"] is True
    assert not [p for p in model.parent.iterdir() if "unpatched" in p.name]


@requires_quantizer
def test_embed_questions_returns_unit_vectors_and_frees_the_session(tmp_path):
    model = build_tiny_quantized(tmp_path, 4)

    vecs = bo.embed_questions(model, model.parent / "tokenizer.json", ["What is a?", "What is b?"])

    assert vecs.shape == (2, TINY_DIM) and vecs.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, rtol=1e-5)
    assert not np.allclose(vecs[0], vecs[1])


# ---------------------------------------------------------------- main(): the whole flow on the tiny model

OUT_FILES = {"model_q8.onnx", "model_q8.onnx_data", "tokenizer.json", "patch_fidelity.json"}


@requires_quantizer
def test_main_builds_a_patched_model_and_passes_the_in_build_gate(tmp_path, monkeypatch, capsys):
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    out = tmp_path / "out"

    bo.main(["--out", str(out), "--no-verify", "--accuracy-level", "4", "--verify-patched"])

    assert {p.name for p in out.iterdir()} == OUT_FILES
    report = json.loads((out / "patch_fidelity.json").read_text(encoding="utf-8"))
    assert report["passed"] is True and report["accuracy_level"] == 4 and report["questions"] == len(bo.GATE_QUESTIONS)
    assert "patch gate" in capsys.readouterr().out


@requires_quantizer
def test_main_keep_unpatched_leaves_a_small_runnable_baseline_beside_the_sidecar(tmp_path, monkeypatch):
    import onnx
    ort = pytest.importorskip("onnxruntime")
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    out = tmp_path / "out"

    bo.main(["--out", str(out), "--no-verify", "--keep-unpatched"])

    baseline = out / "model_q8.unpatched.onnx"
    assert baseline.exists() and baseline.stat().st_size < 50_000  # a proto that points at the same sidecar
    assert effective_levels(onnx.load(str(baseline), load_external_data=False)) == {0}
    ids = np.array([[3, 9, 27]], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
    run = ort.InferenceSession(str(baseline), providers=["CPUExecutionProvider"]).run(None, feeds)[0]
    assert run.shape == (1, 3, TINY_DIM)


@requires_quantizer
def test_main_removes_the_files_it_built_when_the_gate_fails(tmp_path, monkeypatch):
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    monkeypatch.setattr(bo, "embed_questions", lambda model, tok, qs, threads=0: unit_rows(
        len(qs), seed=1 if "unpatched" in Path(model).name else 0))
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(out), "--no-verify", "--accuracy-level", "4", "--verify-patched"])

    assert "PATCH GATE FAILED" in str(raised.value)
    assert not list(out.glob("model_q8*"))


@requires_quantizer
def test_main_never_deletes_a_model_it_did_not_build(tmp_path, monkeypatch):
    """models/.../model_q8.onnx on a dev machine is the BASELINE the parity and timing scripts compare against."""
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    existing = build_tiny_quantized(tmp_path, 4)  # a level-4 build already sits in --out
    digest = existing.read_bytes()
    monkeypatch.setattr(bo, "embed_questions", lambda model, tok, qs, threads=0: unit_rows(
        len(qs), seed=1 if "unpatched" in Path(model).name else 0))  # the gate will fail

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(existing.parent), "--no-verify", "--accuracy-level", "4", "--verify-patched"])

    assert "PATCH GATE FAILED" in str(raised.value) and "--force" in str(raised.value)
    assert existing.read_bytes() == digest and (existing.parent / "model_q8.onnx_data").exists()


@requires_quantizer
def test_main_refuses_to_reuse_a_model_built_at_another_level_and_leaves_it_alone(tmp_path, monkeypatch):
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    existing = build_tiny_quantized(tmp_path, 0)
    digest = existing.read_bytes()

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(existing.parent), "--no-verify", "--accuracy-level", "4"])

    assert "accuracy_level" in str(raised.value) and "--force" in str(raised.value)
    assert existing.read_bytes() == digest


@requires_quantizer
def test_a_bare_run_refuses_a_level_4_model_left_in_the_output_directory(tmp_path, monkeypatch):
    """The reverse trap: an earlier opt-in level-4 build must never silently become the model of a later default run."""
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))
    existing = build_tiny_quantized(tmp_path, 4)

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(existing.parent), "--no-verify"])

    assert "accuracy_level=0" in str(raised.value)


@requires_quantizer
def test_main_removes_the_model_it_built_when_the_gate_crashes(tmp_path, monkeypatch):
    tiny = tmp_path / "fp32"
    write_tiny_embedder(tiny)
    monkeypatch.setattr(bo, "download", fake_download(tiny))

    def crash(out_dir, level, **kwargs):
        raise RuntimeError("session failed to load")

    monkeypatch.setattr(bo, "verify_patched", crash)
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(out), "--no-verify", "--accuracy-level", "4", "--verify-patched"])

    assert "GATE ERROR" in str(raised.value) and "session failed to load" in str(raised.value)
    assert not list(out.glob("model_q8*"))


def test_main_passes_the_requested_level_to_the_quantizer(tmp_path, monkeypatch):
    seen = {}

    def fake_quantize(fp32, data, out_model, bits, block_size, accuracy_level):
        seen.update(bits=bits, block_size=block_size, accuracy_level=accuracy_level)
        out_model.parent.mkdir(parents=True, exist_ok=True)
        out_model.write_bytes(b"x")

    tok = tmp_path / "tokenizer.json"
    tok.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bo, "download", lambda repo, name: tok)
    monkeypatch.setattr(bo, "quantize", fake_quantize)

    bo.main(["--out", str(tmp_path / "o"), "--no-verify", "--accuracy-level", "3", "--bits", "8"])

    assert seen == {"bits": 8, "block_size": 128, "accuracy_level": 3}


def test_main_stops_before_the_slow_build_when_the_gate_cannot_run(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the build must not start when the gate's dependencies are missing")

    monkeypatch.setattr(bo, "missing_gate_dependencies", lambda: ["tokenizers"])
    monkeypatch.setattr(bo, "download", forbidden)
    monkeypatch.setattr(bo, "quantize", forbidden)

    with pytest.raises(SystemExit) as raised:
        bo.main(["--out", str(tmp_path / "o"), "--no-verify", "--accuracy-level", "4", "--verify-patched"])

    assert "tokenizers" in str(raised.value)


# ---------------------------------------------------------------- the real model (skipped where it is not built)

@requires_onnx
@requires_real_model
def test_the_real_model_has_196_matmulnbits_nodes_and_patching_covers_all_of_them():
    import onnx
    proto = onnx.load(str(REAL_MODEL), load_external_data=False)

    assert bo.count_matmul_nbits(proto) == 196
    bo.set_accuracy_level(proto, 4)
    assert bo.count_nodes_missing_accuracy_level(proto, 4) == 0


@requires_real_model
def test_the_inlined_encoder_matches_the_serving_backend_on_the_real_model():
    backend_module = pytest.importorskip("semigraph.embeddings_onnx")
    tokenizer = REAL_MODEL.parent / "tokenizer.json"
    question = "What export-control risks does Nvidia report in its latest annual filing?"

    mine = bo.embed_questions(REAL_MODEL, tokenizer, [question], threads=2)[0]
    serving = np.array(backend_module.OnnxBackend(REAL_MODEL, tokenizer, threads=2).encode_query(question),
                       dtype=np.float32)

    np.testing.assert_allclose(mine, serving, atol=1e-6)
