"""Pre-dequantize the 8-bit query embedder: every MatMulNBits weight becomes a float32 initializer, ONCE, offline.

    python scripts/predequantize_embedder.py [--src models/qwen3-embedding-0.6b-q8]
                                             [--out models/qwen3-embedding-0.6b-fp32] [--force] [--threads N]

Why. ONNX Runtime 1.29 runs ``MatMulNBits`` at accuracy levels 0-3 by dequantizing the whole weight matrix to fp32 on EVERY
call (the int8 fast path is level 4, which failed its retrieval-parity gate, docs/v2/M5_DECISIONS.md section 1.4). The
shipped model therefore spends most of a query rebuilding weights that never change. Doing that once, here, turns each of
the 196 nodes into a plain ``MatMul`` over an fp32 initializer: about 4x faster on one thread with the SAME values (the
dequantization is bit-exact against ONNX Runtime's own). The price is memory and disk: about 2.3 GB of weights instead of
1.0 GB. Whether the price is worth paying on the production machine is decided by a timing/RSS window, not here.

In. ``--src``: a directory with ``model_q8.onnx`` (+ its ``model_q8.onnx_data`` sidecar when the weights are external, as
the Docker build writes it, or a single self-contained file, as the local build does) and ``tokenizer.json``. Nothing is
downloaded and the source is never modified. Out (``--out``): ``model_fp32.onnx`` (the graph, about 1 MB),
``model_fp32.onnx.data`` (every large tensor, 4096-aligned, the embedding table included, so the source can be deleted),
``tokenizer.json`` and ``model_fp32.fidelity.json`` (variant, node count, worst per-node difference, the cosine gate, the
onnxruntime version and the hash of the source model).

Dequantization (the MatMulNBits contract, 8 bits). ``B`` is uint8 ``(N, k_blocks, block_size)``, one byte per weight, blocks
along K; ``scales`` is float32 ``(N, k_blocks)``; the optional uint8 ``zero_points`` is ``(N, k_blocks)`` and defaults to
128. ``W[n, kb*bs + j] = (B[n, kb, j] - zero_point[n, kb]) * scales[n, kb]`` and the node computes ``Y = A @ W.T``. The
replacement is ``MatMul(A, W.T)`` with ``W.T`` stored as ``(K, N)``. Anything else (4-bit or any other width, float zero
points, ``g_idx``, ``bias``, ``accuracy_level`` above 0, a weight shared by two nodes, a MatMulNBits in a subgraph) is
refused: the formula above is not known to describe it.

Gates (a model that fails one is never left on disk; everything is built in a staging directory and moved into place only
after all of them passed, the fidelity file last):

1. Per node, on the BYTES THAT WERE WRITTEN: the weight is read back from the new sidecar and compared, bit for bit, with
   ONNX Runtime's own dequantization of the source node (the original single ``MatMulNBits`` node, accuracy level unset,
   fed an identity matrix: ``I @ W.T`` is ``W.T`` exactly, so its output IS the dequantized weight). No value of this
   script's own formula takes part in the comparison, so a wrong formula and a wrong write fail alike.
2. The rest of the graph is unchanged (every other node, and every other tensor byte for byte), and no MatMulNBits is left.
3. The end-to-end cosine gate of ``build_onnx_embedder.py`` (mean >= 0.998, min >= 0.997 over the 53 example questions with
   the serving prompt) between the source and the new model.

Docker model stage. Needs onnx, onnxruntime, numpy and tokenizers (pin them to the serving image's versions; see the
Dockerfile) and the sibling ``build_onnx_embedder.py``, which it imports for the gate. About 2.4 GB of RAM at the peak of the
cosine gate (the new model's session); Linux RSS and the remote builder's headroom have not been measured. The output does
not depend on how the source stores its weights (single file or sidecar: byte-identical results); a single-file source is
held in memory twice while it is processed (about 2 GB), the sidecar layout of the Docker build is not.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:  # the builder sits beside this file (both are copied into the Docker model stage)
    sys.path.insert(0, str(SCRIPTS_DIR))
import build_onnx_embedder as bo  # noqa: E402

if TYPE_CHECKING:
    from onnx import ModelProto, NodeProto, TensorProto

VARIANT = "fp32"
FORMAT_VERSION = 1
SOURCE_MODEL = bo.MODEL_NAME                      # model_q8.onnx
MODEL_NAME = "model_fp32.onnx"
DATA_NAME = MODEL_NAME + ".data"
FIDELITY_NAME = "model_fp32.fidelity.json"
TOKENIZER_NAME = "tokenizer.json"
STAGING_NAME = ".model_fp32.building"
OUTPUT_NAMES = (MODEL_NAME, DATA_NAME, FIDELITY_NAME, TOKENIZER_NAME)
DEFAULT_SRC, DEFAULT_OUT = "models/qwen3-embedding-0.6b-q8", "models/qwen3-embedding-0.6b-fp32"

BITS = 8
DEFAULT_ZERO_POINT = 2 ** (BITS - 1)              # the contract's default when no zero_points input is given
MIN_BLOCK_SIZE = 16
ALIGN = 4096                                      # sidecar offsets, like onnx's own external-data helper
EXTERNALIZE_MIN_BYTES = 1 << 20                   # a kept tensor smaller than this stays inline in the graph file
HASH_CHUNK, COMPARE_CHUNK = 1 << 20, 1 << 26
ALLOWED_ATTRIBUTES = frozenset({"K", "N", "bits", "block_size", "accuracy_level"})
INPUT_A, INPUT_B, INPUT_SCALES, INPUT_ZERO_POINTS, INPUT_G_IDX, INPUT_BIAS = range(6)
PROGRESS_EVERY = 25

Log = Callable[[str], None]
Embed = Callable[[Path, Path, Sequence[str]], np.ndarray]


class PredequantError(Exception):
    """A gate or a precondition failed; nothing was left on disk."""


@dataclass(frozen=True)
class NodeSpec:
    """One MatMulNBits node: the tensors it reads (by initializer name) and its shape."""

    name: str
    a: str
    output: str
    b: str
    scales: str
    zero_points: str          # "" when the node has none
    k: int
    n: int
    block_size: int

    @property
    def k_blocks(self) -> int:
        return -(-self.k // self.block_size)


# ------------------------------------------------------------------------------------------ the formula

def dequantize(b_q: np.ndarray, scales: np.ndarray, k: int, zero_points: np.ndarray | None = None) -> np.ndarray:
    """W ``(N, K)`` float32 from B ``(N, k_blocks, block)`` uint8, scales ``(N, k_blocks)`` and optional zero points.

    ``(B - zero_point) * scale`` in float32: the subtraction is exact (integers below 256), the product rounds once, which
    is what ONNX Runtime computes. The padding of the last block (K not a multiple of the block size) is cut off."""
    n, k_blocks, block = b_q.shape
    zero = (np.float32(DEFAULT_ZERO_POINT) if zero_points is None
            else np.asarray(zero_points).reshape(n, k_blocks, 1).astype(np.float32))
    w = (b_q.astype(np.float32) - zero) * np.asarray(scales, dtype=np.float32)[:, :, None]
    return np.ascontiguousarray(w.reshape(n, k_blocks * block)[:, :k])


# ------------------------------------------------------------------------------------------ reading the source

def load_inline(tensor: "TensorProto", base_dir: Path) -> None:
    """Replace a tensor's reference to an external file by the values themselves, IN PLACE (a no-op for an inline one)."""
    from onnx import TensorProto
    from onnx.external_data_helper import load_external_data_for_tensor, uses_external_data

    if uses_external_data(tensor):
        load_external_data_for_tensor(tensor, str(base_dir))
        tensor.data_location = TensorProto.DEFAULT
        del tensor.external_data[:]


