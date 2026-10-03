"""Build the torch-free query embedder: 8-bit weight-only ONNX of Qwen3-Embedding-0.6B.

    python scripts/build_onnx_embedder.py [--out models/qwen3-embedding-0.6b-q8]
                                          [--bits 8] [--block-size 128] [--accuracy-level 0]
                                          [--verify] [--verify-patched] [--keep-unpatched] [--force]

1. Downloads the public fp32 ONNX export (onnx-community/Qwen3-Embedding-0.6B-ONNX:
   onnx/model.onnx + onnx/model.onnx_data, ~2.4 GB) and its tokenizer.json.
2. Applies MatMulNBits block-wise weight-only quantization (onnxruntime's own
   quantizer; activations stay fp32 in memory) -> a single self-contained model_q8.onnx.
   --accuracy-level (default 0 = the build that is live today) is written to the
   ``accuracy_level`` attribute of every MatMulNBits node. Without it ONNX Runtime
   dequantizes every weight to fp32 on every call (1.2 s per question); with 4 the kernel
   quantizes the activations to int8 instead (0.3 s). Level 4 is OPT-IN: it has not
   cleared its corpus-parity gate (docs/v2/M5_DECISIONS.md section 1.4), so nothing may
   pass it in the Dockerfile until the owner decides. The level is passed to the quantizer
   AND set again on the quantized graph (set_accuracy_level), so a quantizer that stops
   honouring it cannot silently ship the wrong model.
3. --verify (default on when the local sentence-transformers model is cached):
   embeds the gold-benchmark questions with BOTH backends and refuses to
   keep the file unless cosine(min) >= 0.98. The result is written next to the
   model as fidelity.json so the number ships with the artifact. Needs torch and the
   semigraph package (not available in the Docker model stage).
4. --verify-patched (self-contained; the gate that runs in the Docker model stage):
   (a) fails unless every MatMulNBits node has accuracy_level == the requested level;
   (b) writes a temporary UNPATCHED twin (level 0; a small graph file that points at the
   same weights sidecar, so no second copy of the weights), embeds 53 realistic
   questions with both models one after the other, and fails unless the cosine between
   the two has mean >= 0.998 and min >= 0.997. The twin is always deleted. The result is
   written next to the model as patch_fidelity.json. It is checked BEFORE the slow build
   starts that onnxruntime, onnx and tokenizers are importable.
5. --keep-unpatched leaves that level-0 twin as model_q8.unpatched.onnx next to the model
   (a graph file that points at the same weights sidecar) so a machine that has only onnxruntime can still time
   the baseline: scripts/embedder_timing.py <model_q8.unpatched.onnx> <model_q8.onnx>.

On a failed gate the files THIS run built are deleted. A model that was already on disk
(reused: quantization is skipped unless --force) is never deleted, and is never patched in
place either: if it lacks the level the run warns, and --verify-patched fails with a
message to rebuild with --force (or to build into another --out directory).

Docker model stage (deploy: only this script is copied; no semigraph package, no torch,
no data/). ONLY after the owner approves level 4, replace the current ``--no-verify`` line with exactly:

    RUN pip install "onnx>=1.17" "onnxruntime>=1.29" "onnx-ir>=1.0" "huggingface_hub>=1.0" "numpy>=2" "tokenizers>=0.21"
    RUN python scripts/build_onnx_embedder.py --out /models/qwen3-embedding-0.6b-q8 --no-verify --accuracy-level 4 --verify-patched

(--no-verify only skips the torch fp32 comparison of step 3, which cannot run there;
--verify-patched is the gate. ``tokenizers`` is NOT installed by the current stage and the
gate needs it; the serving image pins tokenizers==0.23.2 in deploy/requirements-serve.txt.)

Run in the Docker build (deploy/Dockerfile) and locally. Idempotent: an
existing model_q8.onnx is reused unless --force.
"""

import argparse
import gc
import importlib.util
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from onnx import ModelProto

REPO = "onnx-community/Qwen3-Embedding-0.6B-ONNX"
FP32 = ("onnx/model.onnx", "onnx/model.onnx_data")
COS_MIN = 0.98

