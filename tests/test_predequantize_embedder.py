"""scripts/predequantize_embedder.py: the 8-bit embedder with every MatMulNBits weight dequantized ONCE, offline.

ONNX Runtime 1.29 dequantizes every MatMulNBits weight on every call at accuracy levels 0-3, so the shipped model spends
most of its time rebuilding fp32 weights that never change. The script writes them once (float32 initializers, plain
MatMul) into an external-data sidecar. These tests pin what makes that safe: the dequantization is bit-exact against
ONNX Runtime's own (with and without zero points), the gate reads back the bytes that were actually written, a model that
fails any gate is never left on disk, a source whose weights live in a sidecar is fully absorbed (the source can be
deleted), and the build is deterministic. Everything runs on a 256-wide synthetic model; ``onnx`` is dev-only, so the
whole module skips in the shipped serve venv.
"""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("onnx", reason="onnx is a dev-only package (not installed in the shipped serve venv)")
pytest.importorskip("tokenizers")

from onnx import TensorProto, helper, numpy_helper  # noqa: E402

from test_build_onnx_embedder import TINY_DIM, write_tiny_embedder  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "predequantize_embedder.py"
spec = importlib.util.spec_from_file_location("predequantize_embedder_script", SCRIPT)
pq = importlib.util.module_from_spec(spec)
sys.modules["predequantize_embedder_script"] = pq
spec.loader.exec_module(pq)

BLOCK = 128
SOURCE_GRAPH, SOURCE_DATA = "model_q8.onnx", "model_q8.onnx_data"
OUT_FILES = {"model_fp32.onnx", "model_fp32.onnx.data", "model_fp32.fidelity.json", "tokenizer.json"}


# ---------------------------------------------------------------- a synthetic MatMulNBits embedder (hand-built, no quantizer)