def tensor_array(tensor: "TensorProto", base_dir: Path) -> np.ndarray:
    """The tensor's values; external data is read from ``base_dir`` WITHOUT keeping it on the proto (a copy is loaded, the
    original is not touched, so a 600 MB table is not held in memory for the rest of the run)."""
    from onnx import TensorProto, numpy_helper
    from onnx.external_data_helper import uses_external_data

    if uses_external_data(tensor):
        copy = TensorProto()
        copy.CopyFrom(tensor)
        load_inline(copy, base_dir)
        return numpy_helper.to_array(copy)
    return numpy_helper.to_array(tensor)


def _int_attributes(node: "NodeProto", label: str) -> dict[str, int]:
    unknown = sorted(a.name for a in node.attribute if a.name not in ALLOWED_ATTRIBUTES)
    if unknown:
        raise PredequantError(f"{label}: attribute(s) {unknown} are not covered by the dequantization formula")
    ints = {a.name: a.i for a in node.attribute if a.type == a.INT}
    missing = sorted(({"K", "N", "bits", "block_size"} | {a.name for a in node.attribute}) - set(ints))
    if missing:
        raise PredequantError(f"{label}: attribute(s) {missing} missing or not integers")
    return ints


def node_spec(node: "NodeProto") -> NodeSpec:
    """Validate one MatMulNBits node against what the formula covers (else :class:`PredequantError`)."""
    label = f"node {node.name or node.output[0]}"
    attrs = _int_attributes(node, label)
    if attrs["bits"] != BITS:
        raise PredequantError(f"{label}: bits={attrs['bits']}; only {BITS}-bit weights are covered")
    if attrs.get("accuracy_level", 0):
        raise PredequantError(f"{label}: accuracy_level={attrs['accuracy_level']}; the pre-dequantized model is built from "
                              "the level-0 8-bit model (rebuild the source with --accuracy-level 0)")
    block = attrs["block_size"]
    if block < MIN_BLOCK_SIZE or block & (block - 1):
        raise PredequantError(f"{label}: block_size={block} is not a power of two of at least {MIN_BLOCK_SIZE}")
    inputs = list(node.input) + [""] * (INPUT_BIAS + 1 - len(node.input))
    if len(node.input) > INPUT_BIAS + 1 or not all(inputs[:INPUT_ZERO_POINTS]):
        raise PredequantError(f"{label}: inputs {list(node.input)} do not fit (A, B, scales[, zero_points])")
    for index, kind in ((INPUT_G_IDX, "g_idx"), (INPUT_BIAS, "bias")):
        if inputs[index]:
            raise PredequantError(f"{label}: a {kind} input is not covered by the dequantization formula")
    return NodeSpec(name=node.name or node.output[0], a=inputs[INPUT_A], output=node.output[0], b=inputs[INPUT_B],
                    scales=inputs[INPUT_SCALES], zero_points=inputs[INPUT_ZERO_POINTS], k=attrs["K"], n=attrs["N"],
                    block_size=block)