MODEL_NAME = "model_q8.onnx"
GATE_TWIN_NAME = "model_q8.gate-unpatched.onnx"
KEPT_TWIN_NAME = "model_q8.unpatched.onnx"
MATMUL_NBITS = "MatMulNBits"
ACCURACY_ATTR = "accuracy_level"
# Opt-in: 0 reproduces the build that is live today. Level 4 failed its pre-registered corpus-parity gate (top-1
# identical 0.933 < 0.95, docs/v2/M5_DECISIONS.md section 1.4); it ships only on the owner's decision.
ACCURACY_LEVEL_DEFAULT = 0
ACCURACY_LEVELS = (0, 1, 2, 3, 4)  # the MatMulNBits contrib op's range; 0 = unset (the op's own default)

# The --verify-patched gate (M5 decision 2.1): thresholds fixed before any run, on the 53 example questions with the
# serving prompt (patched vs a level-0 twin of the same weights). Measured 2026-10-03 on a patched copy of the shipped
# model: cosine mean 0.99877, min 0.99825 (docs/v2/research/m5-spikes/patch_gate_report.json).
PATCH_COS_MEAN_MIN = 0.998
PATCH_COS_MIN_MIN = 0.997
GATE_DEPENDENCIES = ("numpy", "onnx", "onnxruntime", "tokenizers")
MAX_TOKENS = 8192  # the serving backend's truncation (bounds memory; queries are tiny)

# Identical to semigraph.embeddings.QUERY_PROMPT (pinned by tests/test_build_onnx_embedder.py): no trailing space.
QUERY_PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"

