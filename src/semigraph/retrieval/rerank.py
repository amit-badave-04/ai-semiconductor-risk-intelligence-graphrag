"""Local cross-encoder rerank stage (docs/specs/m2-search.md I3b; owner: "Local first, hosted later").

``Reranker`` is the protocol; ``LocalCrossEncoder`` implements it with onnxruntime + tokenizers over a model directory (no
network, no torch). A hosted reranker is NOT implemented: it can be added behind a setting, with the owner's approval, by
implementing the same protocol. Never imported by the upload / workspace-document path (a test pins that).

``get_reranker`` loads a model once per process, lazily; a missing or broken model returns None and logs (never raises), so the
fused order is served unchanged.
"""

import logging
import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np

logger = logging.getLogger("semigraph.retrieval.rerank")

# key -> (directory under the model root, ONNX file inside it)
RERANK_MODELS = {
    "minilm": ("reranker-minilm-l6", "onnx/model_quint8_avx2.onnx"),
    "mxbai": ("reranker-mxbai-xsmall", "onnx/model_quantized.onnx"),
}
MAX_TOKENS = 384        # query + passage tokens per pair; the rest of a passage is cut
BATCH_SIZE = 1          # padding shifts the dynamic-quantised scores (~0.1 logit), so a score never depends on its batch mates
SCORE_DIGITS = 6


class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[str]) -> list[float]:
        """One relevance score per candidate text, higher is better, in the order given."""
        ...


class LocalCrossEncoder:
    """An ONNX cross-encoder (BERT or DeBERTa-v2 style, one logit). Only the inputs the graph declares are fed."""

    def __init__(self, model_dir: Path, onnx_file: str, max_tokens: int = MAX_TOKENS, batch_size: int = BATCH_SIZE,
                 threads: int = 1):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.log_severity_level = 3
        if threads > 0:                       # 0 = onnxruntime default; the serving default is 1 so asks do not fight the embedder
            options.intra_op_num_threads, options.inter_op_num_threads = threads, 1
        self._session = ort.InferenceSession(str(model_dir / onnx_file), options, providers=["CPUExecutionProvider"])
        self._inputs = [i.name for i in self._session.get_inputs()]
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.no_padding()
        self._tokenizer.enable_truncation(max_length=max_tokens, strategy="longest_first")
        self._batch = batch_size

    def _batch_inputs(self, query: str, passages: Sequence[str]) -> dict[str, np.ndarray]:
        encs = self._tokenizer.encode_batch([(query, p) for p in passages])
        width = max(len(e.ids) for e in encs)
        feeds = {name: np.zeros((len(encs), width), dtype=np.int64) for name in self._inputs}   # pad id 0, mask 0, type 0
        for i, e in enumerate(encs):
            n = len(e.ids)
            columns = {"input_ids": e.ids, "attention_mask": e.attention_mask, "token_type_ids": e.type_ids}
            for name, values in columns.items():
                if name in feeds:
                    feeds[name][i, :n] = values
        return feeds

    def rerank(self, query: str, candidates: Sequence[str]) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(candidates), self._batch):
            logits = self._session.run(None, self._batch_inputs(query, candidates[start:start + self._batch]))[0]
            scores.extend(round(float(x), SCORE_DIGITS) for x in np.asarray(logits).reshape(len(logits), -1)[:, 0])
        return scores


def default_root() -> Path:
    from ..config import get_settings

    return Path(get_settings().retrieval_rerank_model_dir)


def default_threads() -> int:
    from ..config import get_settings

    return get_settings().retrieval_rerank_threads


_LOAD_LOCK = threading.Lock()      # two first asks at once must not load (and hold in memory) the model twice


@lru_cache(maxsize=None)
def _load(key: str, root: str, threads: int) -> Reranker | None:
    folder, onnx_file = RERANK_MODELS[key]
    model_dir = Path(root) / folder
    if not (model_dir / onnx_file).is_file() or not (model_dir / "tokenizer.json").is_file():
        logger.warning("rerank model %r not found under %s: the rerank stage is disabled", key, model_dir)
        return None
    try:
        encoder = LocalCrossEncoder(model_dir, onnx_file, threads=threads)
    except Exception as exc:  # noqa: BLE001 - a broken model must not take retrieval down
        logger.warning("rerank model %r failed to load (%s): the rerank stage is disabled", key, type(exc).__name__)
        return None
    logger.info("rerank model %r loaded from %s", key, model_dir)
    return encoder


def get_reranker(key: str, root: Path | None = None, threads: int | None = None) -> Reranker | None:
    """The process-wide reranker for ``key`` (loaded on first use), or None when its files are missing or unloadable."""
    if key not in RERANK_MODELS:
        raise ValueError(f"unknown rerank model {key!r}; one of {', '.join(RERANK_MODELS)}")
    with _LOAD_LOCK:
        return _load(key, str(root if root is not None else default_root()), threads if threads is not None else default_threads())


get_reranker.cache_clear = _load.cache_clear   # type: ignore[attr-defined]


def reorder(fused: Sequence[tuple[str, float]], scores: dict[str, float]) -> list[tuple[str, float]]:
    """The scored candidates first (best rerank score first, ties by chunk id) carrying their rerank score, then every other
    fused entry in its fused order with a score below all of them."""
    head = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    floor = (head[-1][1] if head else 0.0) - 1.0
    tail = [(cid, round(floor - rank * 1e-6, 9)) for rank, (cid, _) in enumerate(c for c in fused if c[0] not in scores)]
    return head + tail