def _has_subgraph(proto: "ModelProto") -> bool:
    return any(attr.HasField("g") or attr.graphs for node in proto.graph.node for attr in node.attribute)


def collect_specs(proto: "ModelProto") -> list[NodeSpec]:
    """The validated spec of every MatMulNBits node, in graph order."""
    if bo.count_matmul_nbits(proto) == 0:
        raise PredequantError(f"no MatMulNBits nodes in {SOURCE_MODEL} (not a quantized model?)")
    if _has_subgraph(proto):
        raise PredequantError("graphs with subgraphs (If / Loop / Scan) are not covered")
    specs = [node_spec(node) for node in proto.graph.node if node.op_type == bo.MATMUL_NBITS]
    initializers = {init.name for init in proto.graph.initializer}
    uses: dict[str, int] = {}
    for node in proto.graph.node:
        for name in node.input:
            uses[name] = uses.get(name, 0) + 1
    for spec in specs:
        for tensor_name in filter(None, (spec.b, spec.scales, spec.zero_points)):
            if tensor_name not in initializers:
                raise PredequantError(f"node {spec.name}: {tensor_name} is not an initializer")
            if uses[tensor_name] != 1:
                raise PredequantError(f"node {spec.name}: {tensor_name} is shared with another node")
    return specs


def read_weights(spec: NodeSpec, initializers: dict, base_dir: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """(B, scales, zero_points or None) of a node, checked against its attributes."""
    b_q = tensor_array(initializers[spec.b], base_dir)
    scales = tensor_array(initializers[spec.scales], base_dir)
    zero_points = tensor_array(initializers[spec.zero_points], base_dir) if spec.zero_points else None
    if b_q.dtype != np.uint8 or b_q.shape != (spec.n, spec.k_blocks, spec.block_size * BITS // 8):
        raise PredequantError(f"node {spec.name}: B is {b_q.dtype}{list(b_q.shape)}, expected uint8 "
                              f"{[spec.n, spec.k_blocks, spec.block_size]}")
    if scales.dtype != np.float32 or scales.shape != (spec.n, spec.k_blocks):
        raise PredequantError(f"node {spec.name}: scales are {scales.dtype}{list(scales.shape)}, expected float32 "
                              f"{[spec.n, spec.k_blocks]}")
    if zero_points is not None and (zero_points.dtype != np.uint8 or zero_points.size != spec.n * spec.k_blocks):
        raise PredequantError(f"node {spec.name}: zero_points are {zero_points.dtype}{list(zero_points.shape)}; only uint8 "
                              f"with {spec.n * spec.k_blocks} values are covered")
    return b_q, scales, zero_points


# ------------------------------------------------------------------------------------------ writing the new model

class SidecarWriter:
    """Appends tensors to ONE sidecar file (4096-aligned offsets) and rewrites their protos to point at it."""

    def __init__(self, path: Path) -> None:
        self.path, self.offset = path, 0
        self._file = open(path, "wb")

    def __enter__(self) -> "SidecarWriter":
        return self

    def __exit__(self, *exc) -> None:
        self._file.close()

    def add(self, tensor: "TensorProto", data: bytes) -> None:
        from onnx.external_data_helper import set_external_data

        pad = (-self.offset) % ALIGN
        if pad:
            self._file.write(b"\0" * pad)
            self.offset += pad
        self._file.write(data)
        set_external_data(tensor, location=self.path.name, offset=self.offset, length=len(data))  # needs raw_data
        tensor.ClearField("raw_data")
        self.offset += len(data)


def weight_name(b_name: str) -> str:
    """The new fp32 tensor's name: the shipped model's ``...weight_Q8`` becomes ``...weight_fp32``."""
    renamed = b_name.replace("weight_Q8", "weight_fp32")
    return renamed if renamed != b_name else b_name + "_fp32"


def replacement(spec: NodeSpec, node: "NodeProto", initializers: dict, src_dir: Path, writer: SidecarWriter):
    """(MatMul node, fp32 weight tensor) for one MatMulNBits node; the tensor's bytes are already in the sidecar."""
    from onnx import helper, numpy_helper

    b_q, scales, zero_points = read_weights(spec, initializers, src_dir)
    w_t = np.ascontiguousarray(dequantize(b_q, scales, spec.k, zero_points).T)   # (K, N): MatMul(A, W.T) == A @ W.T
    tensor = numpy_helper.from_array(w_t, weight_name(spec.b))
    writer.add(tensor, tensor.raw_data)
    matmul = helper.make_node("MatMul", [spec.a, tensor.name], [spec.output],
                              name=node.name.replace("MatMul_Q8", "MatMul_fp32") if node.name else "")
    return matmul, tensor


def carry_over(tensor: "TensorProto", src_dir: Path, writer: SidecarWriter, min_bytes: int) -> "TensorProto":
    """A kept initializer, detached from the source sidecar: large ones move to the new sidecar, small ones are inline."""
    from onnx import TensorProto, numpy_helper

    load_inline(tensor, src_dir)                                # raw_data now; the reference to the old sidecar is gone
    if not tensor.HasField("raw_data"):                         # typed fields (float_data...): normal for small constants
        array = numpy_helper.to_array(tensor)
        if array.nbytes >= min_bytes:
            tensor.CopyFrom(numpy_helper.from_array(array, tensor.name))
    if tensor.HasField("raw_data") and len(tensor.raw_data) >= min_bytes:
        writer.add(tensor, tensor.raw_data)
    kept = TensorProto()
    kept.CopyFrom(tensor)
    return kept


def write_model(src_dir: Path, proto: "ModelProto", specs: list[NodeSpec], stage: Path, min_bytes: int) -> dict[str, str]:
    """Write ``stage/model_fp32.onnx`` + its sidecar; returns {node output: new weight name}. ``proto`` is consumed."""
    import onnx
    from onnx import NodeProto

    initializers = {init.name: init for init in proto.graph.initializer}
    dropped = {name for spec in specs for name in (spec.b, spec.scales, spec.zero_points) if name}
    new_nodes, new_inits, weights = [], [], {}
    pending = iter(specs)
    with SidecarWriter(stage / DATA_NAME) as writer:
        for node in proto.graph.node:
            if node.op_type == bo.MATMUL_NBITS:
                matmul, tensor = replacement(next(pending), node, initializers, src_dir, writer)
                new_nodes.append(matmul)
                new_inits.append(tensor)
                weights[matmul.output[0]] = tensor.name
            else:
                copy = NodeProto()
                copy.CopyFrom(node)
                new_nodes.append(copy)
        for init in proto.graph.initializer:
            if init.name not in dropped:
                new_inits.append(carry_over(init, src_dir, writer, min_bytes))
    del proto.graph.node[:]
    proto.graph.node.extend(new_nodes)
    del proto.graph.initializer[:]
    proto.graph.initializer.extend(new_inits)
    onnx.save_model(proto, str(stage / MODEL_NAME))
    return weights


# ------------------------------------------------------------------------------------------ gate 1 and 2: the written bytes

def ort_weight_t(spec: NodeSpec, b_q: np.ndarray, scales: np.ndarray, zero_points: np.ndarray | None,
                 threads: int = 0) -> np.ndarray:
    """ONNX Runtime's own dequantized ``W.T`` ``(K, N)`` for the original node: the single MatMulNBits node (accuracy level
    unset, the shipped configuration) run on an identity matrix, ``I @ W.T`` being ``W.T`` exactly."""
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper

    inputs = ["A", "B", "S"] + (["ZP"] if zero_points is not None else [])
    init = [numpy_helper.from_array(b_q, "B"), numpy_helper.from_array(scales, "S")]
    if zero_points is not None:
        init.append(numpy_helper.from_array(zero_points, "ZP"))
    node = helper.make_node("MatMulNBits", inputs, ["Y"], domain="com.microsoft", K=spec.k, N=spec.n, bits=BITS,
                            block_size=spec.block_size)
    graph = helper.make_graph([node], "oracle", [helper.make_tensor_value_info("A", TensorProto.FLOAT, [spec.k, spec.k])],
                              [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [spec.k, spec.n])], initializer=init)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 14), helper.make_opsetid("com.microsoft", 1)])
    options = ort.SessionOptions()
    options.log_severity_level = 3
    if threads:
        options.intra_op_num_threads = threads
    session = ort.InferenceSession(model.SerializeToString(), options, providers=["CPUExecutionProvider"])
    return session.run(None, {"A": np.eye(spec.k, dtype=np.float32)})[0]