# The 53 example questions of src/semigraph/artifacts/examples.json, inlined because the Docker model stage copies only
# this script (no semigraph package, no data/). A test pins the list to examples.json.
GATE_QUESTIONS: tuple[str, ...] = (
    "What was Nvidia's total revenue for the fiscal year ended January 28, 2024?",
    "What was Nvidia's total revenue for the fiscal year ended January 25, 2026?",
    "What was Nvidia's total revenue for the fiscal year ended January 26, 2025?",
    "What was Microsoft's total revenue for the fiscal year ended June 30, 2025?",
    "Which companies does Nvidia depend on for chip manufacturing and assembly?",
    "Which memory suppliers does Nvidia buy HBM or memory from?",
    "Which foundries does AMD rely on to manufacture its chips?",
    "Which hyperscaler is disclosed as a customer of AMD in the knowledge graph?",
    "Which BIS export-control rules is AMD affected by?",
    "What does Nvidia disclose about export controls affecting its sales to China?",
    "Did Nvidia stop disclosing any risk factors in its latest 10-K that appeared in earlier 10-Ks?",
    "What supply-chain related risks does TSMC disclose in its annual report?",
    "What geopolitical risks does ASML disclose?",
    "Compare the competition risks disclosed by AMD and Nvidia.",
    "By how much did Nvidia's annual revenue grow from the fiscal year ended January 28, 2024 to the "
    "fiscal year ended January 25, 2026?",
    "What is Samsung's total annual revenue according to its SEC filings?",
    "What did Nvidia's CEO say on the most recent earnings call?",
    "By what percentage did Nvidia's total revenue change from the fiscal year ended January 26, 2025 to "
    "the fiscal year ended January 25, 2026?",
    "By what percentage did AMD's net income change from the fiscal year ended December 28, 2024 to the "
    "fiscal year ended December 27, 2025?",
    "By what percentage did Intel's total revenue change from the fiscal year ended December 28, 2024 to "
    "the fiscal year ended December 27, 2025?",
    "By what percentage did Micron's research and development expense change from the fiscal year ended "
    "August 29, 2024 to the fiscal year ended August 28, 2025?",
    "By what percentage did Broadcom's total revenue change from the fiscal year ended November 3, 2024 "
    "to the fiscal year ended November 2, 2025?",
    "By what percentage did Qualcomm's net income change from the fiscal year ended September 25, 2022 to "
    "the fiscal year ended September 24, 2023?",
    "By what percentage did Meta's research and development expense change from the fiscal year ended "
    "December 31, 2024 to the fiscal year ended December 31, 2025?",
    "By what percentage did Microsoft's total revenue change from the fiscal year ended June 30, 2024 to "
    "the fiscal year ended June 30, 2025?",
    "By what percentage did Amazon's net income change from the fiscal year ended December 31, 2024 to "
    "the fiscal year ended December 31, 2025?",
    "By what percentage did Alphabet's total revenue change from the fiscal year ended December 31, 2021 "
    "to the fiscal year ended December 31, 2022?",
    "What were Nvidia's total revenue and net income for the fiscal year ended January 25, 2026?",
    "What were AMD's total revenue and research and development expense for the fiscal year ended December 28, 2024?",
    "What were Meta's total revenue and net income for the fiscal year ended December 31, 2025?",
    "What were Apple's total revenue and net income for the fiscal year ended September 28, 2024?",
    "What were Micron's total revenue and capital expenditures for the fiscal year ended August 28, 2025?",
    "What were Broadcom's total revenue and net income for the fiscal year ended October 29, 2023?",
    "What were Nvidia's research and development expense for the fiscal years ended January 25, 2026 and "
    "January 29, 2023?",
    "What were Intel's net income for the fiscal years ended December 27, 2025 and December 30, 2023?",
    "What were Qualcomm's total revenue for the fiscal years ended September 28, 2025 and September 26, 2021?",
    "What were Microsoft's net income for the fiscal years ended June 30, 2025 and June 30, 2023?",
    "What were Amazon's total revenue for the fiscal years ended December 31, 2025 and December 31, 2022?",
    "What were Alphabet's research and development expense for the fiscal years ended December 31, 2025 "
    "and December 31, 2023?",
    "What was TSMC's total revenue for the fiscal year ended December 31, 2025, and in which currency is it reported?",
    "What was ASML's net income for the fiscal year ended December 31, 2025, and in which currency is it reported?",
    "What did NVIDIA disclose about the BIS 50% affiliates rule?",
    "What did AMD disclose about the BIS AI Diffusion Rule?",
    "What did Intel disclose about the BIS revocation of Validated End-User authorizations in China?",
    "What did NVIDIA disclose about the BIS license review policy for advanced computing chips published "
    "in January 2026?",
    "Did Meta remove any risk factors between its FY2024 and FY2025 annual reports?",
    "Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?",
    "Did AMD stop disclosing its risk about \"Our ability to design and introduce new products in a timely "
    "manner includes the use of third-party intellectual property.\" between its FY2024 and FY2025 annual "
    "reports?",
    "Did Micron stop disclosing its risk about \"We may be unable to generate sufficient cash flows or "
    "obtain access to external financing necessary to fund our operations, make scheduled debt payments, "
    "pay our dividend, and make adequate capital investments.\" between its FY2024 and FY2025 annual "
    "reports?",
    "Did Nvidia stop disclosing its risk about \"We may not be able to realize the potential benefits of "
    "business investments or acquisitions, and we may not be able to successfully integrate acquired "
    "companies, which could hurt our ability to grow our business, develop new products or sell our "
    "products.\" between its FY2024 and FY2025 annual reports?",
    "Does NVIDIA's FY2026 10-K still say that the Notified Advanced Computing (NAC) process has not "
    "resulted in approvals for exports of products to customers in China, or was that statement removed?",
    "Does NVIDIA's FY2026 10-K still say that it transitioned some operations out of China and Hong Kong "
    "after the 2022 export controls, or was that statement removed?",
    "Does NVIDIA's FY2026 10-K still say that the AI Diffusion IFR would confer special benefits on "
    "select \"Universal Verified End Users\" (UVEU), or was that statement removed?",
)

EmbedFn = Callable[[Path, Path, Sequence[str]], np.ndarray]  # (model, tokenizer.json, questions) -> unit-norm rows


# ------------------------------------------------------------------------------------------ accuracy_level on the graph

def _check_level(level: int) -> None:
    if level not in ACCURACY_LEVELS:
        raise ValueError(f"{ACCURACY_ATTR} must be one of {ACCURACY_LEVELS}, got {level!r}")


