"""Embeddings — Qwen3-Embedding-0.6B, 1024-dim (schema-locked), three backends.

``Embedder`` is a facade; ``settings.embedding_backend`` (or the ``backend``
argument) selects the implementation:

- ``local``  — sentence-transformers + torch. The pipeline default (notebooks,
  ``semigraph build-graph``). Conventions verified in notebooks 09/10/14: load
  from the local Hugging Face cache first (``local_files_only=True``), passages
  are embedded WITHOUT a prompt, queries get the model's "query" prompt.
- ``onnx``   — torch-free onnxruntime session over a quantized export built by
  ``scripts/build_onnx_embedder.py`` (what the web service image ships; that
  script verifies fidelity against ``local`` before it writes the file).
- ``remote`` — any OpenAI-compatible ``/embeddings`` endpoint serving the SAME
  model (the query instruction is prepended client-side).

All backends produce L2-normalized vectors in one space, so a query embedded
by any of them is comparable with passages embedded by ``local``. Embedding
caches are keyed per chunk_id: :func:`embed_chunks_cached` encodes only the
chunk_ids missing from the cache and keeps every stored vector as-is.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .config import get_settings

if TYPE_CHECKING:  # pipeline-only dependency, kept out of the serving path at runtime
    import pandas as pd

logger = logging.getLogger("semigraph.embeddings")

EMBED_DIM = 1024

# The model card's query instruction — identical to the sentence-transformers
# "query" prompt in the model's config_sentence_transformers.json.
QUERY_PROMPT = ("Instruct: Given a web search query, retrieve relevant passages "
                "that answer the query\nQuery:")

BACKENDS = ("local", "onnx", "remote")

# encode(texts, batch_size=..., show_progress=...) -> (len(texts), dim) array;
# ``Embedder.encode_passages`` is one, tests inject a fake.
EncodeFn = Callable[..., np.ndarray]


class LocalBackend:
    """sentence-transformers backend (the original notebook implementation)."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        try:
            self.model = SentenceTransformer(model_name, local_files_only=True)
        except OSError:
            logger.info("%s not in the local cache — downloading once", model_name)
            self.model = SentenceTransformer(model_name)
        self.name = model_name

    def encode_passages(self, texts: list[str], batch_size: int = 8,
                        show_progress: bool = False) -> np.ndarray:
        return self.model.encode(
            texts, batch_size=batch_size, show_progress_bar=show_progress,
            normalize_embeddings=True,
        )

    def encode_query(self, question: str) -> list[float]:
        if "query" in (self.model.prompts or {}):
            return self.model.encode(
                [question], prompt_name="query", normalize_embeddings=True
            )[0].tolist()
        return self.model.encode([question], normalize_embeddings=True)[0].tolist()


def _make_backend(backend: str, model_name: str | None):
    settings = get_settings()
    if backend == "local":
        return LocalBackend(model_name or settings.embedding_model)
    if backend == "onnx":
        from .embeddings_onnx import OnnxBackend
        return OnnxBackend(settings.onnx_model_path, settings.onnx_tokenizer_path,
                           threads=settings.onnx_threads)
    if backend == "remote":
        from .embeddings_remote import RemoteBackend
        return RemoteBackend(settings.embedding_api_base, settings.embedding_api_key,
                             settings.embedding_api_model)
    raise ValueError(f"unknown embedding backend {backend!r} — use one of {BACKENDS}")


class Embedder:
    """Facade over the configured backend (see module docstring)."""

    def __init__(self, model_name: str | None = None, backend: str | None = None):
        self.backend = (backend or get_settings().embedding_backend).lower()
        self._impl = _make_backend(self.backend, model_name)
        self.name = self._impl.name
        logger.info("embedder ready — backend=%s model=%s", self.backend, self.name)

    @property
    def model(self):
        """The underlying sentence-transformers model (``local`` backend only)."""
        return getattr(self._impl, "model", None)

    def encode_passages(self, texts: list[str], batch_size: int = 8,
                        show_progress: bool = False) -> np.ndarray:
        return self._impl.encode_passages(texts, batch_size=batch_size,
                                          show_progress=show_progress)

    def encode_query(self, question: str) -> list[float]:
        return self._impl.encode_query(question)

    def encode_chunks_cached(self, chunks_df: "pd.DataFrame", cache_path: Path,
                             batch_size: int = 8) -> np.ndarray:
        """Embed chunk rows (sub_heading + text) through the per-chunk cache:
        only chunk_ids missing from the cache are encoded (see
        :func:`embed_chunks_cached`). Rows come back in ``chunks_df`` order."""
        return embed_chunks_cached(chunks_df, cache_path, self.encode_passages,
                                   batch_size=batch_size)