def same_bytes(a: np.ndarray, b: np.ndarray) -> bool:
    """Identical dtype, shape and bytes (NaN-safe), compared in chunks so a 600 MB table is not copied."""
    if a.dtype != b.dtype or a.shape != b.shape:
        return False
    x, y = np.ascontiguousarray(a).reshape(-1).view(np.uint8), np.ascontiguousarray(b).reshape(-1).view(np.uint8)
    return all(np.array_equal(x[i:i + COMPARE_CHUNK], y[i:i + COMPARE_CHUNK]) for i in range(0, x.size, COMPARE_CHUNK))


def check_structure(written: "ModelProto", src: "ModelProto", specs: list[NodeSpec], weights: dict[str, str]) -> None:
    """Gate 2, graph part: every node but the replaced ones is unchanged, each replaced one reads its own new weight."""
    replaced = {spec.output: spec for spec in specs}
    old = [n for n in src.graph.node if n.op_type != bo.MATMUL_NBITS]
    new = [n for n in written.graph.node if not (n.output and n.output[0] in replaced)]
    if len(new) != len(old) or any(a.SerializeToString() != b.SerializeToString() for a, b in zip(old, new, strict=True)):
        raise PredequantError("the graph around the replaced nodes changed")
    by_output = {n.output[0]: n for n in written.graph.node}
    for spec in specs:
        node = by_output.get(spec.output)
        if node is None or node.op_type != "MatMul" or list(node.input) != [spec.a, weights[spec.output]]:
            raise PredequantError(f"node {spec.name}: its replacement does not read its own dequantized weight")
    if bo.count_matmul_nbits(written):
        raise PredequantError("MatMulNBits nodes are left in the new graph")