def random_weights(rng, n: int, k: int, zero_points: str | None, bits: int = 8) -> dict:
    """B (N, k_blocks, block), scales (N, k_blocks) and an optional zero-point tensor, as the contrib op stores them."""
    kb = -(-k // BLOCK)
    out = {"b": rng.integers(0, 256, size=(n, kb, BLOCK * bits // 8), dtype=np.uint8),
           "s": (rng.random((n, kb)) * 0.02 + 0.001).astype(np.float32), "zp": None}
    if zero_points == "uint8":
        out["zp"] = rng.integers(96, 160, size=(n, kb), dtype=np.uint8)
    elif zero_points == "float":
        out["zp"] = rng.integers(96, 160, size=(n, kb)).astype(np.float32)
    return out


def nbits_node(name: str, a: str, out: str, k: int, n: int, *, zero_points: bool, bits: int = 8,
               accuracy_level: int | None = None, g_idx: bool = False, bias: bool = False, b_name: str | None = None):
    inputs = [a, b_name or f"{name}.B", f"{name}.S"]
    if zero_points or g_idx or bias:
        inputs.append(f"{name}.ZP" if zero_points else "")
    if g_idx or bias:
        inputs.append(f"{name}.G" if g_idx else "")
    if bias:
        inputs.append(f"{name}.bias")
    attrs = {"K": k, "N": n, "bits": bits, "block_size": BLOCK}
    if accuracy_level is not None:
        attrs["accuracy_level"] = accuracy_level
    return helper.make_node("MatMulNBits", inputs, [out], name=name, domain="com.microsoft", **attrs)


def write_nbits_embedder(directory: Path, *, zero_points=(False, True), external: bool = True, seed: int = 0,
                         size_threshold: int = 1024, **node_options) -> Path:
    """tokens -> masked cumulative sum of embeddings -> one MatMulNBits per entry of ``zero_points`` -> hidden states.

    ``zero_points[i]`` is None / False (symmetric), True / "uint8" or "float" for node ``i``. Writes ``model_q8.onnx``
    (+ ``model_q8.onnx_data`` when ``external``, like the Docker build) and the character-level ``tokenizer.json``."""
    import onnx

    rng = np.random.default_rng(seed)
    init = [numpy_helper.from_array(rng.standard_normal((96, TINY_DIM)).astype(np.float32), "E"),
            numpy_helper.from_array(np.array([-1], dtype=np.int64), "ax"),
            numpy_helper.from_array(np.array(1, dtype=np.int64), "cumsum_axis")]
    nodes = [helper.make_node("Gather", ["E", "input_ids"], ["emb"]),
             helper.make_node("Cast", ["attention_mask"], ["maskf"], to=TensorProto.FLOAT),
             helper.make_node("Unsqueeze", ["maskf", "ax"], ["mask3"]),
             helper.make_node("Mul", ["emb", "mask3"], ["masked"]),
             helper.make_node("CumSum", ["masked", "cumsum_axis"], ["h0"])]
    previous = "h0"
    for index, zp in enumerate(zero_points):
        name = f"proj{index}"
        kind = zp if isinstance(zp, str) else ("uint8" if zp else None)
        weights = random_weights(rng, TINY_DIM, TINY_DIM, kind, node_options.get("bits", 8))
        out = "last_hidden_state" if index == len(zero_points) - 1 else f"h{index + 1}"
        nodes.append(nbits_node(name, previous, out, TINY_DIM, TINY_DIM, zero_points=kind is not None, **node_options))
        init += [numpy_helper.from_array(weights["b"], f"{name}.B"), numpy_helper.from_array(weights["s"], f"{name}.S")]
        if kind is not None:
            init.append(numpy_helper.from_array(weights["zp"], f"{name}.ZP"))
        previous = out
    graph = helper.make_graph(
        nodes, "tiny_nbits",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, [1, "T"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [1, "T"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, [1, "T", TINY_DIM])], initializer=init)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17), helper.make_opsetid("com.microsoft", 1)])
    model.ir_version = 9
    directory.mkdir(parents=True, exist_ok=True)
    if external:
        onnx.save_model(model, str(directory / SOURCE_GRAPH), save_as_external_data=True, all_tensors_to_one_file=True,
                        location=SOURCE_DATA, size_threshold=size_threshold)
    else:
        onnx.save_model(model, str(directory / SOURCE_GRAPH))
    tokenizer = write_tiny_embedder(directory.parent / f"tok-{directory.name}").with_name("tokenizer.json")
    shutil.copy(tokenizer, directory / "tokenizer.json")
    return directory / SOURCE_GRAPH


def run(model: Path, ids=(3, 9, 27, 40)) -> np.ndarray:
    import onnxruntime as ort

    arr = np.array([ids], dtype=np.int64)
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return session.run(None, {"input_ids": arr, "attention_mask": np.ones_like(arr)})[0]


def ort_weight_t(weights: dict, k: int, n: int) -> np.ndarray:
    """ONNX Runtime's own dequantized W.T: the contrib op fed an identity matrix (Y = I @ W.T), accuracy_level unset.
    Written without the script's helpers on purpose, so the script is checked against an independent oracle."""
    import onnxruntime as ort

    inputs = ["A", "B", "S"] + (["ZP"] if weights["zp"] is not None else [])
    init = [numpy_helper.from_array(weights["b"], "B"), numpy_helper.from_array(weights["s"], "S")]
    if weights["zp"] is not None:
        init.append(numpy_helper.from_array(weights["zp"], "ZP"))
    node = helper.make_node("MatMulNBits", inputs, ["Y"], domain="com.microsoft", K=k, N=n, bits=8, block_size=BLOCK)
    graph = helper.make_graph([node], "one", [helper.make_tensor_value_info("A", TensorProto.FLOAT, [k, k])],
                              [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [k, n])], initializer=init)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 14), helper.make_opsetid("com.microsoft", 1)])
    session = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    return session.run(None, {"A": np.eye(k, dtype=np.float32)})[0]


def unit_rows(count: int, seed: int) -> np.ndarray:
    rows = np.random.default_rng(seed).standard_normal((count, 8))
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


