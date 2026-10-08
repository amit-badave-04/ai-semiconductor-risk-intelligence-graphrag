"""Torch-free ONNX backend for Qwen3-Embedding-0.6B (onnxruntime + tokenizers).

The graph we run is the community fp32 export
(``onnx-community/Qwen3-Embedding-0.6B-ONNX``) with 8-bit block-wise
weight-only quantization applied by ``scripts/build_onnx_embedder.py``. Run
locally (with torch available) the script verifies fidelity against the
sentence-transformers backend and refuses to keep a failing file; the result
is committed as ``artifacts/onnx_embedder_fidelity.json``. The Docker image
rebuilds the same deterministic quantization with ``--no-verify`` (no torch in
the build stage) — change the recipe only together with a fresh local verify.

Inference conventions (verified token-for-token against sentence-transformers
in the productionization spike):
- text = QUERY_PROMPT + question for queries; passages get no prompt
- tokenizer.json from the same repo, no extra special tokens
- last-token pooling (the model's 1_Pooling config), then L2 normalization
- the Transformers.js-style export carries KV-cache inputs; they are fed
  empty (past length 0) — for us the graph is a plain encoder pass

Two model variants load through the same code, by path:

- ``q8``   — the 8-bit file above (``model_q8.onnx``). ONNX Runtime 1.29 dequantizes every
  weight on every call.
- ``fp32`` — the same weights dequantized ONCE, offline, into float32 initializers
  (``scripts/predequantize_embedder.py``): ``model_fp32.onnx`` plus its external-data file
  ``model_fp32.onnx.data``, which ONNX Runtime resolves itself from the model's directory.
  Identical retrieval, several times faster, about 2.3 GB instead of 1.0 GB.

``variant`` and ``fidelity`` (a few numbers proving the build) come from the
``<model>.fidelity.json`` beside the model; the shipped 8-bit file has none and is
recognised by its name. ``/healthz`` reports both. This module imports only the standard
library besides numpy, onnxruntime and tokenizers: the serving image has no ``onnx`` package.
"""

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("semigraph.embeddings.onnx")

MAX_TOKENS = 8192  # bounds memory; chunks are far shorter, queries tiny

VARIANT_Q8, VARIANT_FP32, VARIANT_UNKNOWN = "q8", "fp32", "unknown"
KNOWN_VARIANTS = (VARIANT_Q8, VARIANT_FP32)
SHIPPED_Q8_NAME = "model_q8.onnx"
FIDELITY_SUFFIX = ".fidelity.json"
SOURCE_DIGEST_CHARS = 16  # /healthz shows a prefix of the source model's sha256, enough to tell builds apart


def fidelity_path(model_path: Path) -> Path:
    """``model_fp32.onnx`` -> ``model_fp32.fidelity.json`` (beside it)."""
    return model_path.with_name(model_path.stem + FIDELITY_SUFFIX)


def read_fidelity(model_path: Path) -> dict | None:
    """The fidelity report beside ``model_path``, or None. A missing file is normal (the 8-bit model has none); one that is
    unreadable, malformed or of an unknown variant is logged and ignored: a bad report must never stop the service."""
    path = fidelity_path(model_path)
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring the fidelity file %s: %s", path, exc)
        return None
    if not isinstance(report, dict) or report.get("variant") not in KNOWN_VARIANTS:
        logger.warning("ignoring the fidelity file %s: not a report of a known variant %s", path, KNOWN_VARIANTS)
        return None
    return report


def variant_of(model_path: Path, report: dict | None) -> str:
    """``report['variant']`` when there is a report; else ``q8`` for the shipped file name and ``unknown`` for any other."""
    if report:
        return report["variant"]
    return VARIANT_Q8 if model_path.name == SHIPPED_Q8_NAME else VARIANT_UNKNOWN