def verify_written(stage: Path, src: "ModelProto", src_dir: Path, specs: list[NodeSpec], weights: dict[str, str],
                   threads: int, log: Log) -> dict:
    """Gates 1 and 2 on what is on disk in ``stage``: returns the per-run summary of the weights."""
    import onnx

    written = onnx.load(str(stage / MODEL_NAME), load_external_data=False)
    check_structure(written, src, specs, weights)
    out_inits, src_inits = {i.name: i for i in written.graph.initializer}, {i.name: i for i in src.graph.initializer}
    dropped = {name for spec in specs for name in (spec.b, spec.scales, spec.zero_points) if name}
    for name in (set(src_inits) - dropped):
        if name not in out_inits or not same_bytes(tensor_array(src_inits[name], src_dir),
                                                  tensor_array(out_inits[name], stage)):
            raise PredequantError(f"tensor {name} was not carried over unchanged")
    if set(out_inits) != (set(src_inits) - dropped) | set(weights.values()):
        raise PredequantError("the new model's tensors are not the source's minus the replaced weights plus the new ones")
    worst, exact = 0.0, 0
    for index, spec in enumerate(specs, 1):
        expected = ort_weight_t(spec, *read_weights(spec, src_inits, src_dir), threads=threads)
        got = tensor_array(out_inits[weights[spec.output]], stage)            # the bytes that were written
        if got.shape != expected.shape or got.dtype != expected.dtype:
            raise PredequantError(f"node {spec.name}: the written weight is {got.dtype}{list(got.shape)}, "
                                  f"onnxruntime's is {expected.dtype}{list(expected.shape)}")
        diff = float(np.abs(got - expected).max())
        if not np.array_equal(got, expected):
            raise PredequantError(f"node {spec.name}: the written weight differs from onnxruntime's own dequantization "
                                  f"(max abs diff {diff:.3g}, {int((got != expected).sum())} of {got.size} values)")
        worst, exact = max(worst, diff), exact + 1
        if index % PROGRESS_EVERY == 0 or index == len(specs):
            log(f"  verified {index}/{len(specs)} nodes against onnxruntime")
    return {"dequantized_nodes": len(specs), "nodes_bit_exact": exact, "max_node_abs_diff": worst}