def _graphs(graph) -> Iterator:
    """``graph`` and every subgraph nested in its nodes' attributes (If / Loop / Scan bodies)."""
    yield graph
    for node in graph.node:
        for attr in node.attribute:
            if attr.HasField("g"):
                yield from _graphs(attr.g)
            for sub in attr.graphs:
                yield from _graphs(sub)


def _matmul_nbits_nodes(model_proto: "ModelProto") -> Iterator:
    for graph in _graphs(model_proto.graph):
        for node in graph.node:
            if node.op_type == MATMUL_NBITS:
                yield node


def _level_of(node) -> int:
    """The node's accuracy_level; an absent attribute is level 0 (onnxruntime's default, and the quantizer omits it at 0)."""
    return next((attr.i for attr in node.attribute if attr.name == ACCURACY_ATTR), 0)


def count_matmul_nbits(model_proto: "ModelProto") -> int:
    return sum(1 for _ in _matmul_nbits_nodes(model_proto))


def count_nodes_missing_accuracy_level(model_proto: "ModelProto", level: int) -> int:
    """How many MatMulNBits nodes (subgraphs included) do not run at ``level``."""
    _check_level(level)
    return sum(1 for node in _matmul_nbits_nodes(model_proto) if _level_of(node) != level)


def set_accuracy_level(model_proto: "ModelProto", level: int) -> int:
    """Make every MatMulNBits node run at ``level``, IN PLACE; returns how many nodes changed.

    An existing attribute is replaced (never duplicated); other attributes are untouched; a node already at ``level`` (an
    absent attribute counts as 0) is left alone, so a second call returns 0."""
    from onnx import helper

    _check_level(level)
    changed = 0
    for node in _matmul_nbits_nodes(model_proto):
        if _level_of(node) == level:
            continue
        replacement = helper.make_attribute(ACCURACY_ATTR, level)
        existing = next((attr for attr in node.attribute if attr.name == ACCURACY_ATTR), None)
        if existing is None:
            node.attribute.append(replacement)
        else:
            existing.CopyFrom(replacement)
        changed += 1
    return changed


# ------------------------------------------------------------------------------------------ the in-build patch gate