def _embed_inputs(chunks_df: "pd.DataFrame") -> list[str]:
    """The text that is embedded per chunk: sub_heading + newline + text, stripped."""
    return (chunks_df["sub_heading"].fillna("") + "\n" + chunks_df["text"]).str.strip().tolist()


def _load_embedding_cache(cache_path: Path) -> dict[str, np.ndarray]:
    """chunk_id -> vector from the parquet cache (first occurrence wins).

    A missing cache is simply empty. An unreadable or wrong-shaped one is
    logged and treated as empty — it is derived data, rebuilt (and healed) by
    the caller.
    """
    import pandas as pd

    if not cache_path.exists():
        return {}
    try:
        cached = pd.read_parquet(cache_path, columns=["chunk_id", "embedding"])
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("embedding cache %s unreadable (%s) — re-encoding", cache_path.name, exc)
        return {}
    cache: dict[str, np.ndarray] = {}
    for chunk_id, vector in zip(cached["chunk_id"], cached["embedding"]):
        cache.setdefault(chunk_id, np.asarray(vector))
    return cache


def _write_embedding_cache(cache: dict[str, np.ndarray], cache_path: Path) -> None:
    """Write the cache atomically (temp file + rename): a crash mid-write must
    not destroy the vectors already computed."""
    import pandas as pd

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    try:
        pd.DataFrame(
            {"chunk_id": list(cache), "embedding": list(cache.values())}
        ).to_parquet(tmp, index=False)
        tmp.replace(cache_path)
    finally:
        tmp.unlink(missing_ok=True)


def _encode_missing(texts: list[str], encode: EncodeFn, batch_size: int) -> np.ndarray:
    # float32 always: the cache holds float32, so a first call and a later
    # cache hit must return the very same values whatever the backend emits
    vectors = np.asarray(encode(texts, batch_size=batch_size, show_progress=True),
                         dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != len(texts):
        raise ValueError(
            f"encoder returned shape {vectors.shape} — expected {len(texts)} rows "
            f"of {EMBED_DIM}-dim vectors"
        )
    return vectors


def embed_chunks_cached(chunks_df: "pd.DataFrame", cache_path: Path, encode: EncodeFn,
                        batch_size: int = 8) -> np.ndarray:
    """Per-chunk embedding cache (pure w.r.t. the model: ``encode`` is injected).

    - The cache is a parquet with columns ``chunk_id, embedding`` — the layout
      the notebooks wrote, so existing caches stay valid.
    - Only chunk_ids missing from it are encoded; cached vectors are returned
      untouched (bit-identical), so a chunk's vector never drifts once stored.
    - The merged cache is written back atomically (existing entries kept in
      order, new ones appended) and only when something new was encoded.
    - Returns one row per ``chunks_df`` row, in ``chunks_df`` order.

    The cache is keyed by chunk_id alone: if the embedding model changes,
    delete the cache file.
    """
    chunk_ids = chunks_df["chunk_id"].tolist()
    if not chunk_ids:
        return np.empty((0, EMBED_DIM), dtype=np.float32)
    cache = _load_embedding_cache(cache_path)
    text_by_id: dict[str, str] = {}
    for chunk_id, text in zip(chunk_ids, _embed_inputs(chunks_df)):
        text_by_id.setdefault(chunk_id, text)
    missing = [cid for cid in text_by_id if cid not in cache]
    if missing:
        logger.info("encoding %d new chunk(s); %d already cached (%s)",
                    len(missing), len(text_by_id) - len(missing), cache_path.name)
        new_vectors = _encode_missing([text_by_id[cid] for cid in missing], encode, batch_size)
        cache = {**cache, **dict(zip(missing, new_vectors))}
        _write_embedding_cache(cache, cache_path)
    else:
        logger.info("reusing cached embeddings for %d chunks (%s)",
                    len(text_by_id), cache_path.name)
    return np.vstack([cache[cid] for cid in chunk_ids])