# ------------------------------------------------------------------------------------------ gate 3, provenance, publishing

def hash_source(src_dir: Path, proto: "ModelProto") -> dict:
    """sha256 of every file the source model consists of, and ONE combined digest: the bytes of the graph file followed by
    those of its external-data files in name order (so ``sha256 graph data | sha256sum``-style reproduction is possible)."""
    locations = {e.value for init in proto.graph.initializer for e in init.external_data if e.key == "location"}
    combined, files = hashlib.sha256(), {}
    for name in [SOURCE_MODEL, *sorted(locations)]:
        each = hashlib.sha256()
        with open(src_dir / name, "rb") as handle:
            while chunk := handle.read(HASH_CHUNK):
                each.update(chunk)
                combined.update(chunk)
        files[name] = each.hexdigest()
    return {"model": SOURCE_MODEL, "sha256": combined.hexdigest(), "files": files}


def run_cosine_gate(source: Path, staged: Path, tokenizer: Path, questions: Sequence[str], embed: Embed) -> dict:
    """The 53-question gate of the builder between the source and the new model (one session alive at a time)."""
    reference = embed(source, tokenizer, questions)
    candidate = embed(staged, tokenizer, questions)
    gate = bo.cosine_gate(candidate, reference)
    if not gate["passed"]:
        raise PredequantError(f"cosine to the 8-bit source: mean {gate['cosine_mean']:.5f} (>= {gate['mean_threshold']}), "
                              f"min {gate['cosine_min']:.5f} (>= {gate['min_threshold']})")
    return gate