def drifting(model, tokenizer, questions, threads=0) -> np.ndarray:
    """An encoder whose vectors for the pre-dequantized model have nothing to do with the source's (a failed cosine gate)."""
    return unit_rows(len(questions), seed=1 if Path(model).name == "model_fp32.onnx" else 0)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(tmp_path: Path, name: str = "src", **options):
    src_model = write_nbits_embedder(tmp_path / name, **{k: v for k, v in options.items() if k != "build"})
    out = tmp_path / f"out-{name}"
    report = pq.predequantize(src_model.parent, out, **options.get("build", {}))
    return src_model.parent, out, report


# ---------------------------------------------------------------- dequantize(): ONNX Runtime's own formula, bit for bit

@pytest.mark.parametrize("zero_points", [None, "uint8"], ids=["default-zero-point-128", "uint8-zero-points"])
@pytest.mark.parametrize("k", [256, 192], ids=["k-multiple-of-block", "k-padded-to-block"])
def test_dequantize_is_bit_exact_against_onnxruntimes_own_dequantization(zero_points, k):
    n = 128
    weights = random_weights(np.random.default_rng(7), n, k, zero_points)

    ours = np.ascontiguousarray(pq.dequantize(weights["b"], weights["s"], k, weights["zp"]).T)

    assert ours.shape == (k, n) and ours.dtype == np.float32
    assert np.array_equal(ort_weight_t(weights, k, n), ours)


def test_the_default_zero_point_of_an_8_bit_weight_is_128():
    b = np.array([[[0, 128, 255]]], dtype=np.uint8)           # N=1, one block of 3 weights (K=3)
    s = np.array([[0.5]], dtype=np.float32)

    assert pq.dequantize(b, s, 3).tolist() == [[-64.0, 0.0, 63.5]]
    assert pq.dequantize(b, s, 3, np.array([[10]], dtype=np.uint8)).tolist() == [[-5.0, 59.0, 122.5]]


def test_dequantize_cuts_the_block_padding_off_the_last_block():
    b = np.full((2, 2, BLOCK), 129, dtype=np.uint8)
    s = np.ones((2, 2), dtype=np.float32)

    assert pq.dequantize(b, s, 200).shape == (2, 200)


# ---------------------------------------------------------------- predequantize(): the whole build on the synthetic model

@pytest.mark.parametrize("external", [True, False], ids=["sidecar-source", "single-file-source"])
@pytest.mark.parametrize("zero_points", [(False,), (True,), (False, True), ("uint8", False)],
                         ids=["symmetric", "zero-points", "mixed", "mixed-reversed"])
def test_every_matmulnbits_node_becomes_a_plain_matmul_and_the_vectors_do_not_move(tmp_path, external, zero_points):
    import onnx

    src, out, report = build(tmp_path, zero_points=zero_points, external=external)

    assert {p.name for p in out.iterdir()} == OUT_FILES
    proto = onnx.load(str(out / "model_fp32.onnx"), load_external_data=False)
    assert pq.bo.count_matmul_nbits(proto) == 0
    assert [n.op_type for n in proto.graph.node].count("MatMul") == len(zero_points)
    assert report["dequantized_nodes"] == len(zero_points) == report["nodes_bit_exact"]
    assert report["max_node_abs_diff"] == 0.0 and report["gate"]["passed"] is True
    np.testing.assert_allclose(run(out / "model_fp32.onnx"), run(src / SOURCE_GRAPH), rtol=1e-5, atol=1e-5)


@pytest.mark.skipif(importlib.util.find_spec("onnx_ir") is None, reason="the quantizer needs onnx-ir (dev-only)")
def test_the_output_of_the_real_quantizer_is_pre_dequantized(tmp_path):
    """The build that ships: ``build_onnx_embedder.quantize`` (symmetric 8-bit, block 128, weights in a sidecar)."""
    import onnx
    from test_build_onnx_embedder import build_tiny_quantized

    model = build_tiny_quantized(tmp_path, 0)

    report = pq.predequantize(model.parent, tmp_path / "fp32")

    assert report["dequantized_nodes"] == 1 and report["nodes_bit_exact"] == 1 and report["gate"]["passed"]
    proto = onnx.load(str(tmp_path / "fp32" / "model_fp32.onnx"), load_external_data=False)
    assert pq.bo.count_matmul_nbits(proto) == 0
    np.testing.assert_allclose(run(tmp_path / "fp32" / "model_fp32.onnx"), run(model), rtol=1e-5, atol=1e-5)