def cosine_gate(patched: np.ndarray, unpatched: np.ndarray) -> dict:
    """Row-wise cosine between two embedding sets and the pass/fail decision (mean and min thresholds above)."""
    a, b = np.asarray(patched, dtype=np.float64), np.asarray(unpatched, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 2:
        raise ValueError(f"the two vector sets must share one 2-D shape, got {a.shape} and {b.shape}")
    if a.shape[0] == 0:
        raise ValueError("no vectors to compare")
    cos = ((a / np.linalg.norm(a, axis=1, keepdims=True)) * (b / np.linalg.norm(b, axis=1, keepdims=True))).sum(axis=1)
    mean, low = float(cos.mean()), float(cos.min())
    return {"questions": int(a.shape[0]), "cosine_mean": mean, "cosine_min": low,
            "mean_threshold": PATCH_COS_MEAN_MIN, "min_threshold": PATCH_COS_MIN_MIN,
            "passed": bool(mean >= PATCH_COS_MEAN_MIN and low >= PATCH_COS_MIN_MIN)}


class QueryEncoder:
    """Self-contained twin of ``semigraph.embeddings_onnx.OnnxBackend`` for queries (this script is copied alone into the
    Docker model stage). Same recipe: prompt + question, last-token pooling, L2 normalisation."""

    def __init__(self, model_path: Path, tokenizer_path: Path, threads: int = 0) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self._inputs = self.session.get_inputs()

    def _feeds(self, ids: np.ndarray) -> dict:
        feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
        for inp in self._inputs:
            if inp.name == "position_ids":
                feeds[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
            elif inp.name.startswith("past_key_values"):
                shape = [1 if isinstance(d, str) else d for d in inp.shape]
                shape[2] = 0  # empty cache: the graph is a plain encoder pass for us
                feeds[inp.name] = np.zeros(shape, dtype=np.float16 if "float16" in inp.type else np.float32)
        return feeds

    def embed_query(self, question: str) -> np.ndarray:
        ids = np.array([self.tokenizer.encode(QUERY_PROMPT + question).ids], dtype=np.int64)
        out = self.session.run(["last_hidden_state"], self._feeds(ids))[0]
        vec = out[0, -1, :].astype(np.float32)
        return vec / np.linalg.norm(vec)


def embed_questions(model_path: Path, tokenizer_path: Path, questions: Sequence[str], threads: int = 0) -> np.ndarray:
    """One unit-norm row per question. The session is released before returning, so the gate never holds two ~1 GB
    sessions at once (the Fly remote builder was OOM-killed once on a single 1.1 GB save)."""
    encoder = QueryEncoder(model_path, tokenizer_path, threads)
    try:
        return np.vstack([encoder.embed_query(q) for q in questions])
    finally:
        del encoder
        gc.collect()


def load_graph(model_path: Path) -> "ModelProto":
    """The graph without the weights when they sit in a sidecar file (a few MB); the whole proto for a single-file model."""
    import onnx

    return onnx.load(str(model_path), load_external_data=False)


def write_unpatched_variant(proto: "ModelProto", variant_path: Path) -> Path:
    """Save ``proto`` with accuracy_level 0 on every MatMulNBits node as ``variant_path`` (``proto`` is modified in place).

    It must sit in the model's own directory: the external-weights reference inside the graph is relative to the file, so
    the twin reads the same sidecar and costs no second copy of the weights."""
    import onnx

    set_accuracy_level(proto, 0)
    onnx.save_model(proto, str(variant_path))
    return variant_path


def missing_gate_dependencies() -> list[str]:
    return [name for name in GATE_DEPENDENCIES if importlib.util.find_spec(name) is None]


def _attribute_failure(report: dict, model_name: str) -> str | None:
    if report["matmulnbits_nodes"] == 0:
        return f"no MatMulNBits nodes in {model_name} (not a quantized model?)"
    if report["nodes_missing_accuracy_level"]:
        return (f"{report['nodes_missing_accuracy_level']} of {report['matmulnbits_nodes']} MatMulNBits nodes "
                f"lack {ACCURACY_ATTR}={report['accuracy_level']}")
    return None


def _cosine_failure(gate: dict) -> str:
    return (f"cosine to the unpatched twin: mean {gate['cosine_mean']:.5f} (>= {gate['mean_threshold']}), "
            f"min {gate['cosine_min']:.5f} (>= {gate['min_threshold']})")


def verify_patched(out_dir: Path, level: int, *, questions: Sequence[str] = GATE_QUESTIONS,
                   embed: EmbedFn | None = None) -> dict:
    """The --verify-patched gate for ``out_dir/model_q8.onnx``; returns the report (``passed``, ``failure``, ...)."""
    if not questions:
        raise ValueError("the patch gate needs at least one question")
    embed = embed or embed_questions
    model, tokenizer = out_dir / MODEL_NAME, out_dir / "tokenizer.json"
    proto = load_graph(model)
    report = {"accuracy_level": level, "matmulnbits_nodes": count_matmul_nbits(proto),
              "nodes_missing_accuracy_level": count_nodes_missing_accuracy_level(proto, level),
              "questions": len(questions), "cosine_mean": None, "cosine_min": None,
              "mean_threshold": PATCH_COS_MEAN_MIN, "min_threshold": PATCH_COS_MIN_MIN,
              "passed": False, "failure": None}
    report["failure"] = _attribute_failure(report, model.name)
    if report["failure"]:
        return report
    twin = write_unpatched_variant(proto, out_dir / GATE_TWIN_NAME)
    del proto
    gc.collect()
    try:
        patched = embed(model, tokenizer, questions)
        unpatched = embed(twin, tokenizer, questions)
    finally:
        twin.unlink(missing_ok=True)
    gate = cosine_gate(patched, unpatched)
    return {**report, "cosine_mean": gate["cosine_mean"], "cosine_min": gate["cosine_min"], "passed": gate["passed"],
            "failure": None if gate["passed"] else _cosine_failure(gate)}


# ------------------------------------------------------------------------------------------ build + verify

def download(repo: str, filename: str) -> Path:
    from huggingface_hub import hf_hub_download
    try:
        return Path(hf_hub_download(repo, filename, local_files_only=True))
    except Exception:
        return Path(hf_hub_download(repo, filename))


def quantize(fp32_model: Path, fp32_data: Path, out_model: Path, bits: int, block_size: int,
             accuracy_level: int = ACCURACY_LEVEL_DEFAULT) -> None:
    import onnx
    from onnxruntime.quantization.matmul_nbits_quantizer import (
        DefaultWeightOnlyQuantConfig, MatMulNBitsQuantizer)

    # The Hugging Face cache stores files as symlinks (on Linux); onnx refuses to
    # read external weights through a symlink, so stage real copies first.
    work = Path(tempfile.mkdtemp(prefix="qwen3-fp32-"))
    for src, name in ((fp32_model, "model.onnx"), (fp32_data, "model.onnx_data")):
        shutil.copyfile(Path(src).resolve(), work / name)
    print(f"loading {work / 'model.onnx'} ...", flush=True)
    model = onnx.load(str(work / "model.onnx"), load_external_data=True)
    shutil.rmtree(work, ignore_errors=True)
    # level 0 is "unset": the quantizer omits the attribute then, exactly like the build before M5
    config = DefaultWeightOnlyQuantConfig(block_size=block_size, is_symmetric=True, bits=bits,
                                          accuracy_level=accuracy_level or None)
    quant = MatMulNBitsQuantizer(model, algo_config=config)
    t = time.time()
    quant.process()
    print(f"quantized in {time.time() - t:.0f}s -> saving {out_model}", flush=True)
    out_model.parent.mkdir(parents=True, exist_ok=True)
    quantized = quant.model.model
    by_post_pass = set_accuracy_level(quantized, accuracy_level)  # independent of what the quantizer honoured
    print(f"{ACCURACY_ATTR}={accuracy_level} on {count_matmul_nbits(quantized)} MatMulNBits nodes "
          f"({by_post_pass} set by the post-quantize pass)", flush=True)
    # Memory-lean save: the weights go to a sidecar file (model_q8.onnx_data) so the
    # ~1.1 GB proto is never serialized as one in-memory bytes object — the Fly remote
    # builder OOM-killed the single-file save. onnxruntime resolves the sidecar itself.
    del model, quant
    gc.collect()
    onnx.save_model(quantized, str(out_model), save_as_external_data=True,
                    all_tensors_to_one_file=True, location=out_model.name + "_data",
                    size_threshold=1024)


def verify(out_dir: Path) -> dict:
    """Fidelity of the ONNX backend vs sentence-transformers on the benchmark questions."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from semigraph.artifacts import load_benchmark
    from semigraph.embeddings import Embedder

    questions = [b["q"] for b in load_benchmark()]
    ref = Embedder(backend="local")
    ref_vecs = np.array([ref.encode_query(q) for q in questions], dtype=np.float32)
    from semigraph.embeddings_onnx import OnnxBackend
    onnx_backend = OnnxBackend(out_dir / MODEL_NAME, out_dir / "tokenizer.json")
    vecs = np.array([onnx_backend.encode_query(q) for q in questions], dtype=np.float32)
    cos = (vecs * ref_vecs).sum(axis=1)
    report = {"questions": len(questions), "cosine_min": float(cos.min()),
              "cosine_mean": float(cos.mean()), "threshold": COS_MIN,
              "passed": bool(cos.min() >= COS_MIN)}
    print("fidelity:", json.dumps(report), flush=True)
    return report


# ------------------------------------------------------------------------------------------ command line

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="models/qwen3-embedding-0.6b-q8")
    ap.add_argument("--bits", type=int, default=8)
    ap.add_argument("--block-size", type=int, default=128)
    ap.add_argument("--accuracy-level", type=int, choices=ACCURACY_LEVELS, default=ACCURACY_LEVEL_DEFAULT,
                    help="MatMulNBits accuracy_level on every node (0 = the build live today, the default; 4 = int8 "
                         "activations, faster, opt-in until the owner approves it)")
    ap.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                    help="fp32 reference comparison via sentence-transformers (needs torch + the semigraph package)")
    ap.add_argument("--verify-patched", action="store_true",
                    help="self-contained gate: accuracy_level on every node + cosine to an unpatched twin")
    ap.add_argument("--keep-unpatched", action="store_true",
                    help=f"leave the level-0 twin as {KEPT_TWIN_NAME} (a small graph file sharing the weights)")
    ap.add_argument("--force", action="store_true")
    return ap


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.verify_patched and args.accuracy_level == 0:
        ap.error("--verify-patched needs --accuracy-level above 0 (a level-0 model is its own unpatched twin)")
    return args


def remove_model_files(out_dir: Path) -> None:
    """model_q8.onnx and its sidecar (and any twin); the old single unlink left the ~1 GB sidecar behind."""
    for path in out_dir.glob("model_q8*"):
        path.unlink()


def fail(out_dir: Path, built_now: bool, message: str) -> None:
    if built_now:
        remove_model_files(out_dir)
        sys.exit(f"{message} — model removed")
    sys.exit(f"{message} — model kept (this run did not build it; rebuild with --force)")


def ensure_model(args: argparse.Namespace, out_model: Path) -> bool:
    """Build the model unless one is on disk; True when this run built it. A reused file is never modified."""
    if out_model.exists() and not args.force:
        print(f"{out_model} exists — skipping quantization (use --force to rebuild)")
        refuse_reused_model_with_wrong_level(out_model, args.accuracy_level)
        return False
    fp32 = download(REPO, FP32[0])
    data = download(REPO, FP32[1])  # external weights file
    quantize(fp32, data, out_model, args.bits, args.block_size, args.accuracy_level)
    return True


def refuse_reused_model_with_wrong_level(out_model: Path, level: int) -> None:
    """A model already on disk is never patched in place; one built at another level must not be reused silently
    (a level-4 file left in the default --out would otherwise become the baseline of a later bare run)."""
    proto = load_graph(out_model)
    missing = count_nodes_missing_accuracy_level(proto, level)
    if missing:
        sys.exit(f"{out_model.name} has {missing} of {count_matmul_nbits(proto)} MatMulNBits nodes without "
                 f"{ACCURACY_ATTR}={level}; it was built at another level and is not reused or patched — rebuild "
                 "with --force or build into another --out directory")


def run_fp32_gate(out_dir: Path, built_now: bool) -> None:
    report = verify(out_dir)
    (out_dir / "fidelity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["passed"]:
        fail(out_dir, built_now, f"FIDELITY FAILED (cosine min {report['cosine_min']:.4f} < {COS_MIN})")


def run_patch_gate(out_dir: Path, built_now: bool, level: int) -> None:
    report = verify_patched(out_dir, level)
    print("patch gate:", json.dumps(report), flush=True)
    (out_dir / "patch_fidelity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["passed"]:
        fail(out_dir, built_now, f"PATCH GATE FAILED ({report['failure']})")


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.verify_patched and (missing := missing_gate_dependencies()):
        sys.exit(f"--verify-patched needs {', '.join(missing)} in this environment; install it before the build "
                 "(the Docker model stage must pip install it)")
    out_dir = Path(args.out)
    out_model = out_dir / MODEL_NAME
    built_now = ensure_model(args, out_model)
    shutil.copy(download(REPO, "tokenizer.json"), out_dir / "tokenizer.json")
    total = sum(f.stat().st_size for f in out_dir.glob("model_q8.onnx*"))
    print(f"model size: {total / 1e6:.0f} MB ({', '.join(f.name for f in sorted(out_dir.glob('model_q8.onnx*')))})")

    try:
        if args.verify:
            run_fp32_gate(out_dir, built_now)
        if args.verify_patched:
            run_patch_gate(out_dir, built_now, args.accuracy_level)
    except Exception as exc:  # a gate that crashed (an ORT session error) has not approved the model it was checking
        fail(out_dir, built_now, f"GATE ERROR ({type(exc).__name__}: {exc})")
    if args.keep_unpatched:
        kept = write_unpatched_variant(load_graph(out_model), out_dir / KEPT_TWIN_NAME)
        print(f"kept the unpatched baseline: {kept} ({kept.stat().st_size / 1e6:.1f} MB)")
    print("done:", out_dir)


if __name__ == "__main__":
    main()