def environment() -> dict:
    import onnx
    import onnxruntime

    return {"onnxruntime": onnxruntime.__version__, "onnx": onnx.__version__, "numpy": np.__version__,
            "machine": platform.machine()}


def block_size_of(specs: list[NodeSpec]) -> int | list[int]:
    sizes = sorted({spec.block_size for spec in specs})
    return sizes[0] if len(sizes) == 1 else sizes


def publish(stage: Path, src_tokenizer: Path, out_dir: Path, report: dict) -> None:
    """Move the verified files into place; the fidelity file goes last, so its presence means the rest is complete."""
    out_dir.mkdir(parents=True, exist_ok=True)
    os.replace(stage / DATA_NAME, out_dir / DATA_NAME)
    os.replace(stage / MODEL_NAME, out_dir / MODEL_NAME)
    shutil.copyfile(src_tokenizer, out_dir / TOKENIZER_NAME)
    (out_dir / FIDELITY_NAME).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")


def prepare(src_dir: Path, out_dir: Path, force: bool, injected_embed: bool) -> None:
    """Preconditions, all checked before the slow part: dependencies, the source files, an existing output."""
    missing = [name for name in bo.missing_gate_dependencies() if not (injected_embed and name == "tokenizers")]
    if missing:
        raise PredequantError(f"needs {', '.join(missing)} in this environment (install it before the build)")
    for name in (SOURCE_MODEL, TOKENIZER_NAME):
        if not (src_dir / name).is_file():
            raise PredequantError(f"{src_dir / name} is missing (--src is the directory of the 8-bit model and its tokenizer)")
    if src_dir.resolve() == out_dir.resolve():
        raise PredequantError("--out must be another directory than --src")
    existing = [out_dir / name for name in OUTPUT_NAMES[:3] if (out_dir / name).exists()]    # the tokenizer is just copied
    if existing and not force:
        raise PredequantError(f"{existing[0]} exists; use --force to replace it (the old model is removed first, so a "
                              "failed rebuild never leaves it standing)")
    for name in OUTPUT_NAMES:                                    # --force: the old files go first, the tokenizer too
        (out_dir / name).unlink(missing_ok=True)