def test_the_output_survives_deleting_the_source_even_when_its_weights_sat_in_a_sidecar(tmp_path):
    """Every external tensor of the source (the embedding table included) must move to the NEW sidecar."""
    import onnx

    src, out, _ = build(tmp_path, external=True, build={"externalize_min_bytes": 1024})
    expected = run(out / "model_fp32.onnx")

    shutil.rmtree(src)

    proto = onnx.load(str(out / "model_fp32.onnx"), load_external_data=False)
    locations = {entry.value for init in proto.graph.initializer for entry in init.external_data if entry.key == "location"}
    assert locations == {"model_fp32.onnx.data"}
    assert "E" in {i.name for i in proto.graph.initializer if i.external_data}      # the passthrough table moved too
    assert np.array_equal(run(out / "model_fp32.onnx"), expected)


def test_dequantized_weights_are_written_aligned_into_the_sidecar(tmp_path):
    import onnx

    _, out, _ = build(tmp_path)
    proto = onnx.load(str(out / "model_fp32.onnx"), load_external_data=False)
    offsets = [int(e.value) for i in proto.graph.initializer for e in i.external_data if e.key == "offset"]

    assert len(offsets) >= 2 and all(offset % pq.ALIGN == 0 for offset in offsets)


def test_the_build_is_deterministic_byte_for_byte(tmp_path):
    _, first, _ = build(tmp_path, "a", seed=3)
    _, second, _ = build(tmp_path, "b", seed=3)

    for name in ("model_fp32.onnx", "model_fp32.onnx.data"):
        assert digest(first / name) == digest(second / name)


def test_the_fidelity_file_records_what_was_proven_and_where_it_came_from(tmp_path):
    import onnxruntime

    src, out, report = build(tmp_path, zero_points=(False, True))
    on_disk = json.loads((out / "model_fp32.fidelity.json").read_text(encoding="utf-8"))

    assert on_disk == report
    assert (on_disk["variant"], on_disk["bits"], on_disk["block_size"]) == ("fp32", 8, BLOCK)
    assert (on_disk["dequantized_nodes"], on_disk["nodes_bit_exact"], on_disk["max_node_abs_diff"]) == (2, 2, 0.0)
    assert on_disk["onnxruntime"] == onnxruntime.__version__ and on_disk["model"] == "model_fp32.onnx"
    assert on_disk["data"] == "model_fp32.onnx.data"
    gate = on_disk["gate"]
    assert gate["questions"] == len(pq.bo.GATE_QUESTIONS) and gate["cosine_min"] >= gate["min_threshold"]
    assert (gate["mean_threshold"], gate["min_threshold"]) == (pq.bo.PATCH_COS_MEAN_MIN, pq.bo.PATCH_COS_MIN_MIN)
    files = on_disk["source"]["files"]
    assert files == {SOURCE_GRAPH: digest(src / SOURCE_GRAPH), SOURCE_DATA: digest(src / SOURCE_DATA)}
    combined = hashlib.sha256((src / SOURCE_GRAPH).read_bytes() + (src / SOURCE_DATA).read_bytes()).hexdigest()
    assert on_disk["source"]["sha256"] == combined


def test_a_single_file_source_is_hashed_as_one_file(tmp_path):
    src, _, report = build(tmp_path, external=False)

    assert list(report["source"]["files"]) == [SOURCE_GRAPH]
    assert report["source"]["sha256"] == digest(src / SOURCE_GRAPH)