def _number(value) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def fidelity_summary(report: dict) -> dict:
    """The numbers ``/healthz`` shows: how many nodes were dequantized, the worst difference from ONNX Runtime's own
    dequantization, the cosine gate against the 8-bit model and a prefix of the source model's hash. Always the same keys,
    None where the report lacks (or mistypes) a value."""
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    digest = source.get("sha256")
    return {"nodes": _number(report.get("dequantized_nodes")), "max_node_abs_diff": _number(report.get("max_node_abs_diff")),
            "cosine_min": _number(gate.get("cosine_min")), "cosine_mean": _number(gate.get("cosine_mean")),
            "source_sha256": digest[:SOURCE_DIGEST_CHARS] if isinstance(digest, str) and digest else None}


def _require_data_file(model_path: Path, report: dict | None) -> None:
    """The external-data file the report declares must sit beside the model: name it, rather than let ONNX Runtime fail with
    a message about a protobuf. Only a file NAME is honoured (never a path out of the model's directory)."""
    declared = report.get("data") if report else None
    if isinstance(declared, str) and declared:
        data_path = model_path.parent / Path(declared).name
        if not data_path.exists():
            raise FileNotFoundError(f"ONNX embedder files missing: {data_path} (the external weights of {model_path.name})")


class OnnxBackend:
    variant: str = VARIANT_UNKNOWN          # class defaults: tests build bare instances with ``object.__new__``
    fidelity: dict | None = None

    def __init__(self, model_path: Path | str | None,
                 tokenizer_path: Path | str | None = None, threads: int = 0):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        from .embeddings import QUERY_PROMPT

        if not model_path:
            raise RuntimeError(
                "ONNX_MODEL_PATH is not set — build the quantized embedder with "
                "`python scripts/build_onnx_embedder.py` and point ONNX_MODEL_PATH at it."
            )
        model_path = Path(model_path)
        tokenizer_path = (Path(tokenizer_path) if tokenizer_path
                          else model_path.parent / "tokenizer.json")
        if not model_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(
                f"ONNX embedder files missing: {model_path} / {tokenizer_path}")
        report = read_fidelity(model_path)
        _require_data_file(model_path, report)
        self.variant = variant_of(model_path, report)
        self.fidelity = fidelity_summary(report) if report else None
        self._prompt = QUERY_PROMPT
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options,
                                            providers=["CPUExecutionProvider"])
        self._inputs = self.session.get_inputs()
        self.name = f"onnx:{model_path.name}"
        logger.info("onnx embedder loaded from %s (variant %s)", model_path, self.variant)

    def _feeds(self, ids: np.ndarray) -> dict:
        feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
        for inp in self._inputs:
            if inp.name == "position_ids":
                feeds[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
            elif inp.name.startswith("past_key_values"):
                shape = [1 if isinstance(d, str) else d for d in inp.shape]
                shape[2] = 0  # empty cache
                dtype = np.float16 if "float16" in inp.type else np.float32
                feeds[inp.name] = np.zeros(shape, dtype=dtype)
        return feeds

    def embed_one(self, text: str) -> np.ndarray:
        ids = np.array([self.tokenizer.encode(text).ids], dtype=np.int64)
        out = self.session.run(["last_hidden_state"], self._feeds(ids))[0]
        vec = out[0, -1, :].astype(np.float32)  # last-token pooling
        return vec / np.linalg.norm(vec)

    def encode_passages(self, texts: list[str], batch_size: int = 8,
                        show_progress: bool = False) -> np.ndarray:
        return np.vstack([self.embed_one(t) for t in texts])

    def encode_query(self, question: str) -> list[float]:
        return self.embed_one(self._prompt + question).tolist()

    def count_tokens(self, text: str) -> int:
        """Exact token count, never capped by the tokenizer's truncation (counted in short pieces, see
        :func:`semigraph.embeddings.split_for_counting`)."""
        from .embeddings import count_tokens_with

        return count_tokens_with(lambda piece: self.tokenizer.encode(piece).ids, text)