def predequantize(src_dir: Path | str, out_dir: Path | str, *, force: bool = False,
                  questions: Sequence[str] = bo.GATE_QUESTIONS, embed: Embed | None = None, threads: int = 0,
                  externalize_min_bytes: int = EXTERNALIZE_MIN_BYTES, log: Log | None = None) -> dict:
    """Build ``out_dir`` from the 8-bit model in ``src_dir`` and return its fidelity report; raises
    :class:`PredequantError` (with nothing left in ``out_dir``) when a precondition or any gate fails."""
    src_dir, out_dir, log = Path(src_dir), Path(out_dir), log or (lambda message: None)
    prepare(src_dir, out_dir, force, injected_embed=embed is not None)
    embed = embed or (lambda model, tokenizer, qs: bo.embed_questions(model, tokenizer, qs, threads))
    created = not out_dir.exists()
    stage = out_dir / STAGING_NAME
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        report = _build(src_dir, stage, questions, embed, threads, externalize_min_bytes, log)
        publish(stage, src_dir / TOKENIZER_NAME, out_dir, report)
        return report
    except PredequantError:
        raise
    except Exception as exc:  # a gate that crashed (an ORT session error, a full disk) has not approved the model
        raise PredequantError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if created and not any(out_dir.iterdir()):
            out_dir.rmdir()


def _build(src_dir: Path, stage: Path, questions: Sequence[str], embed: Embed, threads: int, min_bytes: int,
           log: Log) -> dict:
    started = time.time()
    proto = bo.load_graph(src_dir / SOURCE_MODEL)
    specs = collect_specs(proto)
    log(f"{len(specs)} MatMulNBits nodes to dequantize (of {len(proto.graph.node)} nodes)")
    source = hash_source(src_dir, proto)
    reference = type(proto)()
    reference.CopyFrom(proto)                                    # write_model consumes ``proto``
    weights = write_model(src_dir, proto, specs, stage, min_bytes)
    log(f"wrote {stage / DATA_NAME} ({(stage / DATA_NAME).stat().st_size / 1e9:.2f} GB) in {time.time() - started:.0f}s")
    summary = verify_written(stage, reference, src_dir, specs, weights, threads, log)
    del reference, proto
    gate = run_cosine_gate(src_dir / SOURCE_MODEL, stage / MODEL_NAME, src_dir / TOKENIZER_NAME, questions, embed)
    return {"variant": VARIANT, "format": FORMAT_VERSION, "model": MODEL_NAME, "data": DATA_NAME, "bits": BITS,
            "block_size": block_size_of(specs), **summary, "source": source, "gate": gate, **environment()}


# ------------------------------------------------------------------------------------------ command line

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=DEFAULT_SRC, help="directory of model_q8.onnx (+ sidecar) and tokenizer.json")
    ap.add_argument("--out", default=DEFAULT_OUT, help="directory to write the pre-dequantized model to")
    ap.add_argument("--force", action="store_true", help="replace an existing output (removed first)")
    ap.add_argument("--threads", type=int, default=0, help="onnxruntime intra-op threads for the gates (0 = its default)")
    return ap


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    started = time.time()

    def log(message: str) -> None:
        print(message, flush=True)

    try:
        report = predequantize(args.src, args.out, force=args.force, threads=args.threads, log=log)
    except PredequantError as exc:
        sys.exit(f"PREDEQUANTIZE FAILED: {exc} — nothing was written")
    gate = report["gate"]
    log(f"DEQUANT VERIFICATION: {report['nodes_bit_exact']}/{report['dequantized_nodes']} nodes bit-exact vs "
        f"onnxruntime's own dequantization (read back from the written sidecar); max abs diff {report['max_node_abs_diff']}")
    log(f"cosine gate: mean {gate['cosine_mean']:.9f}, min {gate['cosine_min']:.9f} over {gate['questions']} questions "
        f"(thresholds {gate['mean_threshold']} / {gate['min_threshold']})")
    size = sum(p.stat().st_size for p in Path(args.out).glob("model_fp32.onnx*"))
    log(f"done: {args.out} ({size / 1e6:.0f} MB) in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
