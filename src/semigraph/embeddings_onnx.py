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
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("semigraph.embeddings.onnx")

MAX_TOKENS = 8192  # bounds memory; chunks are far shorter, queries tiny


class OnnxBackend:
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
        logger.info("onnx embedder loaded from %s", model_path)

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