def test_the_source_is_never_modified(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    before = {p.name: digest(p) for p in src.parent.iterdir()}

    pq.predequantize(src.parent, tmp_path / "out")

    assert {p.name: digest(p) for p in src.parent.iterdir()} == before


# ---------------------------------------------------------------- a model that fails a gate is never left on disk

def nothing_left(out: Path) -> bool:
    return not out.exists() or not any(out.iterdir())


@pytest.mark.parametrize("fault", ["wrong-dequantization", "corrupted-write"])
def test_a_corrupted_node_is_refused_and_no_model_is_written(tmp_path, monkeypatch, fault):
    """Fault injection on the way to disk: the gate compares ONNX Runtime's output with the bytes read back from the new
    sidecar, so it catches a wrong value however it got there (a bad formula or a bad write), not only a bad formula."""
    src = write_nbits_embedder(tmp_path / "src", zero_points=(False, True, False))
    calls = {"n": 0}
    real_dequantize, real_add = pq.dequantize, pq.SidecarWriter.add

    def flipped_dequantize(*args, **kwargs):
        w = real_dequantize(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:
            w = w.copy()
            w[3, 5] = np.nextafter(w[3, 5], np.float32(np.inf))      # ONE element, one ulp
        return w

    def corrupting_add(self, tensor, data):
        if tensor.name.startswith("proj1"):
            data = bytearray(data)
            data[-1] ^= 0x01                                         # the last bit of the last float
            data = bytes(data)
        return real_add(self, tensor, data)

    if fault == "wrong-dequantization":
        monkeypatch.setattr(pq, "dequantize", flipped_dequantize)
    else:
        monkeypatch.setattr(pq.SidecarWriter, "add", corrupting_add)
    out = tmp_path / "out"

    with pytest.raises(pq.PredequantError, match=r"proj1.*(bit|differ)"):
        pq.predequantize(src.parent, out)

    assert nothing_left(out)


def test_a_failing_cosine_gate_removes_the_model_it_built(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    out = tmp_path / "out"

    with pytest.raises(pq.PredequantError, match="cosine"):
        pq.predequantize(src.parent, out, embed=drifting)

    assert nothing_left(out)


def test_a_gate_that_crashes_has_not_approved_the_model(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    out = tmp_path / "out"

    def crash(model, tokenizer, questions):
        raise RuntimeError("session failed to load")

    with pytest.raises(pq.PredequantError, match="session failed to load"):
        pq.predequantize(src.parent, out, embed=crash)

    assert nothing_left(out)


def test_the_cosine_gate_embeds_with_the_source_and_the_staged_model_in_that_order(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    seen = []

    def spy(model, tokenizer, questions):
        seen.append((Path(model).name, Path(tokenizer).name, len(questions)))
        return unit_rows(len(questions), seed=0)

    pq.predequantize(src.parent, tmp_path / "out", embed=spy)

    assert seen == [("model_q8.onnx", "tokenizer.json", len(pq.bo.GATE_QUESTIONS)),
                    ("model_fp32.onnx", "tokenizer.json", len(pq.bo.GATE_QUESTIONS))]


UNSUPPORTED = {
    "four-bit": ({"bits": 4}, "bits"),
    "accuracy-level-4": ({"accuracy_level": 4}, "accuracy_level"),
    "g_idx": ({"g_idx": True}, "g_idx"),
    "bias": ({"bias": True}, "bias"),
}


@pytest.mark.parametrize("options, message", UNSUPPORTED.values(), ids=UNSUPPORTED.keys())
def test_nodes_the_formula_does_not_cover_are_refused_before_anything_is_written(tmp_path, options, message):
    src = write_nbits_embedder(tmp_path / "src", zero_points=(False,), **options)
    out = tmp_path / "out"

    with pytest.raises(pq.PredequantError, match=message):
        pq.predequantize(src.parent, out)

    assert nothing_left(out)


def test_float_zero_points_are_refused(tmp_path):
    src = write_nbits_embedder(tmp_path / "src", zero_points=("float",))

    with pytest.raises(pq.PredequantError, match="zero"):
        pq.predequantize(src.parent, tmp_path / "out")


def test_weights_shared_by_two_nodes_are_refused(tmp_path):
    import onnx

    src = write_nbits_embedder(tmp_path / "src", zero_points=(False, False))
    proto = onnx.load(str(src), load_external_data=True)
    proto.graph.node[-1].input[1] = "proj0.B"
    onnx.save_model(proto, str(src))

    with pytest.raises(pq.PredequantError, match="shared"):
        pq.predequantize(src.parent, tmp_path / "out")


def test_a_model_without_matmulnbits_nodes_is_refused(tmp_path):
    plain = write_tiny_embedder(tmp_path / "plain")
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(plain, src / SOURCE_GRAPH)
    shutil.copy(plain.with_name("model.onnx_data"), src / "model.onnx_data")
    shutil.copy(plain.with_name("tokenizer.json"), src / "tokenizer.json")

    with pytest.raises(pq.PredequantError, match="no MatMulNBits"):
        pq.predequantize(src, tmp_path / "out")


def test_a_missing_tokenizer_is_refused(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    (src.parent / "tokenizer.json").unlink()

    with pytest.raises(pq.PredequantError, match="tokenizer"):
        pq.predequantize(src.parent, tmp_path / "out")


def test_a_missing_source_model_is_refused(tmp_path):
    with pytest.raises(pq.PredequantError, match="model_q8.onnx"):
        pq.predequantize(tmp_path, tmp_path / "out")


# ---------------------------------------------------------------- existing output and the command line

def test_an_existing_output_is_not_overwritten_without_force_and_is_replaced_with_it(tmp_path):
    src = write_nbits_embedder(tmp_path / "src")
    out = tmp_path / "out"
    pq.predequantize(src.parent, out)
    first = digest(out / "model_fp32.onnx.data")

    with pytest.raises(pq.PredequantError, match="--force"):
        pq.predequantize(src.parent, out)
    assert digest(out / "model_fp32.onnx.data") == first

    pq.predequantize(src.parent, out, force=True)
    assert {p.name for p in out.iterdir()} == OUT_FILES and digest(out / "model_fp32.onnx.data") == first


def test_a_failed_forced_rebuild_removes_the_old_model_too(tmp_path):
    """A rebuild that fails must not leave the previous model standing as if it had passed this run."""
    src = write_nbits_embedder(tmp_path / "src")
    out = tmp_path / "out"
    pq.predequantize(src.parent, out)

    with pytest.raises(pq.PredequantError):
        pq.predequantize(src.parent, out, force=True, embed=drifting)

    assert nothing_left(out)


def test_the_command_line_builds_and_prints_the_verdict(tmp_path, capsys):
    src = write_nbits_embedder(tmp_path / "src")

    pq.main(["--src", str(src.parent), "--out", str(tmp_path / "out")])

    assert {p.name for p in (tmp_path / "out").iterdir()} == OUT_FILES
    assert "2/2 nodes bit-exact" in capsys.readouterr().out


def test_the_command_line_exits_with_the_reason_when_a_gate_fails(tmp_path, monkeypatch):
    src = write_nbits_embedder(tmp_path / "src")
    monkeypatch.setattr(pq.bo, "embed_questions", drifting)

    with pytest.raises(SystemExit) as raised:
        pq.main(["--src", str(src.parent), "--out", str(tmp_path / "out")])

    assert "cosine" in str(raised.value)
    assert nothing_left(tmp_path / "out")


def test_the_command_line_stops_before_the_slow_build_when_the_gate_cannot_run(tmp_path, monkeypatch):
    src = write_nbits_embedder(tmp_path / "src")
    monkeypatch.setattr(pq.bo, "missing_gate_dependencies", lambda: ["tokenizers"])

    with pytest.raises(SystemExit) as raised:
        pq.main(["--src", str(src.parent), "--out", str(tmp_path / "out")])

    assert "tokenizers" in str(raised.value) and not (tmp_path / "out").exists()
